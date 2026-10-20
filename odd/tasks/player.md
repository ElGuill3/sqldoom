# Feature: player

Part 5a of SQLDoom-from-scratch on DuckDB: the player's state and movement live in SQL. Every game tic is one SQL
statement that reads the input, applies Doom's momentum and friction, and resolves collisions against the map.
Doors, animation and enemies come in later parts.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando"
- S2: "vamos con el estado del jugador" -> the announced step: "mover el estado del jugador a SQL, con colisiones contra las paredes".

## Tasks

- T1 [S2] SQL game state: TEMP `player` table, `game_step` macro (M1-M6), tests. route: inline (worker failed). commit: see L3 [done]
- T2 [S2] Viewer runs a fixed 35 Hz tic loop over game_step (M7). route: worker. commit: pending
- T3 [S1,S2] README Part 5a, close. route: inline. commit: pending

## Log

- L1: "vamos con el estado del jugador"
- L2: Design (agent).
  - M1 State: TEMP table `player(map VARCHAR, x DOUBLE, y DOUBLE, z DOUBLE, angle DOUBLE, momx DOUBLE, momy DOUBLE, tic BIGINT)` with one row, created by a `game_start(map_name)` step from the map's player 1 start (z = floor height of its sector, momentum 0, tic 0). TEMP keeps the imported database read-only and untouched; state resets every session.
  - M2 Tic: `game_step(forward, side, turn, run)` is a TEMP table macro returning the next player row from the current one; the caller applies it with one `UPDATE player SET ... FROM game_step(...)`. Inputs are -1, 0 or 1 (forward/back, strafe right/left, turn left/right) and a run boolean. Order per tic, as in Doom's P_PlayerThink then P_XYMovement: turn, thrust, move with collision, friction.
  - M3 Doom constants (fixed point converted to map units): turn per tic 3.515625 degrees walking (angleturn 640), 7.03125 running (1280); thrust per tic 0.78125 walking (forwardmove 25 * 2048 / 65536), 1.5625 running; strafe uses the same thrust along the right vector; momentum is clamped to 30 units per tic per axis (MAXMOVE); friction multiplies momentum by 0.90625 after the move; a component with absolute value below 0.0625 (STOPSPEED) becomes 0 when there is no input on any axis.
  - M4 Position check at (px, py) with radius 16 (player radius) and height 56: every linedef whose segment distance to the point is < 16 "touches" the player. A touching one-sided line or a line with flag 1 (impassable) blocks. Otherwise floorz = max(floor of the point's sector, floors of both sectors of every touching two-sided line), ceilingz = min of the ceilings likewise. The position is blocked when ceilingz - floorz < 56, or floorz - z > 24 (step too high). On success the player's z becomes floorz (Doom stands things on the highest floor they touch). This uses a circle instead of Doom's square box: a documented simplification.
  - M5 Moving: if the move this tic is longer than 16 on either axis, it is done in two half steps, each checked (Doom splits moves larger than the radius). For each step: try (x+dx, y+dy); if blocked try (x+dx, y), then (x, y+dy); a blocked axis sets its momentum to 0; if all three are blocked the player stays and both momenta are 0. This is Doom's stairstep fallback of P_SlideMove, without its wall-parallel sliding: a documented simplification.
  - M6 Contract: render, render_ansi, sqldoom-render and sqldoom-bench are unchanged. game_step is a pure function of the player row and the inputs, so tests can call it directly.
  - M7 Viewer: sqldoom-view keeps its CLI. It runs Doom's fixed 35 Hz tics from a clock: before each frame it runs every tic that is due (game_step via UPDATE), then renders once from the player row, so slow frames run several tics and the speed of the game does not depend on the frame rate. Terminals do not report key releases, so a key counts as held for 150 ms after its last press or auto-repeat. Keys: w/s or Up/Down forward/back, a/d or Left/Right turn, , and . strafe left/right, Shift (uppercase W/A/S/D) runs, q/Esc quits. The status line shows x, y, z, angle, speed (units per tic) and fps. --pos/--angle still set the start.
- L3: T1 evidence (parent, inline: the worker run failed with a provider error before writing anything, and T1 is one unit). 25 synthetic tests in tests/test_game.py plus an E1M1 run test (200 running tics, never closer than 16 to a blocking line). Interpretations: turn = 1 turns left (counterclockwise), side = 1 strafes right; the stop rule applies per axis after friction; the point's sector comes from a ray cast east (nearest crossed line, side by cross product), not the renderer's BSP walk; a start outside the map raises `position is outside the map: X,Y`.
- L4: T1 performance (E1M1, load ~2). A chain of table macros (game_try_move -> game_check -> game_sector, LATERAL per candidate) took 28 ms per tic. One query that evaluates every point a tic may need (3 first-step candidates + 3 second-step candidates from each of the 4 first outcomes) in one join with the lines, then picks the outcome: 11.7 ms, same trajectories. Profile: execution ~4 ms; the rest is planning (binding 2.3 ms, optimizers 2.6 ms) and thread scheduling (threads=1: 8.1-8.7 ms). PREPARE/EXECUTE, typed or not, does not help; neither does a scalar arg_min key. Open for T2: the viewer pays ~10 ms per tic, ~35 % of a second at 35 Hz.
