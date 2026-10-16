"""Load parsed WAD data into a DuckDB database."""

import duckdb

from .wad import Lump

SCHEMA = """
CREATE TABLE lumps (
    idx     INTEGER PRIMARY KEY,  -- position in the WAD directory
    name    VARCHAR NOT NULL,
    filepos INTEGER NOT NULL,
    size    INTEGER NOT NULL,
    data    BLOB    NOT NULL
);
"""


def load(con: duckdb.DuckDBPyConnection, lumps: list[Lump]) -> None:
    con.execute(SCHEMA)
    con.executemany(
        "INSERT INTO lumps VALUES (?, ?, ?, ?, ?)",
        [(l.idx, l.name, l.filepos, l.size, l.data) for l in lumps],
    )
