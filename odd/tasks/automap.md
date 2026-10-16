# Feature: automap

Part 2 of SQLDoom-from-scratch on DuckDB: draw a map seen from above, with every pixel computed by SQL.
Builds on Part 1 (`odd/tasks/wad-import.md`); the client only encodes/prints what SQL returns.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando" (one part at a time; the agent implements and explains).
- S2: "procede" -> Part 2 as announced: "dibujar el mapa de E1M1 visto desde arriba **con SQL**. La consulta `linedefs JOIN vertexes` del README ya te da todas las paredes con coordenadas; falta rasterizarlas a píxeles."

## Tasks

- T1 [S1,S2] SQL renderer (`automap` table macro: world->screen transform, line rasterization, priority, player marker) + `sqldoom-automap` ASCII output and errors. route: inline. commit: pending
- T2 [S2] PNG output: SQL returns the RGB framebuffer as one BLOB; Python only wraps it in PNG. route: inline. commit: pending
- T3 [S1] README section explaining the pipeline; render E1M1. route: inline. commit: pending

## Log

- L1: "procede" (after Part 1 closed, proposing Part 2 = top-down map with SQL).
- L2: Decisions: renderer lives in SQL (TEMP table macro loaded from `src/sqldoom/sql/automap.sql`, works on read-only connections); colors like the Doom automap: one-sided wall red `#`, floor change brown `+`, ceiling-only change yellow `=`, other two-sided gray `.`, player start green `@`, background black/space; ASCII default 120x60 with pixel aspect 2 (terminal cells are tall), PNG default 640x400 aspect 1; PLAYPAL-based colors deferred.
