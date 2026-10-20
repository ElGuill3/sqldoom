"""T3: sqldoom-view, the interactive terminal viewer (D9).

Python owns only I/O and the camera: key parsing (parse_keys) and camera updates
(step) are pure and unit-tested here; the pty test drives the whole viewer end to
end, always with deadlines so it can never hang the suite.
"""

import fcntl
import math
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from importlib.resources import files

import duckdb

from conftest import run_import
from sqldoom.view import FIRST_HOLD, REPEAT_HOLD, Keys, due_tics, parse_keys, run_viewer
from test_render import corridor_map_lumps
from wadbuilder import write_wad


def make_db(tmp):
    wad = write_wad(tmp / "map.wad", corridor_map_lumps())  # player 1 starts at (0, 0, 0)
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


# --- parse_keys ---------------------------------------------------------------


def test_parse_keys_maps_wasd_and_arrow_sequences():
    assert parse_keys(b"w") == ["forward"]
    assert parse_keys(b"s") == ["back"]
    assert parse_keys(b"a") == ["left"]
    assert parse_keys(b"d") == ["right"]
    assert parse_keys(b"\x1b[A") == ["forward"]
    assert parse_keys(b"\x1b[B") == ["back"]
    assert parse_keys(b"\x1b[D") == ["left"]
    assert parse_keys(b"\x1b[C") == ["right"]


def test_parse_keys_applies_a_mixed_batch_in_order():
    assert parse_keys(b"ww\x1b[Dq") == ["forward", "forward", "left", "quit"]


def test_parse_keys_quit_bytes():
    assert parse_keys(b"q") == ["quit"]
    assert parse_keys(b"\x03") == ["quit"]  # Ctrl-C arrives as a byte in raw mode
    assert parse_keys(b"\x1b") == ["quit"]  # a lone Esc


def test_parse_keys_ignores_unknown_bytes():
    assert parse_keys(b"x\x01w") == ["forward"]
    assert parse_keys(b"\x1b[Z") == []  # an unknown escape sequence
    assert parse_keys(b"") == []


def test_parse_keys_strafe_and_run():
    assert parse_keys(b",.") == ["strafe_left", "strafe_right"]
    assert parse_keys(b"WASD") == ["run", "forward", "run", "left", "run", "back", "run", "right"]
    # Shift + arrows: CSI 1;2 A/B/C/D
    assert parse_keys(b"\x1b[1;2A\x1b[1;2D") == ["run", "forward", "run", "left"]


# --- held keys (M7) -------------------------------------------------------------


def test_a_fresh_press_holds_long_enough_to_bridge_the_repeat_delay():
    keys = Keys()
    keys.press(["forward"], now=10.0)
    assert keys.controls(10.0) == (1, 0, 0, False)
    assert keys.controls(10.0 + FIRST_HOLD - 0.01) == (1, 0, 0, False)
    assert keys.controls(10.0 + FIRST_HOLD + 0.01) == (0, 0, 0, False)


def test_auto_repeats_keep_a_key_held_until_they_stop():
    keys = Keys()
    for t in (10.0, 10.5, 10.53, 10.56):  # press, then auto-repeat
        keys.press(["left"], now=t)
    assert keys.controls(10.56 + REPEAT_HOLD - 0.01) == (0, 0, 1, False)
    assert keys.controls(10.56 + REPEAT_HOLD + 0.01) == (0, 0, 0, False)


def test_controls_combine_opposite_and_independent_keys():
    keys = Keys()
    keys.press(["forward", "back", "strafe_right", "right", "run", "forward"], now=0.0)
    assert keys.controls(0.1) == (0, 1, -1, True)
    keys = Keys()
    keys.press(["back", "strafe_left", "left"], now=0.0)
    assert keys.controls(0.1) == (-1, -1, 1, False)


# --- tic clock (M7) -------------------------------------------------------------


def test_due_tics_follow_a_35_hz_clock_and_are_capped():
    assert due_tics(start=0.0, now=0.0, done=0) == 0
    assert due_tics(start=0.0, now=1.0, done=0) == 10  # capped: a slow frame never spirals
    assert due_tics(start=0.0, now=1.0, done=30) == 5
    assert due_tics(start=0.0, now=1.0, done=35) == 0
    assert due_tics(start=0.0, now=0.1, done=0, cap=100) == 3  # 0.1 s = 3.5 tics


# --- end to end over a pseudo-terminal -----------------------------------------


