"""T3: sqldoom-view, the interactive terminal viewer (D9).

Python owns only I/O and the camera: key parsing (parse_keys) and camera updates
(step) are pure and unit-tested here; the pty test drives the whole viewer end to
end, always with deadlines so it can never hang the suite.
"""

import fcntl
import math
import os
import pty
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
from sqldoom.view import parse_keys, run_viewer, step
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


# --- step ----------------------------------------------------------------------


def test_step_moves_forward_along_the_facing_direction():
    assert all(math.isclose(a, b, abs_tol=1e-9)
               for a, b in zip(step((0, 0, 0), "forward"), (16, 0, 0)))
    assert all(math.isclose(a, b, abs_tol=1e-9)
               for a, b in zip(step((0, 0, 90), "forward"), (0, 16, 90)))


def test_step_moves_back():
    assert all(math.isclose(a, b, abs_tol=1e-9)
               for a, b in zip(step((10, 0, 0), "back"), (-6, 0, 0)))
    assert all(math.isclose(a, b, abs_tol=1e-9)
               for a, b in zip(step((0, 0, 90), "back"), (0, -16, 90)))


def test_step_turns_and_wraps_the_angle():
    assert step((0, 0, 0), "left")[2] == 7.5
    assert step((0, 0, 352.5), "left")[2] == 0.0  # 360 wraps to 0
    assert step((0, 0, 0), "right")[2] == 352.5
    assert step((0, 0, 7.5), "right")[2] == 0.0


def test_step_leaves_the_camera_unchanged_for_unknown_actions():
    assert step((3, 4, 5), "quit") == (3, 4, 5)


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

        os.write(master, b"w")
        buf += read_until(master, b"x=16 y=0", deadline)  # the moved camera
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
