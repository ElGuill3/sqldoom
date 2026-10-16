"""Load parsed WAD data into a DuckDB database.

Loading speed matters because DuckDB is columnar: per-row inserts are its worst
case. Measured on doom1.wad (~40k map records): row-by-row executemany ~80 s,
binding whole Python lists as UNNEST parameters ~28 s, CSV + COPY well under 1 s.
"""

import csv
import os
import tempfile

import duckdb

from .maps import FORMATS, MapData
from .wad import Lump

SCHEMA = """
CREATE TABLE lumps (
    idx     INTEGER PRIMARY KEY,  -- position in the WAD directory
    name    VARCHAR NOT NULL,
    filepos INTEGER NOT NULL,
    size    INTEGER NOT NULL,
    data    BLOB    NOT NULL
);

CREATE TABLE maps (
    name     VARCHAR PRIMARY KEY,  -- marker lump name: E1M1, MAP01, ...
    lump_idx INTEGER NOT NULL REFERENCES lumps (idx)
);
"""


def insert_columns(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """One INSERT with each column bound as a list. Handles BLOBs; fine for small tables."""
    if not rows:
        return
    columns = [list(col) for col in zip(*rows)]
    unnests = ", ".join("UNNEST(?)" for _ in columns)
    con.execute(f"INSERT INTO {table} SELECT {unnests}", columns)


def copy_rows(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Bulk load through DuckDB's native CSV reader. None becomes an empty field, read as NULL."""
    if not rows:
        return
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        quoted = path.replace("'", "''")
        con.execute(f"COPY {table} FROM '{quoted}' (HEADER false)")
    finally:
        os.unlink(path)


def load(con: duckdb.DuckDBPyConnection, lumps: list[Lump], maps: list[MapData]) -> None:
    con.execute(SCHEMA)
    for fmt in FORMATS:
        con.execute(
            f"CREATE TABLE {fmt.table} (map VARCHAR NOT NULL REFERENCES maps (name), "
            f"id INTEGER NOT NULL, {fmt.columns}, PRIMARY KEY (map, id))"
        )

    insert_columns(con, "lumps", [(l.idx, l.name, l.filepos, l.size, l.data) for l in lumps])
    insert_columns(con, "maps", [(m.name, m.lump_idx) for m in maps])
    for fmt in FORMATS:
        copy_rows(con, fmt.table, [row for m in maps for row in m.rows[fmt.table]])
