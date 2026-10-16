"""T1/T2: point_sector and the first-person renderer, computed entirely by SQL.

Both test maps are corridors y in [-128, 128] (and x in [-256, 128]), 16x10 pixels,
camera at (0, 0) looking east. D1 gives focal f = width/2 = 8 and D2 makes D = (1, -u),
so every column ray crosses x = t at parameter t: the east wall at x = 128 is hit at
t = 128 by all 16 columns (|u| <= 15.5/8 < 1, so the side walls at y = +/-128 are only
reached at t >= 136.5, behind it). With eye = 0 + 41 (D5) and y(z) = 5 - (z - eye)*8/t
(D6), the solid east wall spans cy = 5 - 31*8/128 = 3.0625 to fy = 5 + 41*8/128 = 7.5625:
a span [a, b) covers rows ceil(a - 0.5) .. ceil(b - 0.5) - 1, so every column shows
rows 0-2 ceiling, 3-7 wall, 8-9 floor. The step map puts a two-sided linedef at
x = 64 (t = 64) with the far sector raised to floor 24: fy_f = 5 + 17*8/64 = 7.125,
so the opening is [1.125, 7.125) and the second hit draws rows 1-2 ceiling, 3-5 wall
(y(24, 128) = 6.0625), 6 floor inside that window; the first hit draws row 0 ceiling
and rows 7-9 lower.
"""

import hashlib
import math

from conftest import render_query, run_import
from wadbuilder import map_lumps, write_wad

WIDTH, HEIGHT = 16, 10


def corridor_map_lumps(step: bool = False, player_at: tuple | None = (0, 0, 0)):
    """Corridor x in [-256, 128], y in [-128, 128]. The east wall (x = 128) is solid.

    With step, a two-sided linedef at x = 64 splits it: west sector (floor 0,
    ceiling 72, FLATW/CEILW), east sector (floor 24, ceiling 72, FLATE/CEILE).
    The player 1 start sits at player_at (x, y, angle in degrees), or nowhere.
    """
    vertexes = [(-256, -128), (128, -128), (128, 128), (-256, 128), (64, -128), (64, 128)]
    sectors = [(0, 72, "FLATW", "CEILW", 160)]
    things = [(*player_at, 1)] if player_at is not None else []
    segs, ssectors = [(0, 1, 0, 0, 0, 0)], [(1, 0)]
    if not step:
        linedefs = [
            (0, 1, 0, None),  # south wall
            (1, 2, 1, None),  # east wall
            (2, 3, 2, None),  # north wall
            (3, 0, 3, None),  # west wall
        ]
        sidedefs = [(0, "WALLS", "-", "-"), (0, "WALLE", "-", "-"),
                    (0, "WALLN", "-", "-"), (0, "WALLW", "-", "-")]
    else:
        sectors.append((24, 72, "FLATE", "CEILE", 160))
        segs.append((1, 2, 0, 2, 0, 0))  # subsector 1's first seg: the east wall
        ssectors.append((1, 1))
        linedefs = [
            (0, 1, 0, None),  # south wall
            (4, 5, 4, 5),  # split line: front (east) -> sector 1, back (west) -> sector 0
            (1, 2, 6, None),  # east wall
            (2, 3, 7, None),  # north wall
            (3, 0, 8, None),  # west wall
        ]
        sidedefs = [
            (0, "WALLS", "-", "-"), (0, "WALLE", "-", "-"),
            (0, "WALLN", "-", "-"), (0, "WALLW", "-", "-"),
            (1, "-", "UPPER1", "LOWER1"),  # east side of the split (faces away)
            (0, "-", "UPPER2", "LOWER2"),  # west side of the split: the near side
            (1, "WALLED", "-", "-"),  # east wall, front sidedef -> sector 1
            (0, "WALLN", "-", "-"), (0, "WALLW", "-", "-"),
        ]
    return map_lumps(
        "E1M1",
        vertexes=vertexes,
        linedefs=linedefs,
        sidedefs=sidedefs,
        sectors=sectors,
        things=things,
        segs=segs,
        ssectors=ssectors,
    )


def split_map_lumps():
    """A 128x128 rectangle split at x = 64 by one BSP node into two subsectors.

    Sector 0 is west of the split, sector 1 east; each subsector's first seg points
    at its own boundary linedef (front sidedef), so the D5 walk can tell them apart.
    """
    return map_lumps(
        "E1M1",
        vertexes=[(0, 0), (128, 0), (128, 128), (0, 128), (64, 0), (64, 128)],
        linedefs=[
            (3, 0, 0, None),  # west wall, front sidedef -> sector 0
            (4, 5, 1, 2),  # split line: front (east) -> sector 1, back (west) -> sector 0
            (1, 2, 3, None),  # east wall, front sidedef -> sector 1
            (0, 1, 4, None),  # south wall
            (2, 3, 5, None),  # north wall
        ],
        sidedefs=[0, (1, "-", "-", "-"), (0, "-", "-", "-"), 1, 0, 0],
        sectors=[(0, 72, "FLATW", "CEILW", 160), (24, 72, "FLATE", "CEILE", 160)],
        things=[(32, 64, 0, 1)],
        segs=[(3, 0, 0, 0, 0, 0), (1, 2, 0, 2, 0, 0)],
        ssectors=[(1, 0), (1, 1)],
        nodes=[(64, 0, 0, 128, 1, 0, True, True)],
    )


