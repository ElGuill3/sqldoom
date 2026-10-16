"""T2 (textures): the G4-G8 renderer designs, checked against Python implementations.

The corridor from test_render.py with the test palette (palette[i] = (i, 255-i, i//2))
and the test pattern texel(u, v) = (u + 4v) % 256 makes every rendered pixel
decodable: with the identity colormap a pixel shows palette[texel] directly (G4/G5
texture mapping), and with the revealing colormap (level k maps every entry to k) its
palette index is the lighting level itself (G7). The G3 failure path is tested end to
end in test_render_cli.py and test_view.py and unit-level here.
"""

import math
from pathlib import Path

import duckdb
import pytest

from conftest import render_query, run_import
from sqldoom.common import NoTextures, load_render
from test_render import HEIGHT, WIDTH, corridor_map_lumps, grid, make_db, tex
from wadbuilder import graphics_lumps, map_lumps, reveal_colormap, write_wad


def reference_columns(width: int = WIDTH, height: int = HEIGHT):
    """Every pixel of the corridor seen from (0, 0) at angle 0, computed in Python from
    G4/G5: the east wall (x = 128, linedef (128, 128) -> (128, -128), room on its front
    side) is hit by every column at t = 128 with dist = 128 + 128*u_c; rows 0-2 show
    the near ceiling flat (h = 72), rows 3-7 the middle wall, rows 8-9 the near floor
    flat (h = 0). Identity colormap, so pixel RGB = palette[texel]."""
    f = width / 2
    # D6 span boundaries: row y belongs to the ceiling span while y + 0.5 < cy_n, to
    # the wall while y + 0.5 < fy_n, and to the floor after that.
    cyn = height / 2 - (72 - 41) * f / 128
    fyn = height / 2 - (0 - 41) * f / 128
    columns = []
    for c in range(width):
        u_c = (c + 0.5 - width / 2) / f
        dist = 128 + 128 * u_c
        u_w = int(math.floor(dist)) % 64  # G4 u (no x_offset)
        column = []
        for y in range(height):
            if y + 0.5 < cyn:
                d = (72 - 41) * f / (height / 2 - y - 0.5)  # G5 ceiling depth
                column.append(("ceiling", tex(int(math.floor(d)) % 64,
                                              int(math.floor(u_c * d)) % 64)))
            elif y + 0.5 < fyn:
                z = 41 - (y + 0.5 - height / 2) * 128 / f  # G4 world height at row y
                column.append(("wall", tex(u_w, int(math.floor(72 - z)) % 64)))
            else:
                d = (0 - 41) * f / (height / 2 - y - 0.5)  # G5 floor depth
                column.append(("floor", tex(int(math.floor(d)) % 64,
                                            int(math.floor(u_c * d)) % 64)))
        columns.append(column)
    return columns


def fallback_map_lumps(wall: str = "-", flat: str = "-"):
    """The corridor with the same texture name on every wall and both flats, for the
    G4 missing-texture fallback ('-' imports as NULL, texel 0)."""
    return map_lumps(
        "E1M1",
        vertexes=[(128, 128), (128, -128), (-256, -128), (-256, 128)],
        linedefs=[(0, 1, 0, None), (1, 2, 1, None), (2, 3, 2, None), (3, 0, 3, None)],
        sidedefs=[(0, wall, "-", "-")] * 4,
        sectors=[(0, 72, flat, flat, 160)],
        things=[(0, 0, 0, 1)],
        segs=[(0, 1, 0, 0, 0, 0)],
        ssectors=[(1, 0)],
    ) + graphics_lumps([("WALLE", 64, 64)], ["FLATW"])


def open_map_lumps():
    """The corridor without its east wall: every column ray exits the sector (|u| < 1,
    so the side walls at y = +/-128 are only reached at t > 128), leaving every pixel
    to the D6 'empty' fallback."""
    return map_lumps(
        "E1M1",
        vertexes=[(128, -128), (-256, -128), (-256, 128), (128, 128)],
        linedefs=[(0, 1, 0, None), (1, 2, 1, None), (2, 3, 2, None)],  # no east wall
        sidedefs=[(0, "WALLS", "-", "-"), (0, "WALLW", "-", "-"), (0, "WALLN", "-", "-")],
        sectors=[(0, 72, "FLATW", "CEILW", 160)],
        things=[(0, 0, 0, 1)],
        segs=[(0, 1, 0, 0, 0, 0)],
        ssectors=[(1, 0)],
    ) + graphics_lumps([("WALLS", 64, 64), ("WALLW", 64, 64), ("WALLN", 64, 64)],
                       ["FLATW", "CEILW"])


# --- G4/G5: textured walls and flats ------------------------------------------


