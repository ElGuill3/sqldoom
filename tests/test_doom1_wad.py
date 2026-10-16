"""Integration checks against the real shareware doom1.wad (skipped when absent).

Set SQLDOOM_WAD to point elsewhere; default is wad/doom1.wad (see README).
"""

import os
from pathlib import Path

import pytest

from conftest import query, run_import

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


@pytest.mark.parametrize("check", ORPHANS)
def test_cross_references_resolve(db, check):
    assert query(db, ORPHANS[check]) == []
