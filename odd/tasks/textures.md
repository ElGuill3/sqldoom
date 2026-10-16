# Feature: textures

Part 4a of SQLDoom-from-scratch on DuckDB: the first-person view with Doom's real graphics: PLAYPAL palette,
COLORMAP lighting, composite wall textures, flats and the sky. Sprites are Part 4b; game logic is Part 5.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando"
- S2: "continuemos con eso" -> Part 4 as announced: "que se vea como DOOM de verdad. Hay que decodificar la paleta (`PLAYPAL`) y la tabla de luces (`COLORMAP`), armar las texturas de pared a partir de sus *patches* (`PNAMES`/`TEXTURE1`), y sumar texturas de piso y techo y los sprites. Con unos 33 ms por cuadro hay margen, pero las texturas multiplican el trabajo por píxel, así que vamos a ir midiendo cada paso."

## Tasks

- T1 [S2] Importer: palette, colormaps, composite wall textures, flats as new tables (G1-G3). route: worker. commit: pending
- T2 [S2] Renderer: textured walls (G4), flats (G5), sky (G6), Doom lighting (G7); measure each step. route: worker. commit: pending
- T3 [S1,S2] README Part 4a, timings, refreshed golden hashes and screenshots. route: inline. commit: pending

## Log

- L1: "continuemos con eso"
- L2: Scope decision (agent): sprites move to Part 4b; they need depth sorting against walls per column.
- L3: Design (agent). Decoding stays in the Python importer, like Part 1; all rendering stays in SQL.
  - G1 Tables (additive, existing tables unchanged): `palette(idx 0..255, r, g, b)` from PLAYPAL palette 0; `colormaps(level 0..33, colors UTINYINT[256])` from COLORMAP; `textures(name, width, height)`; `texture_columns(texture, u, pixels UTINYINT[height])` (palette indices, one row per texture column); `flats(name, pixels UTINYINT[4096])` (row-major 64x64) for every 4096-byte lump between F_START and F_END. List columns keep the per-frame lookups to small joins plus `list_extract`.
  - G2 Composite textures: TEXTURE1 (and TEXTURE2 if present): int32 count, int32 offsets; each entry: name char[8], int32 masked, int16 width, int16 height, int32 obsolete, int16 patch count, then per patch int16 originx, int16 originy, int16 patch (PNAMES index), int16 stepdir, int16 colormap. PNAMES: int32 count, char[8] names. Patches use the picture format: int16 width, height, leftoffset, topoffset, int32 column offsets[width]; each column is posts of (byte topdelta, 0xFF ends the column; byte length; one padding byte; length bytes; one padding byte). Patches are drawn in order (later ones overwrite), clipped to the texture; texels no patch covers are palette index 0.
  - G3 Errors: the importer keeps its current messages; a WAD without PLAYPAL/COLORMAP/TEXTURE1/PNAMES still imports maps and leaves the graphics tables empty. The renderer reports `error: database has no textures: re-run sqldoom-import --force` (exit 1) when `textures` is missing or empty.
  - G4 Walls: horizontal texel `u = floor(dist + x_offset) mod width`, `dist` = distance along the linedef from v1 to the hit (`s * length`) when the near side is the front, from v2 (`(1 - s) * length`) when it is the back. Vertical: world height of pixel row y at depth t is `z = eye - (y + 0.5 - height/2) * t / f`; `v = floor(top - z + y_offset) mod texture height`, where `top` follows Doom pegging with linedef flags 8 (upper unpegged) and 16 (lower unpegged): middle wall: `top = near_ceiling`, or `near_floor + texheight` when flag 16; upper: `top = near_ceiling` when flag 8, else `far_ceiling + texheight`; lower: `top = near_ceiling` when flag 16, else `far_floor`. Missing texture name -> draw palette index 0.
  - G5 Flats: floor or ceiling at height h, pixel row y: `d = (h - eye) * f / (height/2 - y - 0.5)` is its depth (rows on the horizon are skipped), world point `P + d * (F + u * R)` with the column's u; texel `(floor(wx) mod 64, floor(-wy) mod 64)`, index `fy * 64 + fx`.
  - G6 Sky (`F_SKY1` ceilings): texture `SKY1`, `u = floor(ray_angle_deg * 1024 / 360) mod 256` with `ray_angle_deg = degrees(atan2(dy, dx))` of the column ray, `v = floor((y + 0.5) * 200 / height) mod 128`; full bright (colormap 0).
  - G7 Lighting like Doom: `light = light_level >> 4`; for walls only, `light - 1` when `ey = 0` (horizontal line) and `light + 1` when `ex = 0` (vertical line), as in Doom's r_segs.c fake contrast; clamp 0..15; `startmap = (15 - light) * 4`. Walls: `scale = least(47, floor(2560 / t))`, `level = startmap - scale // 2`. Flats: `j = least(127, floor(d / 16))`, `level = startmap - 80 // (j + 1)`. Clamp level to 0..31. Pixel RGB = `palette[colormaps[level][texel]]`.
  - G8 Output contract unchanged: `render_pixels` still returns `(x, y, surface, r, g, b)`; `render_rgb`, `render_ansi`, `sqldoom-render`, `sqldoom-view` unchanged. Golden hashes are re-recorded only after a visual check.
- L4: T1 evidence/decisions: doom1.wad PNAMES lists 187 patch names that have no lump (episode 2-3 graphics, registered WAD only) and no TEXTURE1 entry uses them, so a missing patch is corrupt only when a texture draws it (G3 amended). PLAYPAL/COLORMAP shorter than one palette/level load as empty tables. Nested-list parameter binding took ~200 s; lists load through CSV + COPY as DuckDB list literals (0.19 s). Import of doom1.wad 1.8 s -> 2.1 s; 125 textures, 10,776 texture columns, 54 flats.
