# Feature: wad-import

Part 1 of SQLDoom-from-scratch on DuckDB: load a Doom WAD into relational tables.
Later parts (top-down map, BSP wall render, floors/sprites/logic) get their own feature documents.

## Specs

- S1: "quiero intentar hacerlo desde 0" (Doom running in a SQL database, built from scratch).
- S2: "mejor arrancar desde 0, la propuesta con duckDB me parece razonable para expermientacion" (engine: DuckDB, no copying cedardb/sqldoom).
- S3: "vamos por partes, pero tu implementas y vas explicando" (one part at a time; the agent implements and explains each step).
- S4: Part 1 deliverable (agreed roadmap step 1): import the WAD into DuckDB tables: `vertexes`, `linedefs`, `sidedefs`, `sectors`, `segs`, `ssectors`, `nodes`, `things`, plus the lump directory.

## Tasks

- T1 [S2,S4] project scaffold (uv, duckdb, pytest) + WAD header/directory parser -> `lumps` table. route: inline. commit: 27464c6 [done]
- T2 [S4] map lumps of every map -> 8 relational tables with NULL for "no reference". route: inline. commit: 09e2515 [done]
- T3 [S3,S4] CLI `sqldoom-import`, error handling, README walkthrough, integrity queries against doom1.wad. route: inline. commit: (this commit, see git log) [done]

## Log

- L1: "Hace poco vi que lograron hacer correr doom en una base de datos sql, quiero intentar hacerlo desde 0, que necesitaria y que tan complejo seria?"
- L2: "la noticia es mas reciente, revisalo para encontrar si hay una version mas cercana a doom en sql" -> found SQLDoom (cedardb.com/blog/sqldoom).
- L3: "mejor arrancar desde 0, la propuesta con duckDB me parece razonable para expermientacion"
- L4: "vamos por partes, pero tu implementas y vas explicando"
- L5: Decisions: parse binary in Python (`struct`), store as-is in DuckDB; Python 3.12 (duckdb wheels); doom1.wad shareware from the Debian package, kept in gitignored `wad/`; refuse to overwrite an existing database unless `--force`.
- L6: Evidence: row-wise executemany loaded doom1.wad in ~84 s; list-bound UNNEST ~28 s; CSV + COPY 1.4 s. Map tables use CSV + COPY, lumps (BLOBs) use UNNEST.
- L7: Evidence: doom1.wad imports 1264 lumps / 9 maps; E1M1 has 467 vertexes, 475 linedefs, 648 sidedefs, 85 sectors, 732 segs, 237 ssectors, 236 nodes, 138 things; all cross-reference checks are empty.
