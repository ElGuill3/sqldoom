# sqldoom

Doom running inside [DuckDB](https://duckdb.org), built from scratch, one part at a time.

| Part | Goal | Status |
|------|------|--------|
| 1 | Import a WAD file into relational tables | done |
| 2 | Draw a top-down map with SQL | done |
| 3 | First-person view + terminal viewer | done |
| 4a | Doom's palette, lighting, wall textures, flats, sky | done |
| 4b | Sprites: monsters, items, decorations | next |
| 5 | Game logic: collisions, doors, enemies | planned |

## Setup

Requires [uv](https://docs.astral.sh/uv/). DuckDB has no wheels for Python 3.15 yet, so the project pins 3.12.

```sh
uv sync
```

### Getting doom1.wad

The shareware `doom1.wad` (episode 1) is freely redistributable but not committed here. The Debian package
contains it:

```sh
mkdir -p wad && cd wad
curl -fsSLO http://deb.debian.org/debian/pool/non-free/d/doom-wad-shareware/doom-wad-shareware_1.9.fixed-5_all.deb
mkdir x && dpkg-deb -x doom-wad-shareware_*.deb x && find x -iname '*.wad' -exec cp {} . \; && rm -rf x *.deb
sha1sum doom1.wad   # 5b2e249b9c5133ec987b3ea77596381dc0d6bc1d (v1.9)
```

## Part 1: importing a WAD

```sh
uv run sqldoom-import wad/doom1.wad doom1.duckdb
# imported 1264 lumps and 9 maps into doom1.duckdb
```

```
usage: sqldoom-import [-h] [--force] wad database
```

- `wad`: an IWAD or PWAD file.
- `database`: the DuckDB file to create. The command refuses to overwrite it unless you pass `--force`.

Errors go to stderr and exit with code 1:

| Message | Cause |
|---------|-------|
| `error: file not found: <path>` | The WAD path does not exist. |
| `error: not a WAD file: <path>` | The file does not start with `IWAD`/`PWAD`. |
| `error: corrupt WAD file: <path>` | The directory or a lump points outside the file, a lump has a partial record, or the graphics data is malformed (bad texture/patch offsets, a PNAMES name with no lump, a non-4096-byte lump between F_START and F_END). |
| `error: database already exists: <path> (use --force to replace)` | The output exists and `--force` was not given. |

The import fails before it writes anything. With `--force`, the new database is built in a scratch file and swapped
in at the end, so a failed import never damages an existing database.

### The WAD format in one minute

A WAD is a flat archive of named blobs called *lumps*. It starts with a 12-byte header (`IWAD`, lump count,
directory offset) and ends with a directory of 16-byte entries (offset, size, 8-char name). A map is an empty marker
lump (`E1M1`) followed by its data lumps. Each data lump is an array of fixed-size records that point at each other
by array index.

### Schema

Every map table is keyed by `(map, id)`, where `id` is the record's index in its lump.

| Table | One row per | Key columns |
|-------|-------------|-------------|
| `lumps` | directory entry, with raw bytes in `data` | `idx`, `name`, `filepos`, `size` |
| `maps` | map marker | `name`, `lump_idx` |
| `vertexes` | 2D point | `x`, `y` |
| `linedefs` | wall line between two vertexes | `v1`, `v2`, `front_sidedef`, `back_sidedef` (NULL = one-sided wall) |
| `sidedefs` | one face of a linedef | textures (NULL = `-`, none), `sector` |
| `sectors` | area with floor/ceiling heights | `floor_height`, `ceiling_height`, `light_level` |
| `things` | monster, item or player start | `x`, `y`, `angle`, `type` |
| `segs` | piece of a linedef produced by the BSP builder | `v1`, `v2`, `angle` (0..65535), `linedef`, `direction` |
| `ssectors` | convex subsector (BSP leaf) | `first_seg`, `seg_count` |
| `nodes` | BSP split line | partition `x, y, dx, dy`, bounding boxes, `right_child`/`left_child` + `*_is_ssector` |
| `palette` | PLAYPAL color (palette 0) | `idx`, `r`, `g`, `b` |
| `colormaps` | lighting level 0..33 (0 = full bright) | `colors` (256 palette indices) |
| `textures` | composite wall texture | `width`, `height` |
| `texture_columns` | texture column `u`, `pixels` being its palette indices (one per texel row) | `u`, `pixels` |
| `flats` | floor/ceiling texture (64x64) | `pixels` (palette indices, row-major) |

### Exploring it

```sh
duckdb doom1.duckdb    # or: uv run python -c "import duckdb; ..."
```

```sql
-- Biggest maps by wall count
SELECT map, count(*) AS walls FROM linedefs GROUP BY map ORDER BY walls DESC;

-- Every wall of E1M1 as a segment with real coordinates
SELECT l.id, a.x AS x1, a.y AS y1, b.x AS x2, b.y AS y2, l.back_sidedef IS NULL AS solid
FROM linedefs l
JOIN vertexes a ON a.map = l.map AND a.id = l.v1
JOIN vertexes b ON b.map = l.map AND b.id = l.v2
WHERE l.map = 'E1M1';

-- Player 1 start
SELECT x, y, angle FROM things WHERE map = 'E1M1' AND type = 1;
```

### Why the loader uses CSV

DuckDB is columnar, so per-row inserts are its worst case. On doom1.wad (~40k map records), row-by-row
`executemany` took ~80 s, and binding whole Python lists as `UNNEST` parameters still took ~28 s. Writing a temporary
CSV and running `COPY` (DuckDB's native, vectorized reader) takes about a second end to end. List columns (`UTINYINT[]`)
are worse still — UNNEST binding converts nested Python lists one value at a time (~155 s for the ~1M texture texels,
and superlinearly) — so the graphics tables also go through CSV, with each list serialized as a literal like `[1,2,3]`.

## Part 2: the automap, rendered by SQL

![E1M1 automap rendered by SQL](docs/e1m1.png)

```sh
uv run sqldoom-automap doom1.duckdb E1M1               # ASCII art in the terminal (120x60)
uv run sqldoom-automap doom1.duckdb E1M1 -o e1m1.png   # PNG image (640x400)
uv run sqldoom-automap doom1.duckdb e1m6 --size 200x90 # map names are case-insensitive
```

```
usage: sqldoom-automap [-h] [--size WxH] [-o OUTPUT] database map
```

- `--size WxH`: characters for ASCII, pixels for PNG. Both values must be between 8 and 4096.
- `-o OUTPUT`: write a PNG instead of printing ASCII. Prints `wrote <path> (WxH)`.

Errors exit with code 1 and leave no output file: `error: file not found: <path>`, `error: map not found: <MAP>`,
`error: not a sqldoom database: <path>` and `error: cannot write <path>: <strerror>`. An invalid `--size` is a
usage error (exit code 2).

| Line kind | Rule | ASCII | PNG |
|-----------|------|-------|-----|
| Wall | one-sided linedef | `#` | red |
| Floor step | two-sided, floor heights differ | `+` | brown |
| Ceiling step | two-sided, only ceiling heights differ | `=` | yellow |
| Plain | two-sided, same heights | `.` | gray |
| Player 1 start | thing type 1 | `@` | green cross |

When two kinds land on the same pixel, the one higher in the table wins.

### How SQL draws it

The renderer is `src/sqldoom/sql/automap.sql`: DuckDB `TEMP` table macros that Python loads and calls. Python never
touches a pixel. It only prints the text SQL returns, or wraps the RGB bytes SQL returns in a PNG.

1. **Fit**: the map's bounding box and a single `scale` fit the map into the screen with a 1-pixel margin, keeping
   proportions. World y grows upwards and screen y grows downwards, so the formula uses `max_y - y`. Terminal cells
   are about twice as tall as they are wide, so ASCII mode uses a pixel `aspect` of 2.
2. **Classify**: each linedef is joined to its sidedefs and sectors to get its kind.
3. **Rasterize**: SQL has no loops, but `LATERAL generate_series(0, n)` fans each line out into one row per
   pixel. This is the DDA algorithm: `n = max(|dx|, |dy|)` steps, each one rounded to a pixel.
4. **Resolve**: `GROUP BY x, y` with `arg_min(kind, priority)` keeps one kind per pixel.
5. **Fill**: the full `width x height` grid comes from a cross join of two `generate_series`. A `LEFT JOIN` leaves
   empty pixels as background.
6. **Encode**: `string_agg(... ORDER BY y, x)` serializes the grid, as characters for ASCII or as hex RGB that
   `unhex()` turns into a single BLOB of `width * height * 3` bytes.

You can call the macros yourself. In the `duckdb` CLI, load the file with `.read`; from Python, use
`con.execute(open("src/sqldoom/sql/automap.sql").read())`.

```sql
.read src/sqldoom/sql/automap.sql
SELECT kind, count(*) FROM automap_pixels('E1M1', 640, 400, 1.0) GROUP BY kind;
SELECT text FROM automap_ascii('E1M1', 100, 45);
```

## Part 3: the first-person view, in your terminal

![E1M1 from the player start](docs/e1m1_view.png) ![E1M1 looking out of a window](docs/e1m1_view_window.png)

```sh
uv run sqldoom-view doom1.duckdb E1M1                                  # walk around (noclip)
uv run sqldoom-render doom1.duckdb E1M1                                # print one frame (160x50 characters)
uv run sqldoom-render doom1.duckdb E1M1 -o view.png                    # PNG (320x200 pixels)
uv run sqldoom-render doom1.duckdb E1M1 --pos 1056,-3200 --angle 45 -o window.png
```

The viewer needs a terminal with 24-bit color, such as WezTerm or Windows Terminal. Keys: `w`/`s` or Up/Down move
16 units, `a`/`d` or Left/Right turn 7.5 degrees, `q`/Esc quits. There are no collisions yet (Part 5), so you can
walk through walls. The status line shows the position, angle and frames per second. Without `--size` the frame
follows terminal resizes (drag the window edge and it redraws at the new size), and the terminal is restored even
if the viewer is killed with SIGTERM or SIGHUP (for example by closing the tab).

```
usage: sqldoom-render [-h] [--size WxH] [--pos X,Y] [--angle DEG] [-o OUTPUT] database map
usage: sqldoom-view [-h] [--size COLSxROWS] [--pos X,Y] [--angle DEG] database map
```

- Without `--pos`/`--angle` the camera is the player 1 start.
- `--size` counts characters for terminal output (one character = 2 pixels stacked) and pixels for PNG. Both values
  must be between 8 and 4096. The viewer defaults to the terminal size (minus one status row) and, without
  `--size`, keeps following terminal resizes.
- Errors are the automap ones, plus `error: map has no player start: <MAP>` and
  `error: sqldoom-view needs an interactive terminal`.

Walls, floors, ceilings and the sky use Doom's own textures and lighting (Part 4a below). Databases imported before
Part 4a have no textures; the renderer then stops with `error: database has no textures: re-run sqldoom-import --force`.

### How SQL renders a frame

Everything is in `src/sqldoom/sql/render.sql`. Python reads keys, keeps the camera position and writes the text that
SQL returns.

1. **Where am I?** `point_sector` finds the camera's subsector in the BSP tree, and so its sector and eye height
   (floor + 41, as in Doom). Each node is a split line and one cross product decides front or back; the paths from
   the root are precomputed once (see Performance).
2. **Cast one ray per column.** Column `c` gets the direction `forward + u * right`, with `u` from -1 to 1 for a
   90 degree field of view. The direction is not normalized on purpose: the ray parameter `t` is then already the
   perpendicular depth, so there is no fisheye correction to do.
3. **Hit every wall at once.** A cross join of columns x linedefs, solved with 2D cross products, gives every
   ray/wall intersection: about 76,000 candidate pairs on E1M1 at 160 columns. Doom needed the BSP tree to draw front to
   back because sorting was too slow on a 486. In SQL, `ORDER BY t` per column is cheap.
4. **Project.** A height `z` at depth `t` lands on screen row `height/2 - (z - eye) * f / t`.
5. **Clip with window functions.** Walking each column front to back, every two-sided wall leaves an opening
   (between the lower ceiling and the higher floor). `max`/`min` over the preceding rows give the part of the column
   still visible for each hit. That is how steps, windows and ledges hide what is behind them. The first one-sided
   wall closes the column.
6. **Fill spans.** Each hit paints its sector's ceiling above its walls and its floor below them, inside its window.
   `generate_series` turns the spans into pixels, and each pixel gets its texel and light (Part 4a).
7. **Encode.** `render_rgb` returns RGB bytes for PNG. `render_ansi` packs two pixel rows into one text row with
   `▀` (foreground = top pixel, background = bottom pixel). `lag()` emits a 24-bit color code only when the color
   changes from the previous cell.

### Performance

Measured in WSL2 on the development machine, E1M1 from the player start renders a default terminal frame (160x50
characters = 160x100 pixels) in ~33 ms, about 30 fps, on an idle machine; E1M6 takes ~41 ms. A terminal frame is
about 44 KB of text. Loading `render.sql` costs ~100-150 ms once per connection.

The first version needed ~80 ms per frame. A per-CTE profile (cut the CTE chain after each stage and time a
`count(*)`) showed that most of the time went into recomputing data that never changes, so the renderer now:

- answers "which sector am I in?" from precomputed BSP paths: each subsector is the intersection of the half-planes on
  its path from the root, so the lookup is a `GROUP BY subsector HAVING bool_and(test)` instead of a recursive walk;
- keeps line geometry, both sides' sector data and the lit palette in `TEMP` tables built once per connection;
- drops walls entirely behind the camera before the column x wall cross join. This is exact: if both endpoints are
  closer than the near plane, no ray can hit the wall;
- resolves pixels and terminal cells with aggregates instead of window functions.

Measured side by side on the same busy machine, the speedup was ~2.6x for terminal frames and ~1.9x for 320x200 PNGs,
with every frame byte-identical to the first version (golden hashes and differential checks).

## Part 4a: Doom's palette, lighting and textures

Re-import your database first: `uv run sqldoom-import wad/doom1.wad doom1.duckdb --force`. Then the same commands
(`sqldoom-view`, `sqldoom-render`) show the real game graphics.

### What the importer adds

- `palette`: PLAYPAL's first palette, 256 RGB colors. Every pixel in Doom is an index into it.
- `colormaps`: COLORMAP's 34 tables of 256 entries. Table 0 is full bright and 31 is nearly black; lighting is
  "replace each palette index by a darker one", which is how a 1993 game did shading without arithmetic per pixel.
- `textures` and `texture_columns`: wall textures are not stored as images. TEXTURE1 describes each one as patches
  (PNAMES) placed at offsets, and each patch is a column-oriented picture made of "posts" (vertical runs of pixels).
  The importer composes them once and stores each texture column as a list of palette indices.
- `flats`: floor and ceiling textures, 64x64 raw palette indices, between `F_START` and `F_END`.

The shareware PNAMES names 187 patches that only exist in the registered game. No shareware texture uses them, so the
importer only checks the patches a texture actually draws.

### How a pixel gets its color

- **Walls**: the horizontal texel is the distance along the linedef from its start (from its end when you see the
  back side) plus the sidedef's x offset. The vertical texel comes from the world height of the pixel row and Doom's
  pegging rules: by default an upper texture is anchored at its bottom edge (the lower ceiling) and a lower texture
  at its top edge (the top of the step), and the linedef flags "upper unpegged" (8) and "lower unpegged" (16) change the anchor, which is how door frames
  and windows line up in the original maps.
- **Floors and ceilings**: for each pixel row the depth of the plane is `(h - eye) * f / (height/2 - y - 0.5)`, which
  gives a world point per pixel; its coordinates modulo 64 pick the flat texel.
- **Sky**: `F_SKY1` ceilings show `SKY1` by ray angle (four repeats per turn, as in Doom), always full bright.
- **Light**: as in Doom, the sector light picks a starting colormap, walls get darker with distance
  (`2560 / t` steps) and flats with depth, and perfectly horizontal or vertical walls are one step darker or
  brighter (Doom's "fake contrast"). The final color is `palette[colormaps[level][texel]]`, precomputed as one lookup
  table per light level.

### Cost

Textures moved a default terminal frame of E1M1 from ~39 ms to ~48-50 ms on this machine, about 20 fps. The frame
text grew from ~44 KB to ~148 KB, because textured cells change color almost every cell and the ANSI encoder can no
longer skip codes. Both are the next optimization targets.

### Measuring your machine

`sqldoom-bench` separates the two costs of the viewer: the SQL that computes a frame and the terminal that has to
display it. Run it in the terminal you play in:

```sh
uv run sqldoom-bench doom1.duckdb E1M1                 # 160x50 frame, 20 timed frames
uv run sqldoom-bench doom1.duckdb E1M1 --size 120x40 --frames 50
```

```
map E1M1  size 160x50 (160x100 pixels)  frames 20
load render.sql     114.6 ms
sql per frame       avg 49.2 ms  min 46.2  max 55.3  (20.3 fps)
terminal per frame  skipped (stdout is not a terminal)
viewer estimate     20.3 fps (sql only)
duckdb threads      8 (cpu count 8)
```

That run had stdout redirected. In a real terminal the fourth line becomes
`terminal per frame  avg <ms> ms  min <ms>  max <ms>  (<bytes> bytes)` and the estimate adds both costs. The
terminal line flashes the frame on the alternate screen and waits (`tcdrain`) until the terminal has read it.
When stdout is not a terminal (a pipe or a file) it says `skipped` and the estimate counts SQL only. If `sql per frame`
dominates, the renderer is the bottleneck; if `terminal per frame` does, the frame text is. DuckDB spreads each query
over its threads, so machines with fewer cores lose more. `--frames` accepts 1 to 1000; `--size`, `--pos` and
`--angle` work like in `sqldoom-render`.

## Tests

```sh
uv run pytest
```

Unit tests build tiny WADs byte by byte (`tests/wadbuilder.py`). The automap tests use a 180x160 room whose scale
works out exactly, so they compare the full ASCII output and decode the PNG to check individual pixels. `tests/test_doom1_wad.py` checks record counts and
cross-references on the real WAD. It is skipped when `wad/doom1.wad` is missing; set `SQLDOOM_WAD` to use another path.
