"""T1: the automap is computed in SQL and printed as ASCII.

The test room is 180x160 world units. At --size 21x11 in ASCII mode (pixel aspect 2,
1-pixel margin) the scale is exactly 0.1 px/unit horizontally and 0.05 vertically,
so the room's walls land on columns 1..19 and rows 1..9, the split line on column 10,
and the player start (40, 80) on column 5, row 5.
"""

import duckdb
import pytest

from conftest import query, run_automap, run_import
from wadbuilder import room_map_lumps, write_wad


def make_db(tmp, **heights):
    wad = write_wad(tmp / "room.wad", room_map_lumps("E1M1", **heights))
    db = tmp / "room.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def expected_room(split_char: str) -> str:
    rows = [[" "] * 21 for _ in range(11)]
    for x in range(1, 20):
        rows[1][x] = rows[9][x] = "#"
    for y in range(2, 9):
        rows[y][1] = rows[y][19] = "#"
        rows[y][10] = split_char
    rows[5][5] = "@"
    return "".join("".join(r) + "\n" for r in rows)


@pytest.mark.parametrize(
    "heights, split_char",
    [
        ({"right_floor": 24, "right_ceiling": 128}, "+"),  # floor height changes
        ({"right_floor": 0, "right_ceiling": 96}, "="),  # only the ceiling changes
        ({"right_floor": 0, "right_ceiling": 128}, "."),  # nothing changes
    ],
)
def test_draws_walls_split_line_and_player(tmp, heights, split_char):
    db = make_db(tmp, **heights)

    result = run_automap(db, "E1M1", "--size", "21x11")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == expected_room(split_char)


def test_map_name_is_case_insensitive(tmp):
    db = make_db(tmp, right_floor=24)

    result = run_automap(db, "e1m1", "--size", "21x11")

    assert result.returncode == 0
    assert result.stdout == expected_room("+")


def test_default_ascii_size_is_120x60(tmp):
    db = make_db(tmp)

    lines = run_automap(db, "E1M1").stdout.splitlines()

    assert len(lines) == 60
    assert {len(line) for line in lines} == {120}


def test_unknown_map(tmp):
    db = make_db(tmp)
    before = query(db, "SELECT count(*) FROM linedefs")

    result = run_automap(db, "E9M9")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: map not found: E9M9\n"
    assert query(db, "SELECT count(*) FROM linedefs") == before


def test_missing_database(tmp):
    missing = tmp / "nope.duckdb"

    result = run_automap(missing, "E1M1")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: file not found: {missing}\n"
    assert not missing.exists()


@pytest.mark.parametrize("size", ["3x3", "21", "axb", "21x7", "4097x8", "8x5000"])
def test_rejects_bad_size(tmp, size):
    db = make_db(tmp)

    result = run_automap(db, "E1M1", "--size", size)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith(
        f"sqldoom-automap: error: argument --size: expected WIDTHxHEIGHT, both between 8 and 4096, got '{size}'\n"
    )


def test_not_a_sqldoom_database(tmp):
    junk = tmp / "junk.duckdb"
    junk.write_bytes(b"junk bytes, definitely not a DuckDB file\n" * 4)
    before = junk.read_bytes()

    result = run_automap(junk, "E1M1")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: not a sqldoom database: {junk}\n"
    assert junk.read_bytes() == before  # opened read-only, byte-identical


def test_empty_duckdb_file_has_no_maps(tmp):
    empty = tmp / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    before = empty.read_bytes()

    result = run_automap(empty, "E1M1", "--size", "21x11")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: not a sqldoom database: {empty}\n"
    assert empty.read_bytes() == before  # opened read-only, byte-identical
