"""Performance pass (odd/tasks/render-perf.md T2): the optimized renderer must stay
byte-identical to the original, so these tests pin the observable behavior that the
rewrite has to preserve:

- point_sector (rewritten without a recursive CTE) must return exactly what the D5
  node walk returns, checked against an independent plain-Python walk of the nodes
  table on synthetic maps and, in test_doom1_wad.py, on every doom1.wad map;
- points exactly on a node's split line resolve by the D5 strict `<` rule
  (false -> left child);
- render.sql can be executed twice on one connection (the viewer and the tests
  may do that) and still renders the same frame.

None of these add behavior: they are characterization tests, written and passing
against the original renderer before the optimization, and they must keep passing
afterwards unchanged.
"""

import math

import pytest

from conftest import render_query, run_import
from test_render import corridor_map_lumps, grid, make_db, split_map_lumps, tex
from wadbuilder import graphics_lumps, map_lumps, write_wad


def reference_walk(db, map_name: str):
    """Load one map's BSP tables once, return sector_at(px, py): D5 as plain Python.
    Walk the nodes from the root (highest node id), going right (front) when
    (py - n.y) * n.dx < n.dy * (px - n.x), until a subsector; its first seg's
    linedef and seg direction pick the sidedef's sector. A map without nodes is
    subsector 0."""
    nodes = {
        row[0]: row
        for row in render_query(
            db,
            f"SELECT id, x, y, dx, dy, right_child, left_child, right_is_ssector, "
            f"left_is_ssector FROM nodes WHERE map = '{map_name}'",
        )
    }
    ssectors = dict(
        render_query(db, f"SELECT id, first_seg FROM ssectors WHERE map = '{map_name}'")
    )
    segs = {
        row[0]: row
        for row in render_query(
            db, f"SELECT id, linedef, direction FROM segs WHERE map = '{map_name}'"
        )
    }
    linedefs = {
        row[0]: row
        for row in render_query(
            db, f"SELECT id, front_sidedef, back_sidedef FROM linedefs WHERE map = '{map_name}'"
        )
    }
    sidedefs = dict(
        render_query(db, f"SELECT id, sector FROM sidedefs WHERE map = '{map_name}'")
    )

    def sector_at(px, py) -> int:
        if not nodes:
            ssector = 0
        else:
            node_id = max(nodes)
            while True:
                _, x, y, dx, dy, right_child, left_child, right_ss, left_ss = nodes[node_id]
                if (py - y) * dx < dy * (px - x):  # front -> right child
                    if right_ss:
                        ssector = right_child
                        break
                    node_id = right_child
                else:
                    if left_ss:
                        ssector = left_child
                        break
                    node_id = left_child
        _, linedef, direction = segs[ssectors[ssector]]
        _, front_sidedef, back_sidedef = linedefs[linedef]
        return sidedefs[front_sidedef if direction == 0 else back_sidedef]

    return sector_at


def sql_sectors(db, map_name: str, points: list[tuple]) -> dict[tuple, int]:
    """One batched query: the SQL macro's sector for every (px, py) sample point."""
    values = ", ".join(f"({i}, {px}, {py})" for i, (px, py) in enumerate(points))
    rows = render_query(
        db,
        f"SELECT p.i, ps.sector FROM (VALUES {values}) p(i, px, py), "
        f"LATERAL point_sector('{map_name}', p.px, p.py) ps",
    )
    return {points[i]: sector for i, sector in rows}


def sample_points(vertexes: list[tuple[int, int]]) -> list[tuple[float, float]]:
    """Every vertex, the same vertex offset by (+1, +1) (off any axis split), and a
    couple of interior points. Vertexes themselves may sit exactly on node split
    lines, exercising the D5 strict-`<` tie rule against the reference walk."""
    points = [tuple(v) for v in vertexes]
    points += [(x + 1, y + 1) for x, y in vertexes]
    points += [(0.5, 0.5), (-17.25, 3.5)]
    return points


def check_points(db, map_name: str, points: list[tuple]) -> None:
    """Every sample point: the SQL macro must match the Python D5 walk."""
    sector_at = reference_walk(db, map_name)
    got = sql_sectors(db, map_name, points)
    assert len(got) == len(points)  # every point found a sector
    for point in points:
        assert got[point] == sector_at(*point), point


# --- point_sector equivalence against a Python D5 walk -----------------------


def test_point_sector_matches_the_d5_walk_on_the_split_map(tmp):
    db = make_db(tmp, split_map_lumps())

    points = sample_points([(0, 0), (128, 0), (128, 128), (0, 128), (64, 0), (64, 128)])
    check_points(db, "E1M1", points)


