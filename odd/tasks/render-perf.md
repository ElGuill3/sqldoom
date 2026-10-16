# Feature: render-perf

Performance pass on the Part 3 first-person renderer before Part 4 adds textures, plus two viewer fixes from the
Part 3 review. Output must stay pixel-identical (golden hashes in tests/test_doom1_wad.py).

## Specs

- S1: "va bastante bien para ser el primer piloto, por el momento a unos 10 fps"
- S2: "si, procede" -> accepted proposal: measure first (EXPLAIN ANALYZE), then optimize toward 25-30 fps; include the viewer fixes "que se adapte cuando cambiás el tamaño de la ventana" and "que la terminal se restaure aunque el proceso muera por una señal (cerrar la pestaña, `kill`)".

## Tasks

- T1 [S2] Profile render_ansi/render_rgb on E1M1 with EXPLAIN ANALYZE; record where time goes. route: inline. commit: none (evidence in Log)
- T2 [S1,S2] Optimize the renderer guided by T1; frames stay byte-identical. route: worker. commit: pending
- T3 [S2] Viewer: re-render on terminal resize (SIGWINCH); restore the terminal on SIGTERM/SIGHUP. route: worker. commit: pending
- T4 [S2] README performance numbers, close. route: inline. commit: pending

## Log

- L1: "va bastante bien para ser el primer piloto, por el momento a unos 10 fps"
- L2: "si, procede"
- L3: T1 evidence (E1M1, player start, 160x100, best of 5; cumulative time per render_pixels CTE): camera/point_sector 15 ms (recursive BSP walk), raw cross join 14 ms (76,000 column x linedef pairs), geom 20 ms (per-frame joins hits->sides->sidedefs->sectors), spans 20 ms (texel(): md5 per channel per span), final grid + best ~17 ms, render_ansi encoding ~16 ms. render_pixels total ~69 ms, render_ansi 160x50 ~85 ms.
- L4: T2 plan (decided by the agent): (a) point_sector without recursion: precompute each subsector's half-plane path (node, side) from the root, then GROUP BY subsector HAVING bool_and(test); (b) exact depth culling: drop linedefs whose both endpoints have depth dot(A - P, F) < 1 (no hit can have t >= 1); (c) static per-map data (line geometry, both sides' sector data, base texture colors) in TEMP tables built once per connection when render.sql loads (no schema change, existing databases keep working; TEMP tables work on read-only connections, verified); (d) cheaper final grid/best/encoding. Output must stay byte-identical: golden hashes and all exact-pixel tests unchanged.