def test_every_corridor_pixel_matches_the_g4_g5_formulas(tmp):
    db = make_db(tmp, corridor_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    for x, column in enumerate(reference_columns()):
        for y, (surface, rgb) in enumerate(column):
            assert pixels[y][x][2:] == (surface, *rgb), (x, y)


def test_wall_texture_offsets(tmp):
    """G4: u = floor(dist + x_offset) mod width and v = floor(top - z + y_offset)
    mod height, both sidedef offsets."""
    db = make_db(tmp, corridor_map_lumps(east_xoff=96, east_yoff=32))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # Column 8: dist = 136 -> u = floor(136 + 96) mod 64 = 40; row 5: 72 - z = 39 ->
    # v = floor(39 + 32) mod 64 = 7.
    assert pixels[5][8][3:] == tex(40, 7)


def test_middle_wall_unpegged_bottom_anchors_to_the_near_floor(tmp):
    """G4 flag 16 on a middle wall: top = near_floor + texheight = 0 + 64."""
    db = make_db(tmp, corridor_map_lumps(east_flags=16))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # v = floor(64 - z) mod 64: row 3 has z = 65 -> 63, row 5 z = 33 -> 31, and row 7
    # has z = 1 -> 63 again (the rows are one full texture apart).
    assert pixels[3][8][3:] == tex(8, 63)
    assert pixels[5][8][3:] == tex(8, 31)
    assert pixels[7][8][3:] == tex(8, 63)


def test_upper_wall_hangs_from_the_far_ceiling_unless_flag_8(tmp):
    """G4: default top = far_ceiling + texheight; flag 8 (upper unpegged) pins the
    texture to the near ceiling. far_ceiling 56 puts the upper wall at rows 1-2."""
    db = make_db(tmp / "pegged", corridor_map_lumps(step=True, far_ceiling=56))
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # The split line is seen from its back side at t = 64: dist = (1 - s) * 256 = 108
    # -> u = 44. v = floor(56 + 64 - z) mod 64: row 1 has z = 69 -> 51, row 2 -> 59.
    assert pixels[1][5][2] == "upper"
    assert pixels[1][5][3:] == tex(44, 51)
    assert pixels[2][5][3:] == tex(44, 59)

    db = make_db(tmp / "unpegged", corridor_map_lumps(step=True, far_ceiling=56, split_flags=8))
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # top = near_ceiling = 72: v = floor(72 - z) mod 64: row 1 -> 3, row 2 -> 11.
    assert pixels[1][5][3:] == tex(44, 3)
    assert pixels[2][5][3:] == tex(44, 11)


def test_lower_wall_pegs_to_the_far_floor_unless_flag_16(tmp):
    """G4: default top = far_floor = 24; flag 16 (lower unpegged) pins it to the near
    ceiling."""
    db = make_db(tmp / "pegged", corridor_map_lumps(step=True))
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # Rows 7-9 lower at t = 64, u = 44: v = floor(24 - z), z = 21 at row 7 -> 3.
    assert pixels[7][5][3:] == tex(44, 3)

    db = make_db(tmp / "unpegged", corridor_map_lumps(step=True, split_flags=16))
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # top = near_ceiling = 72: v = floor(72 - 21) = 51.
    assert pixels[7][5][3:] == tex(44, 51)


def test_missing_wall_and_flat_textures_draw_texel_zero(tmp):
    """G4/G5: a missing texture name ('-' imports as NULL) draws palette index 0."""
    db = make_db(tmp / "missing", fallback_map_lumps())
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    assert {cell[3:] for row in pixels for cell in row} == {(0, 255, 0)}  # palette[0]

    # Only the wall missing: the flats keep their test pattern.
    db = make_db(tmp / "flat", fallback_map_lumps(flat="FLATW"))
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    assert pixels[5][8][3:] == (0, 255, 0)
    assert pixels[0][8][3:] == tex(55, 3)


def test_nothing_drawn_is_empty_black(tmp):
    db = make_db(tmp, open_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    for y in range(HEIGHT):
        for x in range(WIDTH):
            assert pixels[y][x] == (x, y, "empty", 0, 0, 0)


# --- G6: the sky ---------------------------------------------------------------


def test_sky_draws_the_sky1_texture_by_ray_angle_and_screen_row(tmp):
    db = make_db(tmp, corridor_map_lumps(sky=True))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    for y in range(3):
        for x in range(WIDTH):
            assert pixels[y][x][2] == "sky"
    # G6: u = floor(ray_angle_deg * 1024/360) mod 256 with the ray angle atan2(dy, dx):
    # column 8 (u_c = 1/16) has ray angle -3.5763... degrees, so u = floor(-10.16)
    # mod 256 = 245; v = floor((y + 0.5) * 200/height) mod 128 is 10, 30, 50 for the
    # three sky rows at height 10.
    assert pixels[0][8][3:] == tex(245, 10)
    assert pixels[1][8][3:] == tex(245, 30)
    # Column 15 (ray angle -43.097... degrees): u = floor(-122.58) mod 256 = 133.
    assert pixels[2][15][3:] == tex(133, 50)
    # The wall and floor below the sky keep their textures.
    assert pixels[5][8][3:] == tex(8, 39)
    assert pixels[8][8][3:] == tex(29, 5)


def test_sky_rotates_with_the_camera(tmp):
    db = make_db(tmp, corridor_map_lumps(sky=True))

    # SKY1 wraps every 90 degrees (256 texels at 1024/360 per degree), so a 45 degree
    # camera is a half-texture shift. Column 8's ray angle becomes 45 - 3.576... =
    # 41.414 degrees, so u = floor(41.414 * 1024/360) = 117.
    east = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    diag = grid(db, f"render_pixels('E1M1', 0, 0, 45, {WIDTH}, {HEIGHT})")
    assert diag[0][8][2] == "sky"
    assert diag[0][8][3:] == tex(117, 10)
    assert east[0][8][3:] != diag[0][8][3:]


# --- G7: Doom COLORMAP lighting -------------------------------------------------


def test_wall_lighting_level(tmp):
    """G7 with the revealing colormap: the pixel's palette index is its level."""
    db = make_db(tmp, corridor_map_lumps(colormap=reveal_colormap()))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # East wall (ex = 0, vertical line): light = 160 >> 4 = 10, +1 contrast = 11,
    # startmap = (15 - 11) * 4 = 16, scale = least(47, floor(2560/128)) = 20,
    # level = 16 - 10 = 6.
    assert [pixels[y][8][3] for y in range(3, 8)] == [6] * 5
    # Floor row 8, column 8: light 10 (no contrast on flats), startmap 20,
    # d = 93.714..., j = floor(93.714/16) = 5, level = 20 - 80 // 6 = 7.
    assert pixels[8][8][3] == 7
    # Ceiling row 0: d = 55.11, j = 3, level = 20 - 80 // 4 = 0.
    assert pixels[0][8][3] == 0


def test_horizontal_walls_get_minus_one_contrast(tmp):
    db = make_db(tmp, corridor_map_lumps(colormap=reveal_colormap()))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 90, {WIDTH}, {HEIGHT})")
    # The north wall (ey = 0, horizontal line): light 10 - 1 = 9, startmap 24,
    # scale 20, level 24 - 10 = 14.
    assert [pixels[y][8][3] for y in range(3, 8)] == [14] * 5


def test_lighting_is_clamped_to_levels_0_31_and_light_0_15(tmp):
    dark = make_db(tmp / "dark", corridor_map_lumps(light=0, colormap=reveal_colormap()))
    pixels = grid(dark, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # Wall: light 0, +1 contrast = 1, startmap 56, level 56 - 10 = 46 -> clamped 31.
    assert pixels[5][8][3] == 31
    # Floor: light 0, startmap 60, j = 5, level 60 - 13 = 47 -> clamped 31.
    assert pixels[8][8][3] == 31

    bright = make_db(tmp / "bright", corridor_map_lumps(light=255, colormap=reveal_colormap()))
    pixels = grid(bright, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # Wall: light 15, +1 contrast clamps to 15, startmap 0, level 0.
    assert pixels[5][8][3] == 0
    # Floor: light 15, startmap 0, level 0 everywhere.
    assert pixels[9][8][3] == 0


# --- G3: failing cleanly on pre-T1 databases ------------------------------------


def test_load_render_rejects_a_missing_textures_table(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "pre_t1.duckdb"))
    try:
        con.execute("CREATE TABLE maps (name VARCHAR PRIMARY KEY, lump_idx INTEGER)")
        with pytest.raises(NoTextures) as exc:
            load_render(con)
        assert str(exc.value) == "database has no textures: re-run sqldoom-import --force"
    finally:
        con.close()


def test_load_render_rejects_empty_textures(tmp):
    """G3: a WAD without graphics lumps imports fine, and rendering it must fail."""
    db = make_db(tmp, corridor_map_lumps(graphics=False))
    con = duckdb.connect(str(db))
    try:
        assert con.execute("SELECT count(*) FROM textures").fetchone() == (0,)
        with pytest.raises(NoTextures) as exc:
            load_render(con)
        assert str(exc.value) == "database has no textures: re-run sqldoom-import --force"
    finally:
        con.close()
