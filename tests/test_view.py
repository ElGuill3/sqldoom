"""T3: sqldoom-view, the interactive terminal viewer (D9).

Python owns only I/O and the camera: key parsing (parse_keys) and camera updates
(step) are pure and unit-tested here; the pty test drives the whole viewer end to
end, always with deadlines so it can never hang the suite.
"""

import math
import os
import pty
import select
import subprocess
import sys
import time

from conftest import run_import
from sqldoom.view import parse_keys, step
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


def start_viewer(db, *args):
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sqldoom.view", str(db), "E1M1", *map(str, args)],
        stdin=slave, stdout=slave, stderr=slave,
    )
    os.close(slave)
    return proc, master


def read_until(master: int, needle: bytes, deadline: float) -> bytes:
    """Read from the pty master until `needle` appears, EOF, or the deadline."""
    buf = b""
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


def test_requires_a_tty(tmp):
    db = make_db(tmp)

    result = subprocess.run(
        [sys.executable, "-m", "sqldoom.view", str(db), "E1M1"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: sqldoom-view needs an interactive terminal\n"
