"""sqldoom-render: draw a map seen from inside. Every pixel is computed by SQL.

The renderer lives in sql/render.sql as DuckDB table macros (see odd/tasks/
first-person.md, designs D1-D8). This module only loads them, resolves the camera,
runs one query, and either prints the ANSI frame to the terminal or wraps the RGB
bytes it returns in a PNG.
"""

import argparse
import sys
from importlib.resources import files
from pathlib import Path

from .common import MIN_SIZE, MAX_SIZE, fail, open_map, parse_pos, parse_size
from .png import encode_rgb

RENDER_SIZE = (320, 200)  # PNG pixels
TERMINAL_SIZE = (160, 50)  # terminal characters, one text row per two pixel rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-render",
        description="Render a first-person view of a map, computed entirely in SQL, "
        "as an ANSI terminal frame or a PNG.",
    )
    parser.add_argument("database", type=Path, help="database created by sqldoom-import")
    parser.add_argument("map", help="map name, e.g. E1M1 (case-insensitive)")
    parser.add_argument(
        "--size", type=parse_size, metavar="WxH",
        help=f"output size: COLSxROWS terminal characters when printing a frame "
        f"(default {TERMINAL_SIZE[0]}x{TERMINAL_SIZE[1]}), pixels for PNG output with -o "
        f"(default {RENDER_SIZE[0]}x{RENDER_SIZE[1]}), both between {MIN_SIZE} and {MAX_SIZE}",
    )
    parser.add_argument(
        "--pos", type=parse_pos, metavar="X,Y",
        help="camera position (default: the map's player 1 start)",
    )
    parser.add_argument(
        "--angle", type=float, metavar="DEG",
        help="camera angle in degrees, 0 = east, 90 = north (default: the player 1 start angle)",
    )
    parser.add_argument("-o", "--output", type=Path, help="write a PNG image here (without it, print one ANSI frame to the terminal)")
    args = parser.parse_args(argv)
    map_name = args.map.upper()

    opened = open_map(args.database, map_name)
    if opened is None:
        return 1
    con, map_found = opened

    with con:
        if not map_found:
            return fail(f"map not found: {map_name}")
        con.execute(files("sqldoom").joinpath("sql/render.sql").read_text())
        width, height = args.size or (TERMINAL_SIZE if args.output is None else RENDER_SIZE)
        # Each camera component defaults to the player 1 start; --pos and --angle
        # override independently. With no player start and a flag missing, error.
        player = con.execute(
            "SELECT x, y, angle FROM things WHERE map = ? AND type = 1 ORDER BY id LIMIT 1",
            [map_name],
        ).fetchone()
        if args.pos is None and player is None:
            return fail(f"map has no player start: {map_name}")
        px, py = args.pos if args.pos is not None else (player[0], player[1])
        angle = args.angle if args.angle is not None else (player[2] if player else 0.0)
        if args.output is None:
            # D8: terminal output is cols x rows characters, one per two pixel rows.
            (text,) = con.execute(
                "SELECT text FROM render_ansi(?, ?, ?, ?, ?, ?)",
                [map_name, px, py, angle, width, height],
            ).fetchone()
            sys.stdout.write(text)
            return 0
        (rgb,) = con.execute(
            "SELECT rgb FROM render_rgb(?, ?, ?, ?, ?, ?)", [map_name, px, py, angle, width, height]
        ).fetchone()

    try:
        args.output.write_bytes(encode_rgb(width, height, rgb))
    except OSError as exc:
        return fail(f"cannot write {args.output}: {exc.strerror}")
    print(f"wrote {args.output} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
