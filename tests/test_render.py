"""T1/T2: point_sector and the first-person renderer, computed entirely by SQL.

Both test maps are corridors y in [-128, 128] (and x in [-256, 128]), 16x10 pixels,
camera at (0, 0) looking east. D1 gives focal f = width/2 = 8 and D2 makes D = (1, -u),
so every column ray crosses x = t at parameter t: the east wall at x = 128 is hit at
t = 128 by all 16 columns (|u| <= 15.5/8 < 1, so the side walls at y = +/-128 are only
reached at t >= 136.5, behind it). With eye = 0 + 41 (D5) and y(z) = 5 - (z - eye)*8/t
(D6), the solid east wall spans cy = 5 - 31*8/128 = 3.0625 to fy = 5 + 41*8/128 = 7.5625:
a span [a, b) covers rows ceil(a - 0.5) .. ceil(b - 0.5) - 1, so every column shows
rows 0-2 ceiling, 3-7 wall, 8-9 floor. The east wall linedef runs from (128, 128) to
(128, -128) with the room on its front side, so G4 wall u = floor(dist) with
dist = s * 256 = 128 + 128*u_c. The step map puts a two-sided linedef at x = 64
t = 64) with the far sector raised to floor 24: fy_f = 5 + 17*8/64 = 7.125, so the
opening is [1.125, 7.125) and the second hit draws rows 1-2 ceiling, 3-5 wall
(y(24, 128) = 6.0625), 6 floor inside that window; the first hit draws row 0 ceiling
and rows 7-9 lower.

The maps carry graphics lumps built by wadbuilder.graphics_lumps: the test palette
(palette[i] = (i, 255-i, i//2)), textures and flats with the test pattern
texel(u, v) = (u + 4v) % 256, and by default the identity colormap, so a rendered
pixel shows palette[texel] directly (G7 lighting is tested with the revealing
colormap in test_textures_render.py).
"""

import hashlib
import math

from conftest import render_query, run_import
from wadbuilder import flat, graphics_lumps, map_lumps, write_wad

WIDTH, HEIGHT = 16, 10


