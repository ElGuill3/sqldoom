"""T2: sqldoom-render writes the SQL framebuffer as a PNG, with automap-style errors.

The 16x10 corridor renders every column the same way (see test_render.py), so PNG
comparisons against direct render_pixels queries pin the whole CLI pipeline:
argument parsing, camera resolution, macro loading, and PNG wrapping.
"""

import pytest
from conftest import render_query, run_import, run_render
from test_automap_png import read_png
from test_render import HEIGHT, WIDTH, corridor_map_lumps
from wadbuilder import write_wad

# Camera away from the player start, so flag overrides cannot pass by coincidence.
PLAYER = (-128, 64, 90)


def make_db(tmp, player: bool = True):
    wad = write_wad(tmp / "map.wad", corridor_map_lumps(player_at=PLAYER if player else None))
    db = tmp / "map.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def sql_rows(db, px: float, py: float, angle: float) -> list[list[tuple]]:
    rows = render_query(
        db, f"SELECT r, g, b FROM render_pixels('E1M1', {px}, {py}, {angle}, {WIDTH}, {HEIGHT}) ORDER BY y, x"
    )
    return [rows[y * WIDTH:(y + 1) * WIDTH] for y in range(HEIGHT)]


def render_ok(tmp, db, name: str, *args) -> list[list[tuple]]:
    out = tmp / name
    result = run_render(db, *args, "-o", out)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == f"wrote {out} ({WIDTH}x{HEIGHT})\n"
    width, height, pixels = read_png(out.read_bytes())
    assert (width, height) == (WIDTH, HEIGHT)
    return pixels


def test_writes_png_matching_render_pixels(tmp):
    db = make_db(tmp)

    pixels = render_ok(tmp, db, "view.png", "E1M1", "--size", "16x10", "--pos", "0,0", "--angle", "0")

    assert pixels == sql_rows(db, 0, 0, 0)


def test_default_camera_is_the_player_start(tmp):
    db = make_db(tmp)

    pixels = render_ok(tmp, db, "view.png", "E1M1", "--size", "16x10")

    assert pixels == sql_rows(db, *PLAYER)
    assert pixels != sql_rows(db, 0, 0, 0)  # the player start really is elsewhere


def test_pos_and_angle_override_independently(tmp):
    db = make_db(tmp)

    assert render_ok(tmp, db, "pos.png", "E1M1", "--size", "16x10", "--pos", "0,0") == sql_rows(db, 0, 0, PLAYER[2])
    assert render_ok(tmp, db, "angle.png", "E1M1", "--size", "16x10", "--angle", "0") == sql_rows(db, *PLAYER[:2], 0)


def test_map_name_is_case_insensitive(tmp):
    db = make_db(tmp)

    pixels = render_ok(tmp, db, "view.png", "e1m1", "--size", "16x10", "--pos", "0,0", "--angle", "0")

    assert pixels == sql_rows(db, 0, 0, 0)


def test_missing_database(tmp):
    missing, out = tmp / "nope.duckdb", tmp / "view.png"

    result = run_render(missing, "E1M1", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: file not found: {missing}\n"
    assert not out.exists()


def test_not_a_sqldoom_database(tmp):
    junk, out = tmp / "junk.duckdb", tmp / "view.png"
    junk.write_bytes(b"junk bytes, definitely not a DuckDB file\n" * 4)
    before = junk.read_bytes()

    result = run_render(junk, "E1M1", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: not a sqldoom database: {junk}\n"
    assert junk.read_bytes() == before  # opened read-only, byte-identical


def test_unknown_map_writes_no_file(tmp):
    db, out = make_db(tmp), tmp / "view.png"

    result = run_render(db, "E9M9", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: map not found: E9M9\n"
    assert not out.exists()


def test_no_player_start(tmp):
    db, out = make_db(tmp, player=False), tmp / "view.png"

    result = run_render(db, "E1M1", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: map has no player start: E1M1\n"
    assert not out.exists()


@pytest.mark.parametrize("size", ["3x3", "21", "axb", "21x7", "4097x8", "8x5000"])
def test_rejects_bad_size(tmp, size):
    db, out = make_db(tmp), tmp / "view.png"

    result = run_render(db, "E1M1", "--size", size, "-o", out)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith(
        f"sqldoom-render: error: argument --size: expected WIDTHxHEIGHT, both between 8 and 4096, got '{size}'\n"
    )


def test_without_output_prints_one_ansi_frame(tmp):
    db = make_db(tmp)

    result = run_render(db, "E1M1", "--size", "16x8", "--pos", "0,0", "--angle", "0")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    expected = render_query(db, "SELECT text FROM render_ansi('E1M1', 0, 0, 0, 16, 8)")[0][0]
    assert result.stdout == expected


def test_terminal_default_size_is_160x50(tmp):
    db = make_db(tmp)

    result = run_render(db, "E1M1", "--pos", "0,0", "--angle", "0")

    assert result.returncode == 0, result.stderr
    expected = render_query(db, "SELECT text FROM render_ansi('E1M1', 0, 0, 0, 160, 50)")[0][0]
    assert result.stdout == expected
    assert result.stdout.count("\n") == 50


def test_terminal_size_bounds_are_enforced(tmp):
    db = make_db(tmp)

    result = run_render(db, "E1M1", "--size", "3x3")

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith(
        "sqldoom-render: error: argument --size: expected WIDTHxHEIGHT, both between 8 and 4096, got '3x3'\n"
    )


@pytest.mark.parametrize("pos", ["10", "a,b", "1,2,3"])
def test_rejects_bad_pos(tmp, pos):
    db, out = make_db(tmp), tmp / "view.png"

    result = run_render(db, "E1M1", "--pos", pos, "-o", out)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.endswith(f"sqldoom-render: error: argument --pos: expected X,Y, got '{pos}'\n")


def test_cannot_write_png_to_missing_directory(tmp):
    db, out = make_db(tmp), tmp / "no-such-dir" / "view.png"

    result = run_render(db, "E1M1", "--size", "16x10", "--pos", "0,0", "--angle", "0", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: cannot write {out}: No such file or directory\n"
    assert not out.exists()
