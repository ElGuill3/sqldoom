"""T1: PLAYPAL, COLORMAP, composite wall textures and flats become tables."""

import struct

import pytest

from conftest import query, run_import
from wadbuilder import (
    FLAT_SIZE,
    colormap,
    flat,
    picture,
    playpal,
    pnames,
    sample_map_lumps,
    texture_defs,
    write_wad,
)

# WALL1: 2x3. Column 0 is one 3-tall post; column 1 has two posts (topdelta 0 and 2),
# leaving its middle texel transparent.
PATCH_A = picture(2, 3, {0: [(0, [10, 11, 12])], 1: [(0, [13]), (2, [14])]})
# WALL2: 2x2, column 1 shifted down by one (topdelta 1).
PATCH_B = picture(2, 2, {0: [(0, [20, 21])], 1: [(1, [22])]})
# WALL3/WALL4: plain 2x2 patches for the clipping test.
PATCH_C = picture(2, 2, {0: [(0, [40, 41])], 1: [(0, [42, 43])]})
PATCH_D = picture(2, 2, {0: [(0, [50, 51])], 1: [(0, [52, 53])]})

# TEX1 (4x3): WALL1 at (0,0), then WALL2 at (1,1) overwrites part of it. Texels no
# patch covers stay palette index 0. TEX2 (2x2): WALL3 at (1,1) and WALL4 at (-1,0),
# so both patches are clipped on every side.
GRAPHICS = [
    ("PLAYPAL", playpal()),
    ("COLORMAP", colormap()),
    ("P_START", b""),
    ("WALL1", PATCH_A),
    ("WALL2", PATCH_B),
    ("WALL3", PATCH_C),
    ("WALL4", PATCH_D),
    ("P_END", b""),
    ("PNAMES", pnames(["WALL1", "WALL2", "WALL3", "WALL4"])),
    ("TEXTURE1", texture_defs([
        ("TEX1", 4, 3, [(0, 0, 0), (1, 1, 1)]),
        ("TEX2", 2, 2, [(1, 1, 2), (-1, 0, 3)]),
    ])),
    ("F_START", b""),
    ("F1_START", b""),
    ("FLOOR4_8", flat({7 * 64 + 5: 99})),
    ("NUKAGE1", flat({0: 7})),
    ("F1_END", b""),
    ("F_END", b""),
    ("MUSIC", b"\x01" * FLAT_SIZE),  # flat-sized, but not between F_START and F_END
]


