# Feature: automap

Part 2 of SQLDoom-from-scratch on DuckDB: draw a map seen from above, with every pixel computed by SQL.
Builds on Part 1 (`odd/tasks/wad-import.md`); the client only encodes/prints what SQL returns.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando" (one part at a time; the agent implements and explains).
- S2: "procede" -> Part 2 as announced: "dibujar el mapa de E1M1 visto desde arriba **con SQL**. La consulta `linedefs JOIN vertexes` del README ya te da todas las paredes con coordenadas; falta rasterizarlas a píxeles."

## Tasks

- T1 [S1,S2] SQL renderer (`automap` table macro: world->screen transform, line rasterization, priority, player marker) + `sqldoom-automap` ASCII output and errors. route: inline. commit: 01f3aef [done]
- T2 [S2] PNG output: SQL returns the RGB framebuffer as one BLOB; Python only wraps it in PNG. route: inline. commit: a957d8b [done]
- T3 [S1] README section explaining the pipeline; render E1M1. route: inline. commit: (this commit, see git log) [done]

## Log

- L1: "procede" (after Part 1 closed, proposing Part 2 = top-down map with SQL).
- L2: Decisions: renderer lives in SQL (TEMP table macro loaded from `src/sqldoom/sql/automap.sql`, works on read-only connections); colors like the Doom automap: one-sided wall red `#`, floor change brown `+`, ceiling-only change yellow `=`, other two-sided gray `.`, player start green `@`, background black/space; ASCII default 120x60 with pixel aspect 2 (terminal cells are tall), PNG default 640x400 aspect 1; PLAYPAL-based colors deferred.
- L3: Test fix before GREEN: player moved from x=45 to x=40 so expected pixels never sit on an exact .5 that floating point could round either way.
- L4: Evidence: E1M1 renders in ~0.2 s ASCII / ~0.3 s PNG 640x400 (256k pixels: 4286 wall, 1975 floor, 1107 ceiling, 308 plain, 17 player).