def spawn_viewer(db, rows: int, cols: int, *args):
    """Run the viewer on a fresh pty sized rows x cols, keeping the slave fd open
    so the test can inspect the terminal (winsize, termios) while it runs."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(
        [sys.executable, "-m", "sqldoom.view", str(db), "E1M1", *map(str, args)],
        stdin=slave, stdout=slave, stderr=slave,
    )
    return proc, master, slave


def start_viewer(db, *args):
    proc, master, slave = spawn_viewer(db, 24, 80, *args)
    os.close(slave)
    return proc, master


def stop_viewer(proc, master, slave=None):
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    os.close(master)
    if slave is not None:
        os.close(slave)


def read_until(master: int, needle: bytes, deadline: float, buf: bytes = b"") -> bytes:
    """Read from the pty master until `needle` appears, EOF, or the deadline."""
    while needle not in buf and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the child closed its side, same as EOF
                break
            if not chunk:
                break
            buf += chunk
    return buf


def drain(master: int, seconds: float) -> bytes:
    """Read whatever arrives on the pty master within `seconds`."""
    buf = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:  # EIO: the child closed its side, same as EOF
            break
        if not chunk:
            break
        buf += chunk
    return buf


def latest_frame(buf: bytes) -> bytes:
    """The content of the last complete synchronized frame in `buf`."""
    end = buf.rfind(b"\x1b[?2026l")
    assert end != -1, buf[-200:]
    start = buf.rfind(b"\x1b[?2026h", 0, end)
    assert start != -1, buf[-200:]
    return buf[start + len(b"\x1b[?2026h"):end]


def render_rows(frame: bytes) -> list[bytes]:
    """The ▀-cell text rows of one frame (everything but the status line)."""
    return [row for row in frame.split(b"\n") if "▀".encode() in row]


def test_viewer_walks_forward_and_quits_cleanly(tmp):
    db, proc, master = make_db(tmp), None, None
    proc, master = start_viewer(db, "--size", "20x8")
    try:
        deadline = time.monotonic() + 30
        buf = read_until(master, b" fps", deadline)
        assert b"\x1b[?1049h" in buf  # alternate screen entered
        assert b"\x1b[?25l" in buf  # cursor hidden
        assert b"E1M1  x=0 y=0 angle=0" in buf  # the first status line

        os.write(master, b"w")  # M7: one press walks for FIRST_HOLD, then coasts
        buf += drain(master, 4.0)
        xs = [int(x) for x in re.findall(rb"E1M1  x=(-?\d+) y=0 ", buf)]
        assert xs[0] == 0 and xs == sorted(xs)  # walked east, never back
        assert 96 < xs[-1] <= 112  # stopped by the east wall (x = 128) at the radius
        assert buf.count(b" fps") >= 2  # at least two status lines

        os.write(master, b"q")
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the child exited and closed the slave
                break
            if not chunk:
                break
            buf += chunk
        assert proc.wait(timeout=10) == 0
        assert b"\x1b[?1049l" in buf  # alternate screen left
        assert buf.endswith(b"\x1b[?25h\x1b[?1049l")  # cursor shown back at the end
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)


def test_viewer_fails_cleanly_without_textures(tmp):
    """G3: a database imported before the texture importer fails cleanly."""
    wad = write_wad(tmp / "map.wad", corridor_map_lumps(graphics=False))
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    proc, master, slave = spawn_viewer(db, rows=9, cols=20)
    try:
        deadline = time.monotonic() + 30
        buf = read_until(master, b"database has no textures", deadline)
        assert b"error: database has no textures: re-run sqldoom-import --force" in buf
        assert proc.wait(timeout=10) == 1
    finally:
        stop_viewer(proc, master, slave)


def test_viewer_fails_cleanly_with_a_start_outside_the_map(tmp):
    proc, master, slave = spawn_viewer(make_db(tmp), 9, 20, "--pos", "1000,0")
    try:
        deadline = time.monotonic() + 30
        buf = read_until(master, b"outside the map", deadline)
        assert b"error: position is outside the map: 1000.0,0.0" in buf
        assert b"\x1b[?1049h" not in buf  # failed before taking over the terminal
        assert proc.wait(timeout=10) == 1
    finally:
        stop_viewer(proc, master, slave)


def test_requires_a_tty(tmp):
    db = make_db(tmp)

    result = subprocess.run(
        [sys.executable, "-m", "sqldoom.view", str(db), "E1M1"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: sqldoom-view needs an interactive terminal\n"


# --- resize (SIGWINCH) ----------------------------------------------------------


def test_viewer_redraws_on_terminal_resize(tmp):
    db, proc, master, slave = make_db(tmp), None, None, None
    proc, master, slave = spawn_viewer(db, rows=9, cols=20)  # no --size: follow resizes
    try:
        deadline = time.monotonic() + 30
        buf = read_until(master, b" fps", deadline)
        assert b"E1M1  x=0 y=0 angle=0" in buf

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 11, 30, 0, 0))
        os.kill(proc.pid, signal.SIGWINCH)
        buf = read_until(master, b"\x1b[2J", deadline, buf)  # cleared before the redraw
        buf = read_until(master, b"\x1b[?2026l", deadline, buf)
        buf += drain(master, 0.5)  # let any duplicate SIGWINCH frame finish
        rows = render_rows(latest_frame(buf))
        assert len(rows) == 10  # 11 terminal rows minus the status line
        assert rows[0].count("▀".encode()) == 30  # one ▀ cell per column

        os.write(master, b"q")
        assert proc.wait(timeout=10) == 0
    finally:
        stop_viewer(proc, master, slave)


def test_resize_is_ignored_with_explicit_size(tmp):
    db, proc, master, slave = make_db(tmp), None, None, None
    proc, master, slave = spawn_viewer(db, 9, 20, "--size", "20x8")
    try:
        deadline = time.monotonic() + 30
        buf = read_until(master, b" fps", deadline)

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 11, 30, 0, 0))
        os.kill(proc.pid, signal.SIGWINCH)
        buf += drain(master, 2.0)  # a redraw is allowed, a size change is not
        rows = render_rows(latest_frame(buf))
        assert rows[0].count("▀".encode()) == 20  # still the explicit size

        os.write(master, b"q")
        assert proc.wait(timeout=10) == 0
    finally:
        stop_viewer(proc, master, slave)


# --- terminal restored on SIGTERM / SIGHUP ---------------------------------------


def test_signal_restores_terminal_and_exits_128_plus_signal(tmp):
    for sig, code in [(signal.SIGTERM, 143), (signal.SIGHUP, 129)]:
        wad = write_wad(tmp / f"map-{code}.wad", corridor_map_lumps())
        db = tmp / f"map-{code}.duckdb"
        assert run_import(wad, db).returncode == 0
        proc, master, slave = spawn_viewer(db, rows=9, cols=20)
        try:
            termios_before = termios.tcgetattr(slave)
            deadline = time.monotonic() + 30
            buf = read_until(master, b" fps", deadline)
            after_signal_at = len(buf)

            os.kill(proc.pid, sig)
            buf = read_until(master, b"\x1b[?1049l", deadline, buf)
            assert proc.wait(timeout=10) == code
            assert b"\x1b[?25h" in buf[after_signal_at:]  # cursor shown again
            assert b"\x1b[?1049l" in buf[after_signal_at:]  # alternate screen left

            termios_after = termios.tcgetattr(slave)
            assert termios_after[3] & termios.ECHO  # echoing back on
            assert termios_after[3] & termios.ICANON  # canonical mode back on
        finally:
            stop_viewer(proc, master, slave)


# --- EOF on stdin -----------------------------------------------------------------


def test_viewer_quits_cleanly_on_stdin_eof(tmp):
    db = make_db(tmp)
    master, slave = os.openpty()
    try:
        con = duckdb.connect(str(db), read_only=True)
        try:
            con.execute(files("sqldoom").joinpath("sql/render.sql").read_text())
            reads: list[int] = []

            def eof_reader(fd: int):
                reads.append(fd)
                return None  # what os.read gives at end of file

            def timeout(signum, frame):
                raise TimeoutError("run_viewer did not exit on EOF")

            old_handler = signal.signal(signal.SIGALRM, timeout)
            signal.setitimer(signal.ITIMER_REAL, 30)
            try:
                os.write(master, b"x")  # make stdin readable so the loop reads it
                code = run_viewer(con, "E1M1", (0.0, 0.0, 0.0), 20, 8,
                                  fd=slave, reader=eof_reader)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
            assert code == 0
            assert reads == [slave]  # one read saw EOF, then the loop stopped
        finally:
            con.close()
    finally:
        os.close(master)
        os.close(slave)