@pytest.fixture
def db(tmp):
    path = tmp / "g.duckdb"
    result = run_import(write_wad(tmp / "g.wad", GRAPHICS), path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return path


def test_success_line_is_unchanged(tmp):
    path = tmp / "g.duckdb"

    result = run_import(write_wad(tmp / "g.wad", GRAPHICS), path)

    assert result.returncode == 0
    assert result.stdout == f"imported {len(GRAPHICS)} lumps and 0 maps into {path}\n"


def test_palette(db):
    assert query(db, "SELECT idx, r, g, b FROM palette WHERE idx IN (0, 1, 255) ORDER BY idx") == [
        (0, 0, 255, 0),
        (1, 1, 254, 0),
        (255, 255, 0, 127),
    ]


def test_palette_uses_palette_zero_of_a_multi_palette_playpal(tmp):
    second = [(9, 9, 9)] * 256
    path = tmp / "pal.duckdb"
    wad = write_wad(tmp / "pal.wad", [("PLAYPAL", playpal([[(i, 200, 100) for i in range(256)], second]))])

    result = run_import(wad, path)

    assert result.returncode == 0, result.stderr
    assert query(path, "SELECT idx, r, g, b FROM palette WHERE idx IN (0, 5)") == [(0, 0, 200, 100), (5, 5, 200, 100)]


def test_colormaps(db):
    assert [level for (level,) in query(db, "SELECT level FROM colormaps ORDER BY level")] == list(range(34))
    assert list(query(db, "SELECT colors FROM colormaps WHERE level = 0")[0][0]) == list(range(256))
    assert list(query(db, "SELECT colors FROM colormaps WHERE level = 33")[0][0]) == [(c + 33) % 256 for c in range(256)]


def test_textures(db):
    assert query(db, "SELECT name, width, height FROM textures ORDER BY name") == [("TEX1", 4, 3), ("TEX2", 2, 2)]


def test_texture_columns(db):
    assert query(db, "SELECT texture, u, pixels FROM texture_columns WHERE texture = 'TEX1' ORDER BY u") == [
        ("TEX1", 0, [10, 11, 12]),
        ("TEX1", 1, [13, 20, 21]),  # WALL2's (1,1) and (1,2) overwrote 0 and 14
        ("TEX1", 2, [0, 0, 22]),  # WALL2 column 1 starts at row 1 (topdelta 1)
        ("TEX1", 3, [0, 0, 0]),  # no patch reaches this column
    ]


def test_patches_are_clipped_to_the_texture(db):
    assert query(db, "SELECT u, pixels FROM texture_columns WHERE texture = 'TEX2' ORDER BY u") == [
        (0, [52, 53]),  # WALL4 shifted left by one: only its second column lands
        (1, [0, 40]),  # WALL3 shifted down by one: 40 lands on row 1, 41 falls off
    ]


def test_flats(db):
    assert query(db, "SELECT name, len(pixels) FROM flats ORDER BY name") == [
        ("FLOOR4_8", FLAT_SIZE),
        ("NUKAGE1", FLAT_SIZE),
    ]
    assert query(db, "SELECT pixels[7 * 64 + 5 + 1] FROM flats WHERE name = 'FLOOR4_8'") == [(99,)]
    assert query(db, "SELECT pixels[1] FROM flats WHERE name = 'NUKAGE1'") == [(7,)]


def test_wad_without_graphics_still_imports(tmp):
    path = tmp / "plain.duckdb"

    result = run_import(write_wad(tmp / "plain.wad", sample_map_lumps("E1M1")), path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert query(path, "SELECT name FROM maps") == [("E1M1",)]
    for table in ("palette", "colormaps", "textures", "texture_columns", "flats"):
        assert query(path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_texture2_is_loaded_too(tmp):
    lumps = [
        ("PNAMES", pnames(["WALL1"])),
        ("WALL1", PATCH_A),
        ("TEXTURE1", texture_defs([("TEX1", 2, 2, [(0, 0, 0)])])),
        ("TEXTURE2", texture_defs([("TEX2", 2, 2, [(0, 0, 0)])])),
    ]
    path = tmp / "t2.duckdb"

    result = run_import(write_wad(tmp / "t2.wad", lumps), path)

    assert result.returncode == 0, result.stderr
    assert query(path, "SELECT name FROM textures ORDER BY name") == [("TEX1",), ("TEX2",)]


def corrupt(tmp, lumps):
    wad = write_wad(tmp / "bad.wad", lumps)
    path = tmp / "bad.duckdb"

    result = run_import(wad, path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: corrupt WAD file: {wad}\n"
    assert not path.exists()


def test_rejects_texture_pnames_index_out_of_range(tmp):
    corrupt(tmp, [("PNAMES", pnames(["WALL1"])), ("WALL1", PATCH_A),
                  ("TEXTURE1", texture_defs([("TEX1", 2, 2, [(0, 0, 5)])]))])


def test_rejects_pnames_name_with_no_lump(tmp):
    corrupt(tmp, [("PNAMES", pnames(["NOPE"])),
                  ("TEXTURE1", texture_defs([("TEX1", 2, 2, [(0, 0, 0)])]))])


def test_rejects_texture_entry_offset_outside_the_lump(tmp):
    corrupt(tmp, [("PNAMES", pnames(["WALL1"])), ("TEXTURE1", struct.pack("<ii", 1, 1000))])


def test_rejects_patch_column_offset_outside_the_lump(tmp):
    bad_patch = bytearray(PATCH_A)
    bad_patch[8:12] = struct.pack("<i", 10 ** 6)
    corrupt(tmp, [("PNAMES", pnames(["WALL1"])), ("WALL1", bytes(bad_patch)),
                  ("TEXTURE1", texture_defs([("TEX1", 2, 3, [(0, 0, 0)])]))])


def test_rejects_texture1_without_pnames(tmp):
    corrupt(tmp, [("WALL1", PATCH_A), ("TEXTURE1", texture_defs([("TEX1", 2, 2, [(0, 0, 0)])]))])


def test_short_playpal_leaves_the_table_empty(tmp):
    """A PLAYPAL too short to hold one palette (old tests use 3-byte fillers) is not decodable,
    so the table stays empty instead of raising."""
    path = tmp / "short.duckdb"

    result = run_import(write_wad(tmp / "short.wad", [("PLAYPAL", b"\0" * 3)]), path)

    assert result.returncode == 0
    assert query(path, "SELECT count(*) FROM palette") == [(0,)]


def test_rejects_non_flat_lump_between_f_markers(tmp):
    corrupt(tmp, [("F_START", b""), ("JUNK", b"\0" * 100), ("F_END", b"")])
