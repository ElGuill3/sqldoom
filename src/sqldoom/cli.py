"""sqldoom-import: load a Doom WAD file into a DuckDB database."""

import argparse
import os
import sys
from pathlib import Path

import duckdb

from . import db
from .graphics import decode_graphics
from .maps import decode_map
from .wad import CorruptWad, NotAWad, find_maps, read_lumps


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-import",
        description="Load a Doom WAD file into a DuckDB database.",
    )
    parser.add_argument("wad", type=Path, help="path to an IWAD or PWAD, e.g. doom1.wad")
    parser.add_argument("database", type=Path, help="DuckDB database file to create")
    parser.add_argument("--force", action="store_true", help="replace the database if it already exists")
    args = parser.parse_args(argv)

    if not args.wad.is_file():
        return fail(f"file not found: {args.wad}")
    if args.database.exists() and not args.force:
        return fail(f"database already exists: {args.database} (use --force to replace)")

    # Parse everything before touching the target, so bad input never leaves a database behind.
    try:
        lumps = read_lumps(args.wad.read_bytes())
        maps = [decode_map(lumps, marker) for marker in find_maps(lumps)]
        graphics = decode_graphics(lumps)
    except NotAWad:
        return fail(f"not a WAD file: {args.wad}")
    except CorruptWad:
        return fail(f"corrupt WAD file: {args.wad}")

    # Build into a scratch file and swap it in atomically: an existing database stays intact on failure.
    scratch = args.database.with_name(args.database.name + ".partial")
    scratch.unlink(missing_ok=True)
    try:
        with duckdb.connect(str(scratch)) as con:
            db.load(con, lumps, maps, graphics)
        os.replace(scratch, args.database)
    finally:
        scratch.unlink(missing_ok=True)

    print(f"imported {len(lumps)} lumps and {len(maps)} maps into {args.database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
