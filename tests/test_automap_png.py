"""T2: with -o, SQL returns the RGB framebuffer and Python only wraps it in a PNG.

Image mode uses square pixels (aspect 1). At 21x11 the 180x160 test room scales by
0.05 px/unit and is centered horizontally: the split line (x=90) lands on column 10,
the top wall on row 1, the player start (40, 80) on (8, 5).
"""

import struct
import zlib

from conftest import run_automap, run_import
from wadbuilder import room_map_lumps, write_wad

BLACK, RED, BROWN, GREEN = (0, 0, 0), (255, 0, 0), (188, 120, 72), (0, 255, 0)


def read_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Minimal decoder for 8-bit RGB PNGs whose rows all use filter 0."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        assert struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0] == zlib.crc32(kind + body)
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            assert (depth, color) == (8, 2)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 3
    rows = []
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0
        rows.append([tuple(line[1 + x * 3 : 4 + x * 3]) for x in range(width)])
    return width, height, rows


def make_db(tmp):
    wad = write_wad(tmp / "room.wad", room_map_lumps("E1M1", right_floor=24))
    db = tmp / "room.duckdb"
    assert run_import(wad, db).returncode == 0
    return db


def test_writes_png_framebuffer(tmp):
    db, out = make_db(tmp), tmp / "map.png"

    result = run_automap(db, "E1M1", "--size", "21x11", "-o", out)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == f"wrote {out} (21x11)\n"
    width, height, px = read_png(out.read_bytes())
    assert (width, height) == (21, 11)
    assert px[0][0] == BLACK
    assert px[1][10] == RED  # top wall
    assert px[5][10] == BROWN  # split line, floor height changes
    assert px[5][8] == GREEN  # player start
    assert px[5][12] == BLACK  # inside the room
    assert {c for row in px for c in row} == {BLACK, RED, BROWN, GREEN}


def test_default_png_size_is_640x400(tmp):
    db, out = make_db(tmp), tmp / "map.png"

    result = run_automap(db, "E1M1", "-o", out)

    assert result.stdout == f"wrote {out} (640x400)\n"
    assert read_png(out.read_bytes())[:2] == (640, 400)


def test_accepts_size_4096x8(tmp):
    db, out = make_db(tmp), tmp / "map.png"

    result = run_automap(db, "E1M1", "--size", "4096x8", "-o", out)

    assert result.returncode == 0
    assert result.stdout == f"wrote {out} (4096x8)\n"
    assert read_png(out.read_bytes())[:2] == (4096, 8)


def test_cannot_write_png_to_missing_directory(tmp):
    db = make_db(tmp)
    out = tmp / "no-such-dir" / "map.png"

    result = run_automap(db, "E1M1", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: cannot write {out}: No such file or directory\n"
    assert not out.exists()


def test_unknown_map_writes_no_file(tmp):
    db, out = make_db(tmp), tmp / "map.png"

    result = run_automap(db, "E9M9", "-o", out)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: map not found: E9M9\n"
    assert not out.exists()
