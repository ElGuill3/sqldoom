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
MIN_SIZE, MAX_SIZE = 8, 4096


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match or not (MIN_SIZE <= int(match[1]) <= MAX_SIZE and MIN_SIZE <= int(match[2]) <= MAX_SIZE):
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, both between 8 and 4096, got '{value}'")
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
        f"pixels for PNG (default {PNG_SIZE[0]}x{PNG_SIZE[1]}), both between {MIN_SIZE} and {MAX_SIZE}",
    )
    parser.add_argument("-o", "--output", type=Path, help="write a PNG image here instead of printing ASCII")
    args = parser.parse_args(argv)
    map_name = args.map.upper()

    if not args.database.is_file():
        return fail(f"file not found: {args.database}")

    # Only opening the file and finding the maps table can mean "wrong database"; errors past
    # that point are renderer bugs and must surface as such, not as a misleading message.
    try:
        con = duckdb.connect(str(args.database), read_only=True)
        map_found = con.execute("SELECT 1 FROM maps WHERE name = ?", [map_name]).fetchone()
    except duckdb.Error:
        if "con" in locals():
            con.close()
        return fail(f"not a sqldoom database: {args.database}")

    with con:
        if not map_found:
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
        try:
            args.output.write_bytes(encode_rgb(width, height, rgb))
        except OSError as exc:
            return fail(f"cannot write {args.output}: {exc.strerror}")
        print(f"wrote {args.output} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
