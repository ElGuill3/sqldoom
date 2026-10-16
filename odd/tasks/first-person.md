# Feature: first-person

Part 3 of SQLDoom-from-scratch on DuckDB: first-person view (walls, flat-colored floors/ceilings) rendered by SQL,
shown in the terminal with truecolor half-blocks, plus a walk-around viewer. Textures/sprites are Part 4, game logic Part 5.

## Specs

- S1: "vamos por partes, pero tu implementas y vas explicando"
- S2: "si, todavia no se puede probar verdad?" -> Part 3 must give the user something to try: an interactive viewer.
- S3: "oye si, ese es otro detalle, aca estoy en wsl, se podria hacer en terminal?" -> the viewer runs in the terminal, not a GUI window.
- S4: "aca estoy con wezterm" -> target WezTerm: 24-bit color, synchronized output (DEC mode 2026).

## Tasks

- T1 [S1] `point_sector` macro: find the camera's sector by walking the BSP tree with a recursive CTE. route: worker A. commit: 574fb0e (+ refactor ad84376) [done]
- T2 [S1,S2] `render_pixels`/`render_rgb` macros (Design D1-D7) + `sqldoom-render` PNG output. route: worker A. commit: 574fb0e [done]
- T3 [S2,S3,S4] `render_ansi` macro (D8) + `sqldoom-render` terminal output + `sqldoom-view` interactive viewer (D9). route: worker B. commit: 5b58674 [done]
- T4 [S1] README Part 3 section, frame-time numbers, golden hashes. route: inline. commit: (this commit, see git log) [done]

## Log

- L1: "si, todavia no se puede probar verdad?"
- L2: "oye si, ese es otro detalle, aca estoy en wsl, se podria hacer en terminal?"
- L3: "aca estoy con wezterm"
- L4: Design (decided by the agent; the renderer lives in `src/sqldoom/sql/render.sql` as DuckDB TEMP macros, like `automap.sql`):
  - D1 Conventions: world angles in degrees, 0 = east, 90 = north (counterclockwise), same as THINGS. Screen `width` x `height` pixels, y grows downwards. FOV 90 degrees: focal `f = width / 2`, used for both axes (square pixels).
  - D2 Camera rays: forward `F = (cos a, sin a)`, right `R = (sin a, -cos a)`. Column `c` has `u = (c + 0.5 - width / 2) / f` and ray `P + t * (F + u * R)`. D is NOT normalized, so `t` is already the perpendicular depth (no fisheye correction).
  - D3 Hits: every linedef of the map against every column (set-based; no BSP needed for ordering because SQL sorts per column). Segment `A + s * (B - A)`. With `D = F + u*R`, `E = B - A`, `w = A - P`, `cross(p, q) = p.x*q.y - p.y*q.x`, `den = cross(D, E)`: skip `den = 0`; `t = cross(w, E) / den`, `s = cross(w, D) / den`; hit when `t >= 1` (near plane, world units) and `0 <= s <= 1`.
  - D4 Sides: Doom's front side is to the right of v1->v2: camera is on the front side when `cross(E, P - A) < 0`. One-sided linedef (`back_sidedef IS NULL`): solid, near sector = front sector. Two-sided: near = sector of the side facing the camera, far = the other.
  - D5 Camera height: `eye = floor_height(point_sector(map, x, y)) + 41` (Doom VIEWHEIGHT). `point_sector` walks the BSP from the root node (highest node id): go to `right_child` (front) when `(py - n.y) * n.dx < n.dy * (px - n.x)`, else `left_child`, until a subsector; its first seg -> linedef, seg.direction (0 = front sidedef, 1 = back) -> sidedef.sector. A map without nodes is subsector 0.
  - D6 Projection and clipping per column, hits ordered by `t` (nearest first): `y(z) = height / 2 - (z - eye) * f / t`. `cy_n, fy_n` = near ceiling/floor, `cy_f, fy_f` = far. Solid: wall `[cy_n, fy_n)`, closes the column. Two-sided: upper wall `[cy_n, cy_f)` when `cy_f > cy_n` (skipped when both ceilings are `F_SKY1`), lower wall `[fy_f, fy_n)` when `fy_f < fy_n`; opening `[greatest(cy_n, cy_f), least(fy_n, fy_f))`. Visible window of a hit = `[max(0, max opening top of nearer hits), min(height, min opening bottom of nearer hits))` (window functions, ROWS UNBOUNDED PRECEDING AND 1 PRECEDING). Each hit draws, clipped to its window: near ceiling `[window top, cy_n)`, its walls, near floor `[fy_n, window bottom)`. Hits behind the first solid wall, or with an empty window, draw nothing. A span `[a, b)` covers pixel rows `ceil(a - 0.5)` .. `ceil(b - 0.5) - 1`. If two spans still claim one pixel, the nearest hit wins. Uncovered pixels are black.
  - D7 Colors (flat until Part 4 textures): base RGB from the texture name: `md5(name)` hex bytes 1-3, each mapped to `64 + byte % 160`. Wall texture = middle (solid), upper, lower of the near-side sidedef; NULL texture -> name `-`. `F_SKY1` ceilings are `(100, 150, 220)`, unshaded. Walls are shaded by `light_level / 255 * greatest(0.35, 1 - t / 1600)`, flats by `light_level / 255` only; channels rounded and clamped to 0..255.
  - D8 Terminal frame: two pixel rows per text row with `▀` (foreground = top pixel, background = bottom pixel), 24-bit SGR codes `ESC[38;2;R;G;Bm` / `ESC[48;2;R;G;Bm`, emitted only when the color changes from the previous cell in the row (`lag()`), `ESC[0m` and `\n` at the end of each row. For terminal output `--size` is in characters (pixels = cols x rows*2); for PNG it is in pixels.
  - D9 Viewer: Python owns only I/O and the camera (Part 5 moves player state into SQL): raw terminal (termios/tty, stdlib only), alternate screen, hidden cursor, synchronized output, always restored on exit or error. Keys: `w`/Up forward 16 units, `s`/Down back 16, `a`/Left turn left 7.5 deg, `d`/Right turn right 7.5 deg, `q`/Esc quit. No collisions (noclip). Status line with map, x, y, angle, fps and key help.
- L5: Evidence: E1M1 from the player start renders in ~110 ms at 160x100 (8.9 fps viewer at 160x50 chars, ~44 KB per terminal frame) and ~160 ms at 320x200. Golden sha256 tests pin the E1M1 PNG and the 80x25 terminal frame. Worker decision on D9: raw input only (OPOST/ONLCR kept) so the bare `\n` row terminators return to column 1.
