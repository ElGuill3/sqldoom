"""Integration checks against the real shareware doom1.wad (skipped when absent).

Set SQLDOOM_WAD to point elsewhere; default is wad/doom1.wad (see README).
"""

import hashlib
import os
from pathlib import Path

import pytest

from conftest import query, render_query, run_automap, run_import, run_module

WAD = Path(os.environ.get("SQLDOOM_WAD", Path(__file__).parent.parent / "wad" / "doom1.wad"))

pytestmark = pytest.mark.skipif(not WAD.is_file(), reason=f"{WAD} not found")

# Each query returns the rows that break one cross-reference; all must be empty.
ORPHANS = {
    "linedef vertex": """
        SELECT l.map, l.id FROM linedefs l
        WHERE NOT EXISTS (SELECT 1 FROM vertexes v WHERE v.map = l.map AND v.id = l.v1)
           OR NOT EXISTS (SELECT 1 FROM vertexes v WHERE v.map = l.map AND v.id = l.v2)""",
    "linedef sidedef": """
        SELECT l.map, l.id FROM linedefs l
        WHERE NOT EXISTS (SELECT 1 FROM sidedefs s WHERE s.map = l.map AND s.id = l.front_sidedef)
           OR (l.back_sidedef IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM sidedefs s WHERE s.map = l.map AND s.id = l.back_sidedef))""",
    "sidedef sector": """
        SELECT s.map, s.id FROM sidedefs s
        WHERE NOT EXISTS (SELECT 1 FROM sectors x WHERE x.map = s.map AND x.id = s.sector)""",
    "seg linedef": """
        SELECT g.map, g.id FROM segs g
        WHERE NOT EXISTS (SELECT 1 FROM linedefs l WHERE l.map = g.map AND l.id = g.linedef)""",
    "subsector seg range": """
        SELECT s.map, s.id FROM ssectors s
        WHERE s.first_seg + s.seg_count > (SELECT count(*) FROM segs g WHERE g.map = s.map)""",
    "node child": """
        SELECT n.map, n.id FROM nodes n, (VALUES (true), (false)) side(is_right),
        LATERAL (SELECT CASE WHEN is_right THEN n.right_child ELSE n.left_child END AS child,
                        CASE WHEN is_right THEN n.right_is_ssector ELSE n.left_is_ssector END AS is_ssector)
        WHERE (is_ssector AND NOT EXISTS (SELECT 1 FROM ssectors s WHERE s.map = n.map AND s.id = child))
           OR (NOT is_ssector AND NOT EXISTS (SELECT 1 FROM nodes c WHERE c.map = n.map AND c.id = child))""",
}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("doom1") / "doom1.duckdb"
    result = run_import(WAD, path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"imported 1264 lumps and 9 maps into {path}\n"
    return path


def test_episode_one_maps(db):
    assert query(db, "SELECT name FROM maps ORDER BY name") == [(f"E1M{i}",) for i in range(1, 10)]


def test_e1m1_record_counts(db):
    tables = ["vertexes", "linedefs", "sidedefs", "sectors", "segs", "ssectors", "nodes", "things"]
    counts = query(db, " UNION ALL ".join(f"SELECT '{t}', count(*) FROM {t} WHERE map = 'E1M1'" for t in tables))
    assert dict(counts) == {
        "vertexes": 467, "linedefs": 475, "sidedefs": 648, "sectors": 85,
        "segs": 732, "ssectors": 237, "nodes": 236, "things": 138,
    }


def test_bsp_tree_has_one_more_leaf_than_nodes(db):
    """A binary tree with n internal nodes has n + 1 leaves (the subsectors)."""
    assert query(db, """
        SELECT n.map FROM (SELECT map, count(*) c FROM nodes GROUP BY map) n
        JOIN (SELECT map, count(*) c FROM ssectors GROUP BY map) s USING (map)
        WHERE s.c <> n.c + 1""") == []


# Part 4a: graphics tables.


def test_palette_has_256_rows(db):
    assert query(db, "SELECT count(*) FROM palette") == [(256,)]


def test_colormaps_has_34_levels(db):
    assert query(db, "SELECT count(*) FROM colormaps") == [(34,)]


def test_texture_count_matches_texture1(db):
    assert query(db, "SELECT count(*) FROM textures") == [(125,)]


def test_sky1_is_256x128(db):
    assert query(db, "SELECT width, height FROM textures WHERE name = 'SKY1'") == [(256, 128)]


def test_flats(db):
    names = [name for (name,) in query(db, "SELECT name FROM flats ORDER BY name")]
    assert len(names) == 54
    for flat in ("FLOOR4_8", "CEIL3_5", "NUKAGE1", "F_SKY1"):
        assert flat in names


def test_every_sidedef_texture_exists(db):
    assert query(db, """
        SELECT s.map, s.id FROM sidedefs s
        WHERE upper_texture IS NOT NULL AND NOT EXISTS (SELECT 1 FROM textures t WHERE t.name = s.upper_texture)
           OR lower_texture IS NOT NULL AND NOT EXISTS (SELECT 1 FROM textures t WHERE t.name = s.lower_texture)
           OR middle_texture IS NOT NULL AND NOT EXISTS (SELECT 1 FROM textures t WHERE t.name = s.middle_texture)
    """) == []


def test_texture_columns_cover_every_texture_width(db):
    assert query(db, "SELECT sum(width) FROM textures") == query(db, "SELECT count(*) FROM texture_columns")


@pytest.mark.parametrize("check", ORPHANS)
def test_cross_references_resolve(db, check):
    assert query(db, ORPHANS[check]) == []


def test_point_sector_at_the_player_start(db):
    """D5: walk from the root node (front when (py-n.y)*dx < dy*(px-n.x)), take the
    subsector's first seg, its linedef, and the sidedef picked by seg direction.
    An independent plain-Python walk of E1M1's tree puts the player 1 start
    (1056, -3616) in sector 38 (floor 0, ceiling 72); the SQL macro must agree.
    """
    assert render_query(db, "SELECT sector FROM point_sector('E1M1', 1056, -3616)") == [(38,)]
    assert query(db, "SELECT floor_height, ceiling_height FROM sectors WHERE map = 'E1M1' AND id = 38") == [(0, 72)]


def test_point_sector_matches_a_python_d5_walk_on_every_map_and_vertex(db):
    """The optimized point_sector (no recursive CTE) must return exactly what the D5
    node walk returns. For every map, sample every vertex (some lie exactly on node
    split lines, exercising the strict-`<` tie rule), the same vertices offset by
    (+1, +1), and the player start; the SQL macro is checked against an independent
    plain-Python walk of the nodes table (tests/test_render_perf.py)."""
    from test_render_perf import reference_walk

    for (map_name,) in query(db, "SELECT name FROM maps ORDER BY name"):
        vertexes = query(db, f"SELECT x, y FROM vertexes WHERE map = '{map_name}' ORDER BY id")
        points = [tuple(v) for v in vertexes]
        points += [(x + 1, y + 1) for x, y in vertexes]
        player = query(
            db,
            f"SELECT x, y FROM things WHERE map = '{map_name}' AND type = 1 ORDER BY id LIMIT 1",
        )
        points += [tuple(p) for p in player]
        values = ", ".join(f"({i}, {px}, {py})" for i, (px, py) in enumerate(points))
        got = {
            points[i]: sector
            for i, sector in render_query(
                db,
                f"SELECT p.i, ps.sector FROM (VALUES {values}) p(i, px, py), "
                f"LATERAL point_sector('{map_name}', p.px, p.py) ps",
            )
        }
        assert len(got) == len(points)
        sector_at = reference_walk(db, map_name)
        for point in points:
            assert got[point] == sector_at(*point), (map_name, point)


# Golden fingerprints of real automap output. A refactor of the screen transform once moved
# single pixels here (floating point rounding at .5 boundaries); these pin the exact output.
def test_automap_ascii_output_is_stable(db):
    result = run_automap(db, "E1M3", "--size", "150x70", text=False)

    assert result.returncode == 0
    assert hashlib.sha256(result.stdout).hexdigest() == (
        "f1522c7b0620d41e836e0a86f84eb5b9e41be749da98e659af8817d9a4718c92"
    )


def test_automap_png_output_is_stable(db, tmp_path):
    out = tmp_path / "e1m1.png"

    result = run_automap(db, "E1M1", "-o", out)

    assert result.returncode == 0
    assert hashlib.sha256(out.read_bytes()).hexdigest() == (
        "afce18bbcf4343affb0d14ab6d2d33f01eba0daed9c8413f97f8d210ad55be27"
    )


def test_first_person_png_output_is_stable(db, tmp_path):
    out = tmp_path / "view.png"

    result = run_module("sqldoom.render", db, "E1M1", "-o", out)

    assert result.returncode == 0
    assert hashlib.sha256(out.read_bytes()).hexdigest() == (
        "a977bf34502b434c6f7a4d828c9be84318117a3f7692f1656c1f01f1a46b184f"
    )


def test_first_person_terminal_output_is_stable(db):
    result = run_module("sqldoom.render", db, "E1M1", "--size", "80x25", text=False)

    assert result.returncode == 0
    assert hashlib.sha256(result.stdout).hexdigest() == (
        "a003735cb6e4b58671544395b98b338b31ece3e5428b85c3c33ebcbc13d278fa"
    )