def corridor_map_lumps(step: bool = False, player_at: tuple | None = (0, 0, 0),
                       sky: bool = False, split_flags: int = 0, east_flags: int = 0,
                       east_xoff: int = 0, east_yoff: int = 0, far_ceiling: int = 72,
                       light: int = 160, uniform: bool = False, graphics: bool = True,
                       colormap: bytes | None = None):
    """Corridor x in [-256, 128], y in [-128, 128]. The east wall (x = 128) is solid and
    runs from (128, 128) to (128, -128), room on its front side (G4: u runs from v1
    southward, dist = s * 256).

    With step, a two-sided linedef at x = 64 splits it: west sector (floor 0,
    ceiling 72, FLATW/CEILW), east sector (floor 24, ceiling `far_ceiling`, FLATE/CEILE);
    split_flags sets the split linedef's pegging flags (G4) and east_flags the east
    wall's (a one-sided middle wall with flag 16 is bottom-pegged). With sky, the west
    ceiling texture is F_SKY1 (G6). With uniform, every texture is a 1x1 texture named
    UNIF and every flat is FLATU, so whole pixel rows repeat one color (D8
    code-omission tests). With graphics=False the WAD carries no graphics lumps
    (G3: importing succeeds, rendering must fail cleanly).
    """
    wall = (lambda name: "UNIF") if uniform else (lambda name: name)
    flatn = (lambda name: "FLATU") if uniform else (lambda name: name)
    east_textures = (east_xoff, east_yoff) if east_xoff or east_yoff else ()
    vertexes = [(128, 128), (128, -128), (-256, -128), (-256, 128), (64, -128), (64, 128)]
    sectors = [(0, 72, flatn("FLATW"), "F_SKY1" if sky else flatn("CEILW"), light)]
    things = [(*player_at, 1)] if player_at is not None else []
    if not step:
        segs, ssectors = [(0, 1, 0, 0, 0, 0)], [(1, 0)]
        linedefs = [
            (0, 1, 0, None, east_flags),  # east wall, v1 -> v2 pointing south
            (1, 2, 1, None),  # south wall
            (2, 3, 2, None),  # west wall
            (3, 0, 3, None),  # north wall
        ]
        sidedefs = [(0, wall("WALLE"), "-", "-", *east_textures),
                    (0, wall("WALLS"), "-", "-"),
                    (0, wall("WALLW"), "-", "-"), (0, wall("WALLN"), "-", "-")]
    else:
        sectors.append((24, far_ceiling, flatn("FLATE"), flatn("CEILE"), light))
        # subsector 0's first seg: the south wall (sector 0); subsector 1's: the east
        # wall (sector 1, front sidedef)
        segs, ssectors = [(1, 2, 0, 1, 0, 0)], [(1, 0)]
        segs.append((0, 1, 0, 0, 0, 0))
        linedefs = [
            (0, 1, 0, None, east_flags),  # east wall, front sidedef -> sector 1
            (1, 2, 1, None),  # south wall
            (4, 5, 2, 3, split_flags),  # split line: front (east) -> 1, back (west) -> 0
            (2, 3, 4, None),  # west wall
            (3, 0, 5, None),  # north wall
        ]
        sidedefs = [
            (1, wall("WALLED"), "-", "-"),       # east wall, front sidedef -> sector 1
            (0, wall("WALLS"), "-", "-"),        # south wall
            (1, "-", wall("UPPER1"), wall("LOWER1")),  # east side of the split (faces away)
            (0, "-", wall("UPPER2"), wall("LOWER2")),  # west side of the split: the near side
            (0, wall("WALLW"), "-", "-"),        # west wall
            (0, wall("WALLN"), "-", "-"),        # north wall
        ]
    lumps = map_lumps(
        "E1M1",
        vertexes=vertexes,
        linedefs=linedefs,
        sidedefs=sidedefs,
        sectors=sectors,
        things=things,
        segs=segs,
        ssectors=ssectors,
    )
    if graphics:
        if uniform:
            # One 1x1 texture (texel 0) and one all-zero flat: one color everywhere.
            lumps += graphics_lumps([("UNIF", 1, 1)])
            lumps += [("F_START", b""), ("FLATU", flat()), ("F_END", b"")]
        else:
            lumps += graphics_lumps(
                [("WALLE", 64, 64), ("WALLS", 64, 64), ("WALLW", 64, 64), ("WALLN", 64, 64),
                 ("WALLED", 64, 64), ("UPPER1", 64, 64), ("UPPER2", 64, 64),
                 ("LOWER1", 64, 64), ("LOWER2", 64, 64), ("SKY1", 256, 128)],
                ["FLATW", "CEILW", "FLATE", "CEILE"], colormap_data=colormap)
    return lumps


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
    ) + graphics_lumps()  # palette/colormaps only: point_sector tests need render.sql


