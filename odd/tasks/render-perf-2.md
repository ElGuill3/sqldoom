# Feature: render-perf-2

Second performance pass on the textured first-person renderer (Part 4a), before sprites (Part 4b).
Frames must stay byte-identical (golden hashes in tests/test_doom1_wad.py and every exact-pixel test).

## Specs

- S1: "si, procede con la pasada de rendimiento" -> accepted proposal: same method as the first pass (per-stage profile, attack the most expensive stage, byte-identical frames), target ~30-35 ms per 160x50 frame on the user's laptop (~25-30 fps).
- S2: Evidence that drives it, measured by the user with sqldoom-bench on the laptop: "sql per frame avg 76.2 ms min 65.7 max 102.0 (13.1 fps)", "terminal per frame avg 4.7 ms min 3.8 max 6.7 (148041 bytes)", "viewer estimate 12.4 fps", "duckdb threads 8 (cpu count 8)". The SQL is ~94% of the frame, so the terminal output is out of scope.

## Tasks

- T1 [S1,S2] Profile, optimize render.sql stage by stage, byte-identical frames, report before/after with sqldoom-bench. route: worker. commit: pending
- T2 [S1] README Performance numbers, close. route: inline. commit: pending

## Log

- L1: "si, procede con la pasada de rendimiento"
- L2: Known candidates from Part 4a (odd/tasks/textures.md L5, worker profile): per-pixel lighting join with render_lit ~13 ms; flats' 4096-element list_extract ~5 ms; geometry pipeline (hits, windows, spans) ~26 ms; final grid + ANSI encoding. Lessons: algebraically equal float reorderings move pixels (keep expression order for anything rounded); per-pixel joins carrying LIST columns copy the lists; count(*) prunes projections and misleads stage profilers (materialize the stage, e.g. SELECT sum(hash(*)), when timing).
- L3: T1 result (partial): texel atlas offsets instead of carried texture lists in spans, shared lighting lookup array instead of a per-pixel join, packed-color ANSI pairing. Byte-identical (234 differential comparisons, 9 maps, fixed seed 20260403; 152 tests). Controlled interleaved E1M1 160x50: 92.2 -> 79.2 ms (~14%); E1M3 111.1 -> 106.3; E1M6 110.7 -> 98.5. Target (~half) NOT met. All measurements under load 4-5 from an unrelated long-running job (polymarket-data build_r2_layout.py, ~2 cores). Remaining cost: geometry + windows ~33 ms cumulative, final ANSI ~18 ms on top of the framebuffer. Rejected with numbers: shared span joins, integer hit rank, list-based BSP paths, wide window rows.
