"""Part 4b S2/P3-P8: sprites through the public framebuffer interface."""

import math
from importlib.resources import files

import duckdb
import pytest

from sqldoom.common import load_render
from sqldoom.db import insert_columns
from test_render import corridor_map_lumps, make_db, pal
from wadbuilder import picture, reveal_colormap

SQL = "SELECT x,y,surface,r,g,b FROM render_pixels('E1M1',?,?,?,?,?) ORDER BY y,x"


def sprite(name="BAR1A0", width=64, height=64, left=32, top=64, seed=32, hole=False):
    columns = {u: [(0, [(seed + u + 2*v) % 256 for v in range(height)])]
               for u in range(width)}
    if hole:
        columns[0] = [(4, [(seed + 2*v) % 256 for v in range(4, height)])]
    return name, picture(width, height, columns, left, top)


def scene(tmp, pictures=None, things=None, **room):
    pictures = pictures if pictures is not None else [sprite()]
    db = make_db(tmp, corridor_map_lumps(**room) + [("S_START", b"")] + pictures + [("S_END", b"")])
    con = duckdb.connect(str(db))
    # Reuse the importer insertion helper; ids start after the existing player.
    insert_columns(con, "things", [("E1M1", i+1, *t) for i, t in enumerate(
        things if things is not None else [(64, 0, 0, 2035, 2)])])
    return con


def frame(con, camera=(0, 0, 0), width=16, height=10):
    return con.execute(SQL, [*camera, width, height]).fetchall()


def old_frame(con, camera=(0, 0, 0)):
    # Hide things via their public skill flag, then reload the static renderer.
    con.execute("UPDATE things SET flags=0 WHERE type<>1")
    load_render(con)
    return frame(con, camera)


def assert_overlay(got, baseline, expected):
    assert {(p[0], p[1]) for p in got if p[2] == "sprite"} == set(expected)
    for p, old in zip(got, baseline):
        xy = p[:2]
        assert p == ((*xy, "sprite", *pal(expected[xy])) if xy in expected else old), xy


@pytest.mark.parametrize("hole", [False, True])
def test_projection_exact_centers_offsets_and_transparency(tmp, hole):
    with scene(tmp, [sprite(width=8, height=16, left=4, top=16, hole=hole)]) as con:
        load_render(con)
        got = frame(con)
        # P5: tz=64, tx=0, cx=8, s=1/8. Bounds [7.5,8.5) contain
        # ONLY center x+0.5=7.5 (x=7), sampling u=floor(4-4)=0.
        # z=41-(y-4.5)*8; v=floor(16-z)=8*y-61: y=8,9 -> v=3,11.
        # Every neighbor (including x=8 on the excluded right edge) is untouched.
        expected = {(7, 9): 32+22}
        if not hole:
            expected[7, 8] = 32+6
        assert_overlay(got, old_frame(con), expected)


@pytest.mark.parametrize("x,y,flags,kind", [(-64,0,2,2035), (3,0,2,2035),
    (64,0,0,2035), (64,0,1,2035), (64,0,18,2035), (64,0,2,999),
    (64,0,2,3004), (64,1000,2,2035)])
def test_near_plane_offscreen_and_filters(tmp, x, y, flags, kind):
    with scene(tmp, things=[(x,y,0,kind,flags)]) as con:
        load_render(con)
        got = frame(con)
        assert got == old_frame(con)


def test_minz_four_is_inclusive(tmp):
    with scene(tmp, things=[(4,0,0,2035,2)]) as con:
        load_render(con)
        assert any(p[2] == "sprite" for p in frame(con))


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("view", [0,45,90,135,180,225,270,315,-45,22.4,22.6])
def test_rotation_and_mirrored_pairs(tmp, paired, view):
    names = (["POSSA1", "POSSA2A8", "POSSA3A7", "POSSA4A6", "POSSA5"] if paired
             else [f"POSSA{r}" for r in range(1,9)])
    pics = [sprite(name, seed=10*i) for i, name in enumerate(names)]
    theta = math.radians(view)
    camera = (64*math.cos(theta), 64*math.sin(theta), view+180)
    rot = math.floor(((view+360+22.5) % 360)/45)+1
    selected = min(rot,10-rot) if paired else rot
    with scene(tmp, pics, things=[(0,0,0,3004,2)]) as con:
        load_render(con)
        got = frame(con, camera)
        expected = {}
        # Independent P5 reference, including floating-point camera transform.
        rad = math.radians(camera[2])
        tz = -camera[0]*math.cos(rad)-camera[1]*math.sin(rad)
        tx = -camera[0]*math.sin(rad)+camera[1]*math.cos(rad)
        cx = 8+tx*8/tz
        for x in range(16):
            u = math.floor((x+0.5-cx)*tz/8+32)
            if not 0 <= u < 64:
                continue
            if paired and rot >= 6:
                u = 63-u
            for y in range(10):
                v = math.floor(64-(41-(y+0.5-5)*tz/8))
                if 0 <= v < 64:
                    expected[x,y] = (10*(selected-1)+u+2*v) % 256
        assert expected
        assert_overlay(got, old_frame(con, camera), expected)


