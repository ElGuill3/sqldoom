"""sqldoom-bench: time the SQL renderer and the terminal separately, on any machine."""

import os
import pty
import re
import select
import subprocess
import sys
import time

import pytest

from conftest import run_import, run_module
from test_render import corridor_map_lumps
from wadbuilder import map_lumps, write_wad

NUMBER = r"\d+\.\d"


def make_db(tmp):
    wad = write_wad(tmp / "map.wad", corridor_map_lumps())  # player 1 starts at (0, 0, 0)
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def run_bench(*args):
    return run_module("sqldoom.bench", *args)


def test_report_without_a_terminal(tmp):
    db = make_db(tmp)

    result = run_bench(db, "e1m1", "--size", "16x8", "--frames", "3")

    assert result.returncode == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[0] == "map E1M1  size 16x8 (16x16 pixels)  frames 3"
    assert re.fullmatch(rf"load render\.sql     {NUMBER} ms", lines[1])
    assert re.fullmatch(rf"sql per frame       avg {NUMBER} ms  min {NUMBER}  max {NUMBER}  \({NUMBER} fps\)", lines[2])
    assert lines[3] == "terminal per frame  skipped (stdout is not a terminal)"
    assert re.fullmatch(rf"viewer estimate     {NUMBER} fps \(sql only\)", lines[4])
    assert re.fullmatch(rf"duckdb threads      \d+ \(cpu count \d+\)", lines[5])
    assert len(lines) == 6


def test_defaults_are_160x50_and_20_frames(tmp):
    db = make_db(tmp)

    result = run_bench(db, "E1M1")

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "map E1M1  size 160x50 (160x100 pixels)  frames 20"


@pytest.mark.parametrize("frames", ["0", "1001", "abc", "2.5"])
def test_rejects_bad_frame_counts(tmp, frames):
    db = make_db(tmp)

    result = run_bench(db, "E1M1", "--frames", frames)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith(
        f"sqldoom-bench: error: argument --frames: expected a whole number between 1 and 1000, got '{frames}'\n"
    )


def test_shares_the_renderer_errors(tmp):
    db = make_db(tmp)
    missing = tmp / "nope.duckdb"
    old = tmp / "old.duckdb"
    no_graphics = write_wad(tmp / "old.wad", map_lumps(
        "E1M1", vertexes=[(0, 0), (64, 0), (64, 64)], linedefs=[(0, 1, 0, None)],
        sidedefs=[0], sectors=[(0, 72)], things=[(16, 16, 1)],
    ))
    assert run_import(no_graphics, old).returncode == 0

    cases = [
        ((missing, "E1M1"), f"error: file not found: {missing}\n"),
        ((db, "E9M9"), "error: map not found: E9M9\n"),
        ((old, "E1M1"), "error: database has no textures: re-run sqldoom-import --force\n"),
    ]
    for args, message in cases:
        result = run_bench(*args)
        assert (result.returncode, result.stdout, result.stderr) == (1, "", message)


def test_measures_the_terminal_in_a_pty(tmp):
    db = make_db(tmp)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sqldoom.bench", str(db), "E1M1", "--size", "16x8", "--frames", "2"],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    )
    os.close(slave)
    output, deadline = b"", time.monotonic() + 30
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the child closed the terminal
                break
            if not chunk:
                break
            output += chunk
        elif proc.poll() is not None:
            break
    proc.wait(timeout=5)
    os.close(master)

    assert proc.returncode == 0
    text = output.decode("utf-8", "replace")
    enter, leave = text.find("\x1b[?1049h"), text.rfind("\x1b[?1049l")
    assert 0 <= enter < leave
    report = text[leave:].replace("\r\n", "\n")
    assert re.search(
        rf"terminal per frame  avg {NUMBER} ms  min {NUMBER}  max {NUMBER}  \(\d+ bytes\)\n", report
    )
    assert re.search(rf"viewer estimate     {NUMBER} fps\n", report)
