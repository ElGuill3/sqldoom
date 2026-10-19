# Feature: sprites

Part 4b of SQLDoom-from-scratch on DuckDB: draw the map's things (monsters, items, decorations) as Doom sprites in the
first-person view, correctly hidden behind walls and steps. Things stay static (no animation, no AI): Part 5.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando"
- S2: "si, por ahora sigamos, luego vemos si podemos optimizar aun mas" -> continue with Part 4b as announced: "sumar ... los sprites (enemigos, items y decoraciones)".

## Tasks

- T1 [S2] Importer: sprite pictures (P1) and the thing type table (P2). route: worker. commit: pending
- T2 [S2] Renderer: project, rotate, light and clip sprites (P3-P7); measure. route: worker. commit: pending
- T3 [S1,S2] README Part 4b, goldens after a visual check, screenshots. route: inline. commit: pending

## Log

- L1: "si, por ahora sigamos, luego vemos si podemos optimizar aun mas"
- L2: Design (agent). Decoding stays in the Python importer; rendering stays in SQL.
  - P1 Sprites: every lump strictly between S_START and S_END (skip 0-byte markers) is a picture (same format as wall patches: width, height, left offset, top offset, column posts). Tables: `sprites(name PRIMARY KEY, width, height, left_offset, top_offset)` and `sprite_columns(sprite, u, pixels UTINYINT[])` with PRIMARY KEY (sprite, u), one row per column, `pixels` length = height, NULL elements where no post covers the texel (transparent). A malformed sprite picture is CorruptWad (existing error message). A WAD without S_START/S_END imports with empty sprite tables.
  - P2 Thing types: table `thing_types(type PRIMARY KEY, sprite VARCHAR(4), frame VARCHAR(1), full_bright BOOLEAN)`, loaded by the importer from a static Python table that follows Doom's info.c spawn states, for every type used by the shareware maps: 5 BKEY A, 6 YKEY A, 8 BPAK A, 9 SPOS A, 10 PLAY W, 12 PLAY W, 13 RKEY A, 15 PLAY N, 24 POL5 A, 34 CAND A bright, 35 CBRA A bright, 46 TRED A bright, 48 ELEC A, 58 SARG A, 2001 SHOT A, 2002 MGUN A, 2003 LAUN A, 2005 CSAW A, 2007 CLIP A, 2008 SHEL A, 2010 ROCK A, 2011 STIM A, 2012 MEDI A, 2013 SOUL A bright, 2014 BON1 A, 2015 BON2 A, 2018 ARM1 A, 2019 ARM2 A, 2024 PINS A bright, 2025 SUIT A bright, 2026 PMAP A bright, 2028 COLU A bright, 2035 BAR1 A, 2045 PVIS A bright, 2046 BROK A, 2048 AMMO A, 2049 SBOX A, 3001 TROO A, 3002 SARG A, 3003 BOSS A, 3004 POSS A. Not drawn (absent from the table): player starts 1-4, deathmatch start 11, teleport destination 14. Spectres (58) are drawn as normal demons until a fuzz effect exists.
  - P3 Which things: map things whose type is in `thing_types`, whose flags include 2 (Doom's default skill, "Hurt me plenty") and not 16 (multiplayer only), and whose sprite picture resolves (P4); others are skipped silently.
  - P4 Rotation: lumps are named SPRITE + FRAME + ROTATION [+ FRAME + ROTATION] (e.g. `TROOA1`, `TROOA2A8`, `BAR1A0`). Rotation 0 serves every angle. Otherwise, with the camera at P and the thing at T facing `thing.angle` degrees, `view = degrees(atan2(P.y - T.y, P.x - T.x))`, `rot = floor(((view - thing.angle + 360 + 22.5) mod 360) / 45) + 1` (1 = facing the camera, counterclockwise around the thing). A lump's second frame/rotation pair names the same picture mirrored horizontally (`TROOA2A8` is rotation 2, and rotation 8 when mirrored).
  - P5 Projection (camera conventions D1-D2 of odd/tasks/first-person.md): `d = T - P`, depth `tz = dot(d, F)`, lateral `tx = dot(d, R)`; skip when `tz < 4` (Doom's MINZ). Screen center column `cx = width/2 + tx * f / tz`, scale `s = f / tz`. Sprite column u covers screen columns where `x + 0.5` lies in `[cx + (u - left_offset) * s, cx + (u + 1 - left_offset) * s)` (mirrored: u' = width - 1 - u). Vertical: the thing stands on the floor of its sector (`point_sector`): `top = floor_height + top_offset`; pixel row y (center y + 0.5) maps to texel `v = floor((top - z) )` with `z = eye - (y + 0.5 - height/2) * tz / f`; rows with v outside `[0, height)` or a NULL texel are transparent.
  - P6 Clipping: a sprite pixel in column x is visible only inside the window left by the wall hits of that column that are nearer than the sprite (`t < tz`), computed exactly like D6's windows (max opening top, min opening bottom over those hits; a nearer one-sided hit hides the whole column). Visible sprite pixels replace whatever the column drew there; where sprites overlap, the nearest sprite (smallest tz, then thing id) wins.
  - P7 Lighting: sprites use the light of the thing's sector with the wall rule of G7 (scale `least(47, floor(2560 / tz))`, no fake contrast), or colormap 0 when `full_bright`. Surface name `sprite` (new value in render_pixels.surface).
  - P8 Contract: render_pixels/render_rgb/render_ansi and the CLIs keep their signatures. A database imported before Part 4b (no `sprites` table) renders without sprites rather than failing, and sqldoom-render/view print nothing extra.