@pytest.mark.parametrize("thing_angle", [0,90,180,270])
def test_rotation_zero_serves_every_view_and_thing_angle(tmp, thing_angle):
    with scene(tmp, things=[(64,0,thing_angle,2035,2)]) as con:
        load_render(con)
        got = frame(con)
        assert got[3*16+7][2:] == ("sprite", *pal(32+28+2*11))


@pytest.mark.parametrize("depth,step", [(160,False), (96,True), (32,True)])
def test_wall_and_step_windows(tmp, depth, step):
    with scene(tmp, things=[(depth,0,0,2035,2)], step=step) as con:
        load_render(con)
        got = frame(con)
        expected = {}
        for x in range(16):
            u = math.floor((x+0.5-8)*depth/8+32)
            for y in range(10):
                # No BSP nodes in this fixture: point_sector returns sector 0.
                v = math.floor(64-(41-(y+0.5-5)*depth/8))
                in_window = depth < 64 or (1.125 <= y+0.5 < 7.125)
                if depth < 128 and 0 <= u < 64 and 0 <= v < 64 and (not step or in_window):
                    expected[x,y] = (32+u+2*v) % 256
        if depth == 96:
            assert any(y == 6 for x,y in expected)
            assert not any(y >= 7 for x,y in expected)
        assert_overlay(got, old_frame(con), expected)


@pytest.mark.parametrize("same_depth", [False, True])
def test_overlap_nearest_then_thing_id_wins(tmp, same_depth):
    pics = [sprite(), sprite("POSSA0", seed=180)]
    with scene(tmp, pics, things=[(64,0,0,2035,2), (64 if same_depth else 96,0,0,3004,2)]) as con:
        load_render(con)
        both = frame(con)
        con.execute("UPDATE things SET flags=0 WHERE type=3004")
        load_render(con)
        near = frame(con)
        # The nearer image covers the whole farther image in this view.
        assert both == near
        assert any(p[2] == "sprite" for p in both)


@pytest.mark.parametrize("kind,light,depth,level", [(2035,160,64,0), (2035,160,96,7),
    (2035,0,64,31), (2035,255,96,0), (34,0,96,0)])
def test_sector_lighting_without_fake_contrast_and_full_bright(tmp, kind, light, depth, level):
    name = "CANDA0" if kind == 34 else "BAR1A0"
    with scene(tmp, [sprite(name)], things=[(depth,0,0,kind,2)],
               light=light, colormap=reveal_colormap()) as con:
        load_render(con)
        pixels = [p for p in frame(con) if p[2] == "sprite"]
        assert pixels
        # P7: start=(15-(light>>4))*4; scale=min(47,floor(2560/tz));
        # clamp(start-scale//2,0,31), with no horizontal/vertical contrast.
        assert {p[3:] for p in pixels} == {pal(level)}


def test_legacy_database_and_reload_preserve_all_public_outputs(tmp, capsys):
    with scene(tmp, things=[]) as con:
        sql = files("sqldoom").joinpath("sql/render.sql").read_text()
        con.execute(sql)
        queries = ["SELECT * FROM render_pixels('E1M1',0,0,0,16,10) ORDER BY y,x",
                   "SELECT * FROM render_rgb('E1M1',0,0,0,16,10)",
                   "SELECT * FROM render_ansi('E1M1',0,0,0,16,5)"]
        expected = [con.execute(q).fetchall() for q in queries]
        # Simulate the exact pre-Part-4b schema without changing a disk database.
        legacy = duckdb.connect()
        try:
            for (table,) in con.execute("SHOW TABLES").fetchall():
                if not table.startswith(('render_', 'point_sector_')) and table not in (
                        'sprites', 'sprite_columns', 'thing_types'):
                    result = con.execute(f'SELECT * FROM "{table}"')
                    columns = ','.join(f'"{d[0]}" {d[1]}' for d in result.description)
                    rows = result.fetchall()
                    legacy.execute(f'CREATE TABLE "{table}" ({columns})')
                    insert_columns(legacy, table, rows)
            for _ in range(2):  # loading twice on one connection must keep working
                load_render(legacy)
                assert [legacy.execute(q).fetchall() for q in queries] == expected
        finally:
            legacy.close()
        assert capsys.readouterr() == ("", "")
