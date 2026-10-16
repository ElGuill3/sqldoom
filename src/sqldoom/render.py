"""sqldoom-render: draw a map seen from inside. Every pixel is computed by SQL.

The renderer lives in sql/render.sql as DuckDB table macros (see odd/tasks/
first-person.md, designs D1-D7). This module only loads them, resolves the camera,
runs one query, and wraps the RGB bytes it returns in a PNG.
"""

import argparse
import sys
from importlib.resources import files
from pathlib import Path

from .common import MIN_SIZE, MAX_SIZE, fail, open_map, parse_pos, parse_size
from .png import encode_rgb

RENDER_SIZE = (320, 200)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-render",
        description="Render a first-person view of a map, computed entirely in SQL, as a PNG.",
    )
    parser.add_argument("database", type=Path, help="database created by sqldoom-import")
    parser.add_argument("map", help="map name, e.g. E1M1 (case-insensitive)")
    parser.add_argument(
        "--size", type=parse_size, metavar="WxH",
        help=f"output size in pixels (default {RENDER_SIZE[0]}x{RENDER_SIZE[1]}), "
        f"both between {MIN_SIZE} and {MAX_SIZE}",
    )
    parser.add_argument(
        "--pos", type=parse_pos, metavar="X,Y",
        help="camera position (default: the map's player 1 start)",
    )
    parser.add_argument(
        "--angle", type=float, metavar="DEG",
        help="camera angle in degrees, 0 = east, 90 = north (default: the player 1 start angle)",
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="write a PNG image here")
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
        width, height = args.size or RENDER_SIZE
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