def test_point_sector_matches_the_d5_walk_on_maps_without_and_with_steps(tmp):
    for name, lumps in (("plain", corridor_map_lumps()), ("step", corridor_map_lumps(step=True))):
        (tmp / name).mkdir()
        db = make_db(tmp / name, lumps)

        points = sample_points(
            [(-256, -128), (128, -128), (128, 128), (-256, 128), (64, -128), (64, 128)]
        )
        check_points(db, "E1M1", points)


# --- the D5 tie rule: a point exactly on a node's split line -----------------


def on_line_map_lumps():
    """A 128x128 rectangle whose single BSP node splits at x = 64 (dx = 0, dy > 0),
    like split_map_lumps: the D5 test (py - n.y) * n.dx < n.dy * (px - n.x) becomes
    0 < dy * (px - 64), so a point exactly ON the line goes LEFT (west, sector 0)."""
    return map_lumps(
        "E1M1",
        vertexes=[(0, 0), (128, 0), (128, 128), (0, 128), (64, 0), (64, 128)],
        linedefs=[
            (3, 0, 0, None),
            (4, 5, 1, 2),
            (1, 2, 3, None),
            (0, 1, 4, None),
            (2, 3, 5, None),
        ],
        sidedefs=[0, (1, "-", "-", "-"), (0, "-", "-", "-"), 1, 0, 0],
        sectors=[(0, 72, "FLATW", "CEILW", 160), (24, 72, "FLATE", "CEILE", 160)],
        things=[(32, 64, 0, 1)],
        segs=[(3, 0, 0, 0, 0, 0), (1, 2, 0, 2, 0, 0)],
        ssectors=[(1, 0), (1, 1)],
        nodes=[(64, 0, 0, 128, 1, 0, True, True)],
    ) + graphics_lumps()  # palette/colormaps only: point_sector tests need render.sql


def test_point_exactly_on_a_split_line_goes_to_the_left_child(tmp):
    db = make_db(tmp, on_line_map_lumps())

    for y in (-40.5, 0, 64, 128, 200):
        # On the line: (py - 0) * 0 == 128 * (px - 64), so `<` is false -> left -> sector 0.
        assert render_query(db, f"SELECT sector FROM point_sector('E1M1', 64, {y})") == [(0,)], y
        # Half a unit to either side resolves by the same strict comparison.
        assert render_query(db, f"SELECT sector FROM point_sector('E1M1', 63.5, {y})") == [(0,)], y
        assert render_query(db, f"SELECT sector FROM point_sector('E1M1', 64.5, {y})") == [(1,)], y
    assert reference_walk(db, "E1M1")(64, 64) == 0  # the Python walk agrees


def test_short_sky_columns_do_not_sample_the_next_image_in_the_atlas(tmp):
    """Characterize list_extract's old out-of-image NULL -> palette[0] fallback.
    G6 always wraps v at 128, even for a shorter imported SKY1. An atlas lookup
    must not read the following column/image when that local index is absent.
    """
    lumps = corridor_map_lumps(sky=True, graphics=False) + graphics_lumps(
        [("SKY1", 256, 16), ("WALLE", 64, 64), ("WALLS", 64, 64),
         ("WALLW", 64, 64), ("WALLN", 64, 64)],
        ["FLATW", "CEILW"],
    )
    db = make_db(tmp, lumps)
    pixels = grid(db, "render_pixels('E1M1', 0, 0, 0, 16, 10)")
    assert pixels[0][8][2:] == ("sky", *tex(245, 10))
    for y in (1, 2):  # v = 30, 50: beyond the 16-entry sky column
        assert all(cell[2:] == ("sky", 0, 255, 0) for cell in pixels[y])
    assert pixels[5][8][2:] == ("wall", *tex(8, 39))
    assert pixels[8][8][2:] == ("floor", *tex(29, 5))


# --- render.sql loads twice on one connection --------------------------------


def test_render_sql_executed_twice_on_one_connection_still_renders(tmp):
    import duckdb
    from importlib.resources import files

    db = make_db(tmp, corridor_map_lumps())
    sql = files("sqldoom").joinpath("sql/render.sql").read_text()
    with duckdb.connect(str(db)) as con:
        con.execute(sql)
        once = con.execute("SELECT text FROM render_ansi('E1M1', 0, 0, 30, 16, 5)").fetchall()

        con.execute(sql)  # the viewer and tests may reload the macros
        twice = con.execute("SELECT text FROM render_ansi('E1M1', 0, 0, 30, 16, 5)").fetchall()
        # And a different camera still works after the reload (static data rebuilt).
        moved = con.execute("SELECT text FROM render_ansi('E1M1', -100, 0, 90, 16, 5)").fetchall()

    assert once == twice
    assert moved == render_query(db, "SELECT text FROM render_ansi('E1M1', -100, 0, 90, 16, 5)")
