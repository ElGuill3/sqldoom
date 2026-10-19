"""Load parsed WAD data into a DuckDB database.

Loading speed matters because DuckDB is columnar: per-row inserts are its worst
case. Measured on doom1.wad (~40k map records): row-by-row executemany ~80 s,
binding whole Python lists as UNNEST parameters ~28 s, CSV + COPY well under 1 s.
List (UTINYINT[]) columns are worse still: UNNEST binding converts nested Python
lists one value at a time (~155 s for doom1.wad's ~1M texture texels, and it
grows superlinearly), so those tables also go through CSV, with each list
serialized as a DuckDB literal like "[1,2,3]".
"""

import csv
import os
import tempfile

import duckdb

from .graphics import Graphics
from .maps import FORMATS, MapData
from .things import THING_TYPES
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

CREATE TABLE palette (
    idx INTEGER PRIMARY KEY,  -- palette entry 0..255 (PLAYPAL palette 0)
    r   INTEGER NOT NULL,
    g   INTEGER NOT NULL,
    b   INTEGER NOT NULL
);

CREATE TABLE colormaps (
    level  INTEGER PRIMARY KEY,  -- lighting level 0..33 (0 = full bright)
    colors UTINYINT[256] NOT NULL  -- palette index per texel
);

CREATE TABLE textures (
    name   VARCHAR PRIMARY KEY,
    width  INTEGER NOT NULL,
    height INTEGER NOT NULL
);

CREATE TABLE texture_columns (
    texture VARCHAR NOT NULL REFERENCES textures (name),
    u       INTEGER NOT NULL,  -- texel column 0..width-1
    pixels  UTINYINT[] NOT NULL,  -- palette indices, one per texel row
    PRIMARY KEY (texture, u)
);

CREATE TABLE sprites (
    name        VARCHAR PRIMARY KEY,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    left_offset INTEGER NOT NULL,
    top_offset  INTEGER NOT NULL
);

CREATE TABLE sprite_columns (
    sprite VARCHAR NOT NULL REFERENCES sprites (name),
    u      INTEGER NOT NULL,
    pixels UTINYINT[] NOT NULL,  -- one per texel row; NULL elements are transparent
    PRIMARY KEY (sprite, u)
);

CREATE TABLE thing_types (
    type        INTEGER PRIMARY KEY,
    sprite      VARCHAR(4) NOT NULL,
    frame       VARCHAR(1) NOT NULL,
    full_bright BOOLEAN NOT NULL
);

CREATE TABLE flats (
    name   VARCHAR PRIMARY KEY,
    pixels UTINYINT[4096] NOT NULL  -- 64x64, row-major
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
    """Bulk load through DuckDB's native CSV reader. None becomes an empty field, read as NULL.
    List-valued cells must be pre-serialized with list_literal."""
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


def list_literal(values: list[int] | list[int | None]) -> str:
    """DuckDB list literal for COPY; spell transparent elements NULL, not Python's None."""
    return "[" + ",".join("NULL" if value is None else str(value) for value in values) + "]"


def load(con: duckdb.DuckDBPyConnection, lumps: list[Lump], maps: list[MapData],
         graphics: Graphics) -> None:
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

    # Graphics. The UNNEST loader is pathologically slow on UTINYINT[] columns
    # (~200 s for doom1.wad's ~1M texels), so every list column goes through
    # CSV + COPY like the map tables, with lists as DuckDB literals.
    insert_columns(con, "palette", [(i, r, g, b) for i, (r, g, b) in enumerate(graphics.palette)])
    copy_rows(con, "colormaps", [(k, list_literal(colors)) for k, colors in enumerate(graphics.colormaps)])
    copy_rows(con, "textures", [(t.name, t.width, t.height) for t in graphics.textures])
    copy_rows(con, "texture_columns", [
        (t.name, u, list_literal(pixels)) for t in graphics.textures for u, pixels in enumerate(t.columns)
    ])
    copy_rows(con, "flats", [(name, list_literal(pixels)) for name, pixels in graphics.flats])
    copy_rows(con, "sprites", [
        (s.name, s.width, s.height, s.left_offset, s.top_offset) for s in graphics.sprites
    ])
    copy_rows(con, "sprite_columns", [
        (s.name, u, list_literal(pixels)) for s in graphics.sprites for u, pixels in enumerate(s.columns)
    ])
    copy_rows(con, "thing_types", THING_TYPES)