def make_db(tmp, lumps) -> object:
    tmp.mkdir(exist_ok=True)
    wad = write_wad(tmp / "map.wad", lumps)
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def pal(index: int) -> tuple[int, int, int]:
    """The test palette's RGB for palette index `index`: palette[i] = (i, 255-i, i//2)."""
    return (index, 255 - index, index // 2)


def tex(u: int, v: int) -> tuple[int, int, int]:
    """The test pattern's texel (u, v) as rendered RGB: texel(u, v) = (u + 4v) % 256
    (wadbuilder.test_texture/test_flat), shown directly through the identity colormap
    (G7: pixel = palette[colormaps[level][texel]] and colormaps[k][c] = c)."""
    return pal((u + 4 * v) % 256)


def east_u(c: int, width: int = WIDTH) -> int:
    """G4 wall u of the east wall hit at t = 128 by column c: the linedef runs
    (128, 128) -> (128, -128) with the room on its front side, so
    dist = s * 256 = 128 + 128 * u_c (no x_offset)."""
    return int(math.floor(128 + 128 * (c + 0.5 - width / 2) / (width / 2))) % 64


def east_v(y: int, top: float = 72.0, height: int = HEIGHT, t: float = 128.0) -> int:
    """G4 wall v of pixel row y at depth t: z = 41 - (y + 0.5 - height/2) * t / 8 and
    v = floor(top - z) mod 64 (no y_offset)."""
    z = 41 - (y + 0.5 - height / 2) * t / 8
    return int(math.floor(top - z)) % 64


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


# --- T3: render_ansi (D8) ---------------------------------------------------


def pixel_grid(db, px, py, angle, cols, rows) -> list[list[tuple]]:
    """The render_pixels colors render_ansi encodes: grid[y][x] = (r, g, b)."""
    cells = render_query(
        db,
        f"SELECT x, y, r, g, b FROM render_pixels('E1M1', {px}, {py}, {angle}, {cols}, {rows * 2}) ORDER BY y, x",
    )
    return [[cell[2:] for cell in cells[y * cols:(y + 1) * cols]] for y in range(rows * 2)]


def expected_frame(grid: list[list[tuple]], cols: int, rows: int) -> str:
    """Encode the grid per D8: one text row per pixel-row pair, fg then bg codes only
    when the color differs from the previous cell (always on the first cell), ESC[0m
    and a newline at the end of every row."""
    lines = []
    for tr in range(rows):
        prev_fg = prev_bg = None
        parts = []
        for x in range(cols):
            fg, bg = grid[2 * tr][x], grid[2 * tr + 1][x]
            if fg != prev_fg:
                parts.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
            if bg != prev_bg:
                parts.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
            parts.append("▀")
            prev_fg, prev_bg = fg, bg
        parts.append("\x1b[0m\n")
        lines.append("".join(parts))
    return "".join(lines)


def decode_frame(text: str) -> list[list[tuple]]:
    """Decode D8 rows back to pixels: each ▀ cell contributes fg = top, bg = bottom."""
    grid = []
    fg = bg = (0, 0, 0)
    for line in text.splitlines():
        top, bottom = [], []
        i = 0
        while i < len(line):
            if line[i] == "\x1b":
                end = line.index("m", i)
                params = line[i + 2:end].split(";")
                if params[0] == "38":
                    fg = tuple(int(p) for p in params[2:5])
                elif params[0] == "48":
                    bg = tuple(int(p) for p in params[2:5])
                else:
                    assert params[0] == "0"  # the row-ending reset
                i = end + 1
            else:
                assert line[i] == "▀"
                top.append(fg)
                bottom.append(bg)
                i += 1
        grid.append(top)
        grid.append(bottom)
    return grid


def test_render_ansi_matches_the_d8_encoding_exactly(tmp):
    # angle 30 makes wall depth (and so color) vary per column, so mid-row code
    # changes are exercised, not just first-cell codes.
    db = make_db(tmp, corridor_map_lumps())

    text = render_query(db, "SELECT text FROM render_ansi('E1M1', 0, 0, 30, 16, 5)")[0][0]

    lines = text.splitlines()
    assert len(lines) == 5
    assert text.endswith("\x1b[0m\n")
    assert all(line.startswith("\x1b[38;2;") for line in lines)  # first cell always coded
    assert any(line.count("\x1b[38;2;") > 1 for line in lines)  # a mid-row fg change
    assert any(line.count("\x1b[48;2;") > 1 for line in lines)  # a mid-row bg change
    assert text == expected_frame(pixel_grid(db, 0, 0, 30, 16, 5), 16, 5)


def test_render_ansi_emits_no_repeated_codes_when_colors_repeat(tmp):
    # The uniform corridor at angle 0: every pixel of a row repeats one color (the 1x1
    # texture and the all-zero flat show one texel, and wall depth and lighting are
    # constant across columns).
    db = make_db(tmp, corridor_map_lumps(uniform=True))

    text = render_query(db, "SELECT text FROM render_ansi('E1M1', 0, 0, 0, 16, 5)")[0][0]

    assert text.count("\x1b[38;2;") == 5  # exactly one fg code per text row
    assert text.count("\x1b[48;2;") == 5  # exactly one bg code per text row
    assert text == expected_frame(pixel_grid(db, 0, 0, 0, 16, 5), 16, 5)


def test_render_ansi_decodes_back_to_every_pixel(tmp):
    db = make_db(tmp, corridor_map_lumps())

    text = render_query(db, "SELECT text FROM render_ansi('E1M1', 0, 0, 30, 16, 5)")[0][0]

    grid, decoded = pixel_grid(db, 0, 0, 30, 16, 5), decode_frame(text)
    assert len(decoded) == 10
    for y in range(10):
        for x in range(16):
            assert decoded[y][x] == grid[y][x]


# --- T2: render_pixels ------------------------------------------------------


def test_square_room_rows_are_ceiling_wall_floor_in_every_column(tmp):
    db = make_db(tmp, corridor_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    surfaces = [[cell[2] for cell in row] for row in pixels]
    for column in zip(*surfaces):
        assert list(column) == ["ceiling"] * 3 + ["wall"] * 5 + ["floor"] * 2


def test_square_room_pixel_colors_come_from_wall_and_flat_texels(tmp):
    """G4/G5 anchors through the identity colormap. Column 8 has u_c = 1/16: the east
    wall is hit at dist = 128 + 8 = 136, so u_tex = 136 mod 64 = 8; its middle wall
    rows have z = 41 - (y - 4.5)*16 and v = floor(72 - z) = 16y - 41 (mod 64)."""
    db = make_db(tmp, corridor_map_lumps())

    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    assert [pixels[y][8][3] for y in range(3, 8)] == [tex(8, v)[0] for v in (7, 23, 39, 55, 7)]
    # The far right column: dist = 128 + 128 * 15.5/8 = 248 -> u = 56.
    assert pixels[5][15][3:] == tex(56, 39)
    # G5: the ceiling at row 0, column 8 has d = 31*8/4.5 = 55.11, so its world point
    # is (55.11, -3.44) (u_c = 1/16), texel (55, 3) of the test flat; the floor at
    # row 8 has d = 41*8/3.5 = 93.714..., texel (93 mod 64, floor(5.86) = 5).
    assert pixels[0][8][3:] == tex(55, 3)
    assert pixels[8][8][3:] == tex(29, 5)
    # Every other pixel follows the same formulas (test_textures_render.py compares
    # the full grid against a Python implementation of G4/G5).
    assert pixels[3][15][3:] == tex(east_u(15), east_v(3))


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
    # Column 5 has u_c = -0.3125, so dy = -u_c = +0.3125 and the x = 64 and x = 128
    # hits sit at y = 20 and y = 40.
    # Row 0: near (west) ceiling at t = 64, d = 31*8/4.5 = 55.11 -> texel
    # (55, floor(-17.22) mod 64 = 46). Rows 1-2: far (east) ceiling at t = 128,
    # d = 31*8/3.5 = 70.86 -> texel (70 mod 64, floor(-22.14) mod 64 = 41).
    assert pixels[0][5][3:] == tex(55, 46)
    assert pixels[1][5][3:] == tex(6, 41)
    # Rows 3-5: the east wall's front (east) sidedef middle texture, near sector =
    # east: dist = 128 + 128*u_c = 88 -> u = 24; v = floor(72 - z) = 23 at row 4.
    assert pixels[4][5][3:] == tex(24, 23)
    # Row 6: the east sector's floor peeking above its wall base, d = 41*8/1.5 = 90.67
    # -> texel (90 mod 64, floor(-28.33) mod 64 = 35).
    assert pixels[6][5][3:] == tex(26, 35)
    # Rows 7-9: lower texture of the west (back, near-side) sidedef at t = 64: the hit
    # sits at y = 20, s = 148/256, dist = (1 - s)*256 = 108 -> u = 44; v = floor(24 - z).
    assert pixels[7][5][3:] == tex(44, 3)
    assert pixels[9][5][3:] == tex(44, 19)


def test_render_rgb_is_row_major_rgb_matching_render_pixels(tmp):
    db = make_db(tmp, corridor_map_lumps())

    blob = render_query(db, f"SELECT rgb FROM render_rgb('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")[0][0]
    assert len(blob) == WIDTH * HEIGHT * 3
    pixels = grid(db, f"render_pixels('E1M1', 0, 0, 0, {WIDTH}, {HEIGHT})")
    assert bytes(blob[0:3]) == bytes(pixels[0][0][3:])  # first pixel is row 0, column 0
    offset = 3 * (4 * WIDTH + 7)  # row 4, column 7
    assert bytes(blob[offset:offset + 3]) == bytes(pixels[4][7][3:])
