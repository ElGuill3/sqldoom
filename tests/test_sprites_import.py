"""P1-P2: picture sprites retain transparency and spawn-state metadata."""

import struct

import pytest

from conftest import query, run_import
from wadbuilder import picture, sample_map_lumps, write_wad


EXPECTED_TYPES = [
    (5, "BKEY", "A", False), (6, "YKEY", "A", False),
    (8, "BPAK", "A", False), (9, "SPOS", "A", False),
    (10, "PLAY", "W", False), (12, "PLAY", "W", False),
    (13, "RKEY", "A", False), (15, "PLAY", "N", False),
    (24, "POL5", "A", False), (34, "CAND", "A", True),
    (35, "CBRA", "A", True), (46, "TRED", "A", True),
    (48, "ELEC", "A", False), (58, "SARG", "A", False),
    (2001, "SHOT", "A", False), (2002, "MGUN", "A", False),
    (2003, "LAUN", "A", False), (2005, "CSAW", "A", False),
    (2007, "CLIP", "A", False), (2008, "SHEL", "A", False),
    (2010, "ROCK", "A", False), (2011, "STIM", "A", False),
    (2012, "MEDI", "A", False), (2013, "SOUL", "A", True),
    (2014, "BON1", "A", False), (2015, "BON2", "A", False),
    (2018, "ARM1", "A", False), (2019, "ARM2", "A", False),
    (2024, "PINS", "A", True), (2025, "SUIT", "A", True),
    (2026, "PMAP", "A", True), (2028, "COLU", "A", True),
    (2035, "BAR1", "A", False), (2045, "PVIS", "A", True),
    (2046, "BROK", "A", False), (2048, "AMMO", "A", False),
    (2049, "SBOX", "A", False), (3001, "TROO", "A", False),
    (3002, "SARG", "A", False), (3003, "BOSS", "A", False),
    (3004, "POSS", "A", False),
]


def test_sprite_columns_offsets_and_inner_markers(tmp):
    sprite = picture(3, 4, {0: [(0, [0]), (2, [20, 21])], 2: [(1, [30, 31])]},
                     leftoffset=-2, topoffset=-3)
    lumps = [("OUTSIDE", b"not a picture"), ("S_START", b""),
             ("S1_START", b""), ("TESTA0", sprite), ("S1_END", b""), ("S_END", b"")]
    path = tmp / "sprites.duckdb"

    result = run_import(write_wad(tmp / "sprites.wad", lumps), path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"imported 6 lumps and 0 maps into {path}\n"
    assert result.stderr == ""
    assert query(path, "SELECT * FROM sprites") == [("TESTA0", 3, 4, -2, -3)]
    assert query(path, "SELECT * FROM sprite_columns ORDER BY u") == [
        ("TESTA0", 0, [0, None, 20, 21]),
        ("TESTA0", 1, [None, None, None, None]),
        ("TESTA0", 2, [None, 30, 31, None]),
    ]


def test_no_sprite_namespace_still_imports_maps_and_thing_types(tmp):
    path = tmp / "plain.duckdb"
    lumps = sample_map_lumps()

    result = run_import(write_wad(tmp / "plain.wad", lumps), path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"imported {len(lumps)} lumps and 1 maps into {path}\n"
    assert result.stderr == ""
    assert query(path, "SELECT name FROM maps") == [("E1M1",)]
    assert query(path, "SELECT * FROM sprites") == []
    assert query(path, "SELECT * FROM sprite_columns") == []
    assert query(path, "SELECT count(*) FROM thing_types") == [(41,)]
    assert query(path, "SELECT * FROM thing_types ORDER BY type") == EXPECTED_TYPES


@pytest.mark.parametrize("data", [
    b"short", struct.pack("<4h", -1, 4, 0, 0), struct.pack("<4h", 1, -1, 0, 0),
    struct.pack("<4hi", 1, 4, 0, 0, 1000),
    struct.pack("<4hi", 1, 4, 0, 0, 12) + b"\x00\x03\x00\x01",
    picture(1, 4, {0: [(0, [1])]})[:-1],
])
def test_malformed_sprite_leaves_no_database(tmp, data):
    wad = write_wad(tmp / "bad.wad", [("S_START", b""), ("BADA0", data), ("S_END", b"")])
    path = tmp / "bad.duckdb"

    result = run_import(wad, path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: corrupt WAD file: {wad}\n"
    assert not path.exists()
    assert not path.with_name(path.name + ".partial").exists()
