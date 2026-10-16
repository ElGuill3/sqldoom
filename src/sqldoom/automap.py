"""sqldoom-automap: draw a map seen from above. Every pixel is computed by SQL.

The renderer lives in sql/automap.sql as DuckDB table macros. This module only
loads them, runs one query, and prints the text or wraps the RGB bytes it returns in a PNG.
"""

import argparse
import re
import sys
from importlib.resources import files
from pathlib import Path

import duckdb

from .png import encode_rgb

ASCII_SIZE = (120, 60)
PNG_SIZE = (640, 400)


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match or min(int(match[1]), int(match[2])) < 8:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, both at least 8, got '{value}'")
    return int(match[1]), int(match[2])


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-automap",
        description="Draw a map seen from above, rendered entirely in SQL, as ASCII art or a PNG.",
    )
    parser.add_argument("database", type=Path, help="database created by sqldoom-import")
    parser.add_argument("map", help="map name, e.g. E1M1 (case-insensitive)")
    parser.add_argument(
        "--size", type=parse_size, metavar="WxH",
        help=f"output size: characters for ASCII (default {ASCII_SIZE[0]}x{ASCII_SIZE[1]}), "
        f"pixels for PNG (default {PNG_SIZE[0]}x{PNG_SIZE[1]})",
    )
    parser.add_argument("-o", "--output", type=Path, help="write a PNG image here instead of printing ASCII")
    args = parser.parse_args(argv)
    map_name = args.map.upper()

    if not args.database.is_file():
        return fail(f"file not found: {args.database}")

    with duckdb.connect(str(args.database), read_only=True) as con:
        if not con.execute("SELECT 1 FROM maps WHERE name = ?", [map_name]).fetchone():
            return fail(f"map not found: {map_name}")
        con.execute(files("sqldoom").joinpath("sql/automap.sql").read_text())
        if args.output is None:
            width, height = args.size or ASCII_SIZE
            (text,) = con.execute("SELECT text FROM automap_ascii(?, ?, ?)", [map_name, width, height]).fetchone()
        else:
            width, height = args.size or PNG_SIZE
            (rgb,) = con.execute("SELECT rgb FROM automap_rgb(?, ?, ?)", [map_name, width, height]).fetchone()

    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_bytes(encode_rgb(width, height, rgb))
        print(f"wrote {args.output} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