def make_db(tmp, lumps) -> object:
    wad = write_wad(tmp / "map.wad", lumps)
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def md5_color(name: str) -> tuple[int, int, int]:
    """D7 base RGB: md5(name) hex bytes 1-3, each mapped through 64 + byte % 160."""
    digest = hashlib.md5(name.encode("ascii")).hexdigest()
    return tuple(64 + int(digest[2 * i:2 * i + 2], 16) % 160 for i in range(3))


def shaded(color, light: int, depth: int | None = None) -> tuple[int, int, int]:
    """D7 shading: flats by light/255, walls by light/255 * greatest(0.35, 1 - depth/1600).

    Channels round half away from zero (DuckDB round); floor(x + 0.5) agrees for the
    non-negative values here, and at these light levels an exact .5 tie cannot occur.
    """
    factor = light / 255 * (1 if depth is None else max(0.35, 1 - depth / 1600))
    return tuple(min(255, max(0, math.floor(c * factor + 0.5))) for c in color)


def grid(db, macro: str) -> list[list[tuple]]:
    rows = render_query(db, f"SELECT x, y, surface, r, g, b FROM {macro} ORDER BY y, x")
    assert len(rows) == WIDTH * HEIGHT
    return [rows[y * WIDTH:(y + 1) * WIDTH] for y in range(HEIGHT)]


# --- T1: point_sector -------------------------------------------------------


def test_point_sector_walks_to_the_subsector_on_each_side_of_the_node(tmp):
    db = make_db(tmp, split_map_lumps())

    assert render_query(db, "SELECT sector FROM point_sector('E1M1', 32, 64)") == [(0,)]
    assert render_query(db, "SELECT sector FROM point_sector('E1M1', 96, 64)") == [(1,)]


def test_point_sector_returns_an_integer_column(tmp):
    db = make_db(tmp, split_map_lumps())

    assert render_query(db, "SELECT typeof(sector) FROM point_sector('E1M1', 32, 64)") == [("INTEGER",)]


def test_point_sector_without_nodes_is_subsector_zero(tmp):
    db = make_db(tmp, corridor_map_lumps())

    assert render_query(db, "SELECT sector FROM point_sector('E1M1', -100, 0)") == [(0,)]
    assert render_query(db, "SELECT sector FROM point_sector('E1M1', 100, -50)") == [(0,)]


# --- T2: render_pixels ------------------------------------------------------


def test_square_room_rows_are_ceiling_wall_floor_in_every_column(tmp):
    db = make_db(tmp, corridor_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    surfaces = [[cell[2] for cell in row] for row in pixels]
    for column in zip(*surfaces):
        assert list(column) == ["ceiling"] * 3 + ["wall"] * 5 + ["floor"] * 2


def test_square_room_pixel_colors_come_from_md5_and_shading(tmp):
    db = make_db(tmp, corridor_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    wall = shaded(md5_color("WALLE"), 160, 128)  # east wall, one-sided, t = 128
    ceiling = shaded(md5_color("CEILW"), 160)
    floor = shaded(md5_color("FLATW"), 160)
    assert pixels[5][5][3:] == wall
    assert pixels[0][0][3:] == ceiling
    assert pixels[9][WIDTH - 1][3:] == floor
    assert {cell[3:] for row in pixels for cell in row} == {wall, ceiling, floor}


def test_two_sided_step_draws_lower_but_no_upper(tmp):
    db = make_db(tmp, corridor_map_lumps(step=True))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    surfaces = [[cell[2] for cell in row] for row in pixels]
    for column in zip(*surfaces):
        assert list(column) == ["ceiling", "ceiling", "ceiling", "wall", "wall", "wall",
                                "floor", "lower", "lower", "lower"]
    assert {"upper", "sky", "empty"}.isdisjoint(cell[2] for row in pixels for cell in row)


def test_two_sided_step_pixel_colors_use_near_and_far_sector_data(tmp):
    db = make_db(tmp, corridor_map_lumps(step=True))

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    # Row 0: near (west) ceiling. Rows 1-2: far (east) ceiling, drawn by the t=128 hit.
    assert pixels[0][5][3:] == shaded(md5_color("CEILW"), 160)
    assert pixels[1][5][3:] == shaded(md5_color("CEILE"), 160)
    # Rows 3-5: the east wall's front (east) sidedef middle texture, near sector = east.
    assert pixels[4][5][3:] == shaded(md5_color("WALLED"), 160, 128)
    # Row 6: the east sector's floor peeking above its wall base.
    assert pixels[6][5][3:] == shaded(md5_color("FLATE"), 160)
    # Rows 7-9: lower texture of the west (back, near-side) sidedef at t = 64.
    assert pixels[8][5][3:] == shaded(md5_color("LOWER2"), 160, 64)


def test_render_rgb_is_row_major_rgb_matching_render_pixels(tmp):
    db = make_db(tmp, corridor_map_lumps())

    blob = render_query(db, f"SELECT rgb FROM render_rgb('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")[0][0]
    assert len(blob) == WIDTH * HEIGHT * 3
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    assert bytes(blob[0:3]) == bytes(pixels[0][0][3:])  # first pixel is row 0, column 0
    offset = 3 * (4 * WIDTH + 7)  # row 4, column 7
    assert bytes(blob[offset:offset + 3]) == bytes(pixels[4][7][3:])
