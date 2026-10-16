"""Helpers shared by the sqldoom renderer CLIs: size parsing, error style, database opening."""

import argparse
import re
import sys
from pathlib import Path

import duckdb

MIN_SIZE, MAX_SIZE = 8, 4096


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match or not (MIN_SIZE <= int(match[1]) <= MAX_SIZE and MIN_SIZE <= int(match[2]) <= MAX_SIZE):
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, both between 8 and 4096, got '{value}'")
    return int(match[1]), int(match[2])


def parse_pos(value: str) -> tuple[float, float]:
    match = re.fullmatch(r"([^,]+),([^,]+)", value)
    if not match:
        raise argparse.ArgumentTypeError(f"expected X,Y, got '{value}'")
    try:
        return float(match[1]), float(match[2])
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y, got '{value}'")


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def open_map(database: Path, map_name: str):
    """Open a database read-only and check the map exists.

    Returns (connection, map_found); only errors from opening the file and finding the
    maps table mean "wrong database", so the caller can decide what missing maps mean.
    """
    if not database.is_file():
        fail(f"file not found: {database}")
        return None
    try:
        con = duckdb.connect(str(database), read_only=True)
        map_found = con.execute("SELECT 1 FROM maps WHERE name = ?", [map_name]).fetchone()
    except duckdb.Error:
        if "con" in locals():
            con.close()
        fail(f"not a sqldoom database: {database}")
        return None
    return con, map_found
