"""T2: each map's binary lumps become relational tables keyed by (map, id)."""

import pytest

from conftest import query, run_import
from wadbuilder import sample_map_lumps, write_wad


@pytest.fixture
def db(tmp):
    wad = write_wad(tmp / "m.wad", [("PLAYPAL", b"\0" * 3), *sample_map_lumps("E1M1"), *sample_map_lumps("E1M2")])
    path = tmp / "m.duckdb"
    result = run_import(wad, path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == f"imported 23 lumps and 2 maps into {path}\n"
    return path


def test_maps(db):
    assert query(db, "SELECT name, lump_idx FROM maps ORDER BY name") == [("E1M1", 1), ("E1M2", 12)]


def test_things(db):
    assert query(db, "SELECT * FROM things WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 64, 96, 90, 1, 7),
        ("E1M1", 1, -32, 16, 270, 3004, 4),
    ]


def test_vertexes(db):
    assert query(db, "SELECT * FROM vertexes WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 0, 0),
        ("E1M1", 1, 128, 0),
        ("E1M1", 2, 128, -128),
    ]


def test_linedefs_use_null_for_missing_back_side(db):
    assert query(db, "SELECT * FROM linedefs WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 0, 1, 1, 0, 0, 0, None),
        ("E1M1", 1, 1, 2, 4, 11, 3, 0, 1),
    ]


def test_sidedefs_use_null_for_no_texture(db):
    assert query(db, "SELECT * FROM sidedefs WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 0, 0, None, None, "STARTAN3", 0),
        ("E1M1", 1, 16, -8, "BROWN1", "STEP6", None, 1),
    ]


def test_sectors(db):
    assert query(db, "SELECT * FROM sectors WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 0, 72, "FLOOR4_8", "CEIL3_5", 160, 0, 0),
        ("E1M1", 1, -16, 128, "NUKAGE1", "F_SKY1", 255, 7, 2),
    ]


def test_segs_keep_unsigned_angle(db):
    assert query(db, "SELECT * FROM segs WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 0, 1, 0, 0, 0, 0),
        ("E1M1", 1, 1, 2, 49152, 1, 1, 32),
    ]


def test_ssectors(db):
    assert query(db, "SELECT * FROM ssectors WHERE map = 'E1M1' ORDER BY id") == [
        ("E1M1", 0, 1, 0),
        ("E1M1", 1, 1, 1),
    ]


def test_nodes_split_child_flag_from_index(db):
    assert query(db, "SELECT * FROM nodes WHERE map = 'E1M1'") == [
        ("E1M1", 0, 128, 0, 0, -128, 0, -128, 128, 256, 0, -128, 0, 128, 0, True, 1, True),
    ]


def test_every_map_is_loaded(db):
    assert query(db, "SELECT map, count(*) FROM linedefs GROUP BY map ORDER BY map") == [("E1M1", 2), ("E1M2", 2)]


def test_rejects_map_lump_with_partial_record(tmp):
    lumps = sample_map_lumps("E1M1")
    lumps[4] = ("VERTEXES", b"\0\0\0\0\0")  # 5 bytes: not a multiple of the 4-byte record
    wad = write_wad(tmp / "bad.wad", lumps)
    path = tmp / "bad.duckdb"

    result = run_import(wad, path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: corrupt WAD file: {wad}\n"
    assert not path.exists()
