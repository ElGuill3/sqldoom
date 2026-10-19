-- First-person renderer, all SQL: which sector a point is in (T1) and the textured,
-- lit RGB framebuffer of the view from a camera (T2, odd/tasks/textures.md G4-G8).
-- Python only wraps the bytes in a PNG.
--
-- Conventions (design D1): world angles in degrees, 0 = east, 90 = north
-- (counterclockwise, same as THINGS); screen y grows downwards; FOV 90 degrees with
-- focal f = width / 2 used for both axes.
--
-- Per-frame work only picks texels and lighting: everything static is put into TEMP
-- tables when this file executes (built once per connection, for all maps, keyed by
-- map; no change to the database schema, and executing this file twice on one
-- connection rebuilds them with OR REPLACE). The BSP point_sector walks of D5 become
-- precomputed half-plane test chains (a), linedefs are culled by an exact near-plane
-- test (b), and the G7 lit palette (palette[colormaps[level][texel]], packed RGB per
-- level/texel) is materialized in render_lit so a pixel reads one list entry.

-- (a) The D5 walk of point_sector as data: for every map and subsector, the chain of
-- half-plane tests from the root node down to it. Each row is one test: the node's
-- split line (nx, ny, ndx, ndy) and which side the path took (front = the test came
-- out true, i.e. the walk went to the right child). Built once per connection; the
-- recursion here runs at load time, never per frame. A map without nodes produces no
-- rows and is handled by the subsector-0 fallback in point_sector.
CREATE OR REPLACE TEMP TABLE point_sector_tests AS
WITH RECURSIVE walk(map, ssector, node, is_node, tests) AS (
    -- Seed: every map's root node (highest node id), no test yet.
    SELECT map, NULL::INTEGER, max(id), true,
           []::STRUCT(nx INTEGER, ny INTEGER, ndx INTEGER, ndy INTEGER, front BOOLEAN)[]
    FROM nodes GROUP BY map
    UNION ALL
    -- Expand one node state into both children, recording the node's test and the
    -- chosen side; a subsector child ends its path (node NULL, ssector set).
    SELECT s.map, s.ssector, s.node, s.is_node, s.tests
    FROM (
        SELECT w.map,
               CASE WHEN ch.is_ss THEN ch.id ELSE w.ssector END AS ssector,
               CASE WHEN ch.is_ss THEN NULL ELSE ch.id END AS node,
               NOT ch.is_ss AS is_node,
               list_append(w.tests, struct_pack(nx := n.x, ny := n.y, ndx := n.dx,
                                                ndy := n.dy, front := c.is_right)) AS tests
        FROM walk w
        JOIN nodes n ON n.map = w.map AND n.id = w.node
        CROSS JOIN (VALUES (true), (false)) c(is_right),
        LATERAL (SELECT CASE WHEN c.is_right THEN n.right_child ELSE n.left_child END AS id,
                        CASE WHEN c.is_right THEN n.right_is_ssector ELSE n.left_is_ssector END AS is_ss) ch
        WHERE w.is_node
    ) s
)
SELECT leaves.map AS map, leaves.ssector AS ssector, sidedefs.sector AS sector,
       t.nx, t.ny, t.ndx, t.ndy, t.front
FROM (
    SELECT leaves.map, leaves.ssector, leaves.t,
           -- the sidedef id resolved here so the sidedefs join below stays a hash join
           CASE WHEN segs.direction = 0 THEN linedefs.front_sidedef
                ELSE linedefs.back_sidedef END AS side_id
    FROM (SELECT map, ssector, unnest(tests) AS t FROM walk WHERE NOT is_node) leaves
    JOIN ssectors ON ssectors.map = leaves.map AND ssectors.id = leaves.ssector
    JOIN segs ON segs.map = leaves.map AND segs.id = ssectors.first_seg
    JOIN linedefs ON linedefs.map = leaves.map AND linedefs.id = segs.linedef
) leaves
JOIN sidedefs ON sidedefs.map = leaves.map AND sidedefs.id = leaves.side_id;

-- T1 (D5) without recursion: a point is in a subsector exactly when it passes every
-- half-plane test of that subsector's path from the root. The test is the D5 walk's
-- comparison, kept verbatim (including the strict `<`, which decides points exactly
-- on a split line: the test comes out false, so only the left/front-false path
-- accepts them). The subsector's sector (via its first seg's linedef and the
-- sidedef picked by seg direction, as in D5) is resolved here at load time. A map
-- without nodes produces no rows and is handled by the subsector-0 fallback in
-- point_sector.
CREATE OR REPLACE TEMP MACRO point_sector(map_name, px, py) AS TABLE
SELECT sector
FROM (
    SELECT ssector, any_value(sector) AS sector, bool_and(
               CASE WHEN front THEN (py - ny) * ndx < ndy * (px - nx)
                    ELSE NOT ((py - ny) * ndx < ndy * (px - nx)) END) AS on_path
    FROM point_sector_tests
    WHERE map = map_name
    GROUP BY ssector
)
WHERE on_path
UNION ALL
SELECT sidedefs.sector
FROM ssectors
JOIN segs ON segs.map = map_name AND segs.id = ssectors.first_seg
JOIN linedefs ON linedefs.map = map_name AND linedefs.id = segs.linedef
JOIN sidedefs ON sidedefs.map = map_name AND sidedefs.id =
    CASE segs.direction WHEN 0 THEN linedefs.front_sidedef ELSE linedefs.back_sidedef END
WHERE ssectors.map = map_name AND ssectors.id = 0
  AND NOT EXISTS (SELECT 1 FROM nodes WHERE map = map_name);

-- (c) Static line geometry for all maps, built once per connection. length feeds the
-- G4 wall u (distance along the linedef); flags carry the pegging bits 8 (upper
-- unpegged) and 16 (lower unpegged).
CREATE OR REPLACE TEMP TABLE render_lines AS
SELECT linedefs.map, linedefs.id AS line_id,
       a.x AS ax, a.y AS ay, b.x AS bx, b.y AS by,
       b.x - a.x AS ex, b.y - a.y AS ey,
       sqrt(pow(b.x - a.x, 2.0) + pow(b.y - a.y, 2.0)) AS length,
       linedefs.flags,
       linedefs.back_sidedef IS NULL AS one_sided
FROM linedefs
JOIN vertexes a ON a.map = linedefs.map AND a.id = linedefs.v1
JOIN vertexes b ON b.map = linedefs.map AND b.id = linedefs.v2;

-- (c) Both sides of every linedef side by side, with their sidedef textures and
-- sector data, built once per connection (the same joins the per-frame sides CTE
-- used to do). front_side/back_side say whether that side's sidedef and sector both
-- resolved (the old per-frame INNER JOINs), so invalid maps keep the exact old
-- behavior: a hit whose near side did not resolve draws nothing.
CREATE OR REPLACE TEMP TABLE render_sides AS
SELECT linedefs.map, linedefs.id AS line_id,
       sf.id IS NOT NULL AND secf.id IS NOT NULL AS front_side,
       sf.middle_texture AS front_middle, sf.upper_texture AS front_upper,
       sf.lower_texture AS front_lower, sf.x_offset AS front_xoff,
       sf.y_offset AS front_yoff, secf.floor_height AS front_floor,
       secf.ceiling_height AS front_ceiling, secf.light_level AS front_light,
       secf.floor_texture AS front_ftex, secf.ceiling_texture AS front_ctex,
       sb.id IS NOT NULL AND secb.id IS NOT NULL AS back_side,
       sb.middle_texture AS back_middle, sb.upper_texture AS back_upper,
       sb.lower_texture AS back_lower, sb.x_offset AS back_xoff,
       sb.y_offset AS back_yoff, secb.floor_height AS back_floor,
       secb.ceiling_height AS back_ceiling, secb.light_level AS back_light,
       secb.floor_texture AS back_ftex, secb.ceiling_texture AS back_ctex
FROM linedefs
LEFT JOIN sidedefs sf ON sf.map = linedefs.map AND sf.id = linedefs.front_sidedef
LEFT JOIN sectors secf ON secf.map = linedefs.map AND secf.id = sf.sector
LEFT JOIN sidedefs sb ON sb.map = linedefs.map AND sb.id = linedefs.back_sidedef
LEFT JOIN sectors secb ON secb.map = linedefs.map AND secb.id = sb.sector;

-- (G7) One packed RGB lookup array, indexed by level * 256 + texel. A single-row
-- cross product reads the shared list without a per-pixel hash join.
-- Generate slots even for absent colormaps: they retain the old black fallback.
CREATE OR REPLACE TEMP TABLE render_lit AS
SELECT list(coalesce(p.r * 65536 + p.g * 256 + p.b, 0)
            ORDER BY levels.level, g.i) AS colors
FROM generate_series(0, 33) levels(level)
CROSS JOIN generate_series(0, 255) g(i)
LEFT JOIN colormaps ON colormaps.level = levels.level
LEFT JOIN palette p ON p.idx = list_extract(colormaps.colors, g.i + 1);

-- A single texel atlas: spans carry offsets, not pixel lists. In particular a
-- flat's 4096 entries must not travel through per-hit joins and pixel expansion.
-- Keep the original column/flat order within each image; coordinates below are
-- unchanged. NULL offsets still sample NULL (the missing-texture palette index 0).
CREATE OR REPLACE TEMP TABLE render_texture_columns AS
SELECT texture, u, len(pixels) AS tex_length,
       coalesce(sum(len(pixels)) OVER (
           ORDER BY texture, u ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)::BIGINT AS tex_offset
FROM texture_columns;

CREATE OR REPLACE TEMP TABLE render_flats AS
SELECT name,
       ((SELECT coalesce(sum(len(pixels)), 0) FROM texture_columns)
           + (row_number() OVER (ORDER BY name) - 1) * 4096)::BIGINT AS tex_offset
FROM flats;

CREATE OR REPLACE TEMP TABLE render_texels AS
SELECT flatten(list(pixels ORDER BY kind, name, u)) AS pixels
FROM (
    SELECT 0 AS kind, texture AS name, u, pixels FROM texture_columns
    UNION ALL
    SELECT 1, name, 0, CAST(pixels AS UTINYINT[]) FROM flats
);

-- T2 (S2, G4-G8): every visible pixel of the view from (px, py) at `angle` degrees, as
-- (x, y, surface, r, g, b) rows. Surfaces: 'ceiling', 'wall', 'upper', 'lower',
-- 'floor', 'sky', 'empty' (G8, unchanged). Colors come from the imported graphics:
-- pixel RGB = palette[colormaps[level][texel]] (G7) with the texel picked per G4
-- (walls), G5 (flats), or G6 (sky).
CREATE OR REPLACE TEMP MACRO render_pixels(map_name, px, py, angle, width, height) AS TABLE
WITH camera AS (
    -- D5: eye height is the floor of the camera's sector + 41 (Doom VIEWHEIGHT).
    SELECT radians(angle) AS rad, width / 2.0 AS f,
           (SELECT sectors.floor_height + 41
            FROM point_sector(map_name, px, py) ps
            JOIN sectors ON sectors.map = map_name AND sectors.id = ps.sector) AS eye
),
rays AS (
    -- D2: column c has u = (c + 0.5 - width/2) / f and direction D = F + u * R with
    -- F = (cos a, sin a), R = (sin a, -cos a). D is not normalized, so t is
    -- perpendicular depth. sky_u is the G6 sky column: floor(ray_angle_deg * 1024/360)
    -- mod 256 of the unnormalized column ray.
    SELECT g.c,
           (g.c + 0.5 - width / 2.0) / f AS u,
           cos(rad) + (g.c + 0.5 - width / 2.0) / f * sin(rad) AS dx,
           sin(rad) - (g.c + 0.5 - width / 2.0) / f * cos(rad) AS dy,
           ((CAST(floor(degrees(atan2(sin(rad) - (g.c + 0.5 - width / 2.0) / f * cos(rad),
                                      cos(rad) + (g.c + 0.5 - width / 2.0) / f * sin(rad)))
                         * 1024.0 / 360.0) AS BIGINT) % 256) + 256) % 256 AS sky_u
    FROM generate_series(0, width - 1) g(c) CROSS JOIN camera
),
lines AS (
    -- (b) Exact near-plane culling. For a hit H = P + t*D on the segment, t equals
    -- dot(H - P, F) with F = (cos a, sin a): D = F + u*R and dot(D, F) = F*F + u*F*R
    -- = 1. The depth dot(A - P, F) is affine in A, so when BOTH endpoints are behind
    -- the near plane (dot < 1) every point of the segment is, and no hit can satisfy
    -- t >= 1. The 1e-9 margin only absorbs rounding differences between this dot
    -- product and the cross-product t below, so culling can never drop a hit the
    -- exact t >= 1 test would keep.
    SELECT line_id, ax, ay, ex, ey, length, flags, one_sided
    FROM (
        SELECT render_lines.line_id, render_lines.ax, render_lines.ay,
               render_lines.ex, render_lines.ey, render_lines.length,
               render_lines.flags, render_lines.one_sided,
               greatest((render_lines.ax - px) * cos(camera.rad)
                            + (render_lines.ay - py) * sin(camera.rad),
                        (render_lines.bx - px) * cos(camera.rad)
                            + (render_lines.by - py) * sin(camera.rad)) AS max_depth
        FROM render_lines, camera
        WHERE render_lines.map = map_name
    )
    WHERE max_depth >= 1.0 - 1e-9
),
raw AS (
    -- D3: segment A + s * (B - A) against ray P + t * D, with w = A - P and
    -- cross(p, q) = p.x*q.y - p.y*q.x. cross(w, E) equals cross(E, P - A), so its sign
    -- also tells which side (D4: front) the camera is on.
    SELECT rays.c, lines.line_id, lines.one_sided, lines.ex, lines.ey, lines.length,
           lines.flags, rays.dx, rays.dy, rays.sky_u,
           rays.dx * lines.ey - rays.dy * lines.ex AS den,
           (lines.ax - px) * lines.ey - (lines.ay - py) * lines.ex AS cwe,
           (lines.ax - px) * rays.dy - (lines.ay - py) * rays.dx AS cwd
    FROM rays CROSS JOIN lines
),
hits AS (
    SELECT c, line_id, one_sided, ex, ey, length, flags, dx, dy, sky_u,
           cwe / den AS t,
           cwd / den AS s,
           CASE WHEN one_sided OR cwe < 0 THEN 0 ELSE 1 END AS near_side
    FROM raw
    WHERE den <> 0 AND cwe / den >= 1 AND cwd / den BETWEEN 0 AND 1
),
sides AS (
    -- (c): the per-map rows of the static wide sides table.
    SELECT line_id, front_side, front_middle, front_upper, front_lower,
           front_xoff, front_yoff, front_floor, front_ceiling, front_light,
           front_ftex, front_ctex,
           back_side, back_middle, back_upper, back_lower,
           back_xoff, back_yoff, back_floor, back_ceiling, back_light,
           back_ftex, back_ctex
    FROM render_sides
    WHERE map = map_name
),
geom AS (
    -- D4: the near side faces the camera (the front side for one-sided lines, whose
    -- front sector is the near sector); the far side is the other one. dist is the G4
    -- distance along the linedef: from v1 (s * length) when the near side is the
    -- front, from v2 ((1 - s) * length) when it is the back.
    SELECT hits.c, hits.t, hits.line_id, hits.one_sided,
           CASE WHEN hits.near_side = 0 THEN sides.front_middle ELSE sides.back_middle END AS middle_texture,
           CASE WHEN hits.near_side = 0 THEN sides.front_upper ELSE sides.back_upper END AS upper_texture,
           CASE WHEN hits.near_side = 0 THEN sides.front_lower ELSE sides.back_lower END AS lower_texture,
           CASE WHEN hits.near_side = 0 THEN sides.front_floor ELSE sides.back_floor END AS nf,
           CASE WHEN hits.near_side = 0 THEN sides.front_ceiling ELSE sides.back_ceiling END AS nc,
           CASE WHEN hits.near_side = 0 THEN sides.front_light ELSE sides.back_light END AS nl,
           CASE WHEN hits.near_side = 0 THEN sides.front_ftex ELSE sides.back_ftex END AS nft,
           CASE WHEN hits.near_side = 0 THEN sides.front_ctex ELSE sides.back_ctex END AS nct,
           CASE WHEN hits.near_side = 0 THEN sides.back_floor ELSE sides.front_floor END AS xf,
           CASE WHEN hits.near_side = 0 THEN sides.back_ceiling ELSE sides.front_ceiling END AS xc,
           CASE WHEN hits.near_side = 0 THEN sides.back_ctex ELSE sides.front_ctex END AS xct,
           CASE WHEN hits.near_side = 0 THEN sides.front_xoff ELSE sides.back_xoff END AS nxoff,
           CASE WHEN hits.near_side = 0 THEN sides.front_yoff ELSE sides.back_yoff END AS nyoff,
           CASE WHEN hits.near_side = 0 THEN hits.s * hits.length
                ELSE (1 - hits.s) * hits.length END AS dist,
           hits.ex, hits.ey, hits.flags, hits.dx, hits.dy, hits.sky_u
    FROM hits
    JOIN sides ON sides.line_id = hits.line_id
        -- the near side must have resolved (the old INNER JOIN); the far side stays
        -- NULL when it did not (the old LEFT JOIN)
        AND CASE WHEN hits.near_side = 0 THEN sides.front_side ELSE sides.back_side END
),
projected AS (
    -- D6: y(z) = height/2 - (z - eye) * f / t, for the near and far sector bounds.
    -- (t, line_id) identifies a hit within a column and sorts nearest-first, so it
    -- plays the role of the rank the old row_number window computed. wall_level is
    -- the G7 lighting of a wall hit: fake contrast (+1 on vertical lines, -1 on
    -- horizontal ones, as in r_segs.c) on the clamped sector light, then the depth
    -- scale least(47, floor(2560/t)); flats only need their start map here, their
    -- depth factor runs per pixel.
    SELECT geom.*, height / 2.0 - (nc - eye) * f / t AS cyn,
           height / 2.0 - (nf - eye) * f / t AS fyn,
           height / 2.0 - (xc - eye) * f / t AS cyf,
           height / 2.0 - (xf - eye) * f / t AS fyf,
           greatest(0, least(31,
               (15 - least(15, greatest(0, (nl >> 4)
                   + CASE WHEN ex = 0 THEN 1 WHEN ey = 0 THEN -1 ELSE 0 END))) * 4
               - least(47, CAST(floor(2560.0 / t) AS BIGINT)) // 2)) AS wall_level,
           (15 - (nl >> 4)) * 4 AS flat_startmap
    FROM geom, camera
),
openings AS (
    -- D6: a two-sided hit opens the window [greatest(cy_n, cy_f), least(fy_n, fy_f));
    -- a solid hit closes the column (empty opening).
    SELECT c, t, line_id, one_sided, middle_texture, upper_texture, lower_texture,
           nf, nc, xf, xc, cyn, fyn, cyf, fyf, nft, nct, xct,
           nxoff, nyoff, dist, ex, ey, flags, dx, dy, sky_u, wall_level, flat_startmap,
           CASE WHEN one_sided THEN 0.0 ELSE greatest(cyn, cyf) END AS opening_top,
           CASE WHEN one_sided THEN 0.0 ELSE least(fyn, fyf) END AS opening_bottom
    FROM projected
),
bounds AS (
    -- D6: the visible window of a hit is bounded by the openings of all nearer hits
    -- (max of tops, min of bottoms), clamped to the screen. Only the running bounds
    -- are computed here, on slim rows; everything else joins back below.
    SELECT c, t, line_id,
           greatest(0.0, coalesce(max(opening_top) OVER w, 0.0)) AS window_top,
           least(CAST(height AS DOUBLE),
                 coalesce(min(opening_bottom) OVER w, CAST(height AS DOUBLE))) AS window_bottom
    FROM openings
    WINDOW w AS (PARTITION BY c ORDER BY t, line_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
windows AS (
    SELECT openings.*, bounds.window_top, bounds.window_bottom
    FROM openings JOIN bounds USING (c, t, line_id)
),
spans AS (
    -- D6: each hit draws, clipped to its window, its near ceiling, its walls, and its
    -- near floor; a span [a, b) only exists when b > a. Surfaces keep the G8 names.
    -- Spans carry an atlas offset and image length, never the image itself. Texture
    -- column joins run per hit (and per column u), never per pixel.
    SELECT * FROM (
        -- near ceiling [window top, cy_n): sky when the ceiling texture is F_SKY1
        SELECT w.c, w.t, w.line_id, 0 AS prio, w.window_top AS lo,
               least(w.cyn, w.window_bottom) AS hi,
               CASE WHEN w.nct = 'F_SKY1' THEN 'sky' ELSE 'ceiling' END AS surface,
               CASE WHEN w.nct = 'F_SKY1' THEN 's' ELSE 'f' END AS kind,
               CASE WHEN w.nct = 'F_SKY1' THEN skyc.tex_offset
                    ELSE flatc.tex_offset END AS tex_offset,
               CASE WHEN w.nct = 'F_SKY1' THEN skyc.tex_length ELSE 4096 END AS tex_length,
               NULL::DOUBLE AS top, NULL::DOUBLE AS yoff, NULL::INTEGER AS texh,
               CASE WHEN w.nct = 'F_SKY1' THEN NULL ELSE w.nc END AS h,
               w.dx, w.dy, NULL::BIGINT AS startmap, NULL::INTEGER AS level
        FROM windows w
        LEFT JOIN render_flats flatc ON flatc.name = w.nct
        LEFT JOIN render_texture_columns skyc ON skyc.texture = 'SKY1'
            AND skyc.u = w.sky_u
        UNION ALL
        -- solid wall: G4 pegging, top = near ceiling or near floor + texture height
        SELECT w.c, w.t, w.line_id, 1, greatest(w.cyn, w.window_top),
               least(w.fyn, w.window_bottom), 'wall', 'w', tcol.tex_offset, tcol.tex_length,
               CASE WHEN (w.flags & 16) <> 0 THEN w.nf + tex.height ELSE w.nc END,
               w.nyoff, tex.height, NULL::DOUBLE, w.dx, w.dy, NULL::BIGINT, w.wall_level
        FROM windows w
        LEFT JOIN textures tex ON tex.name = w.middle_texture
        LEFT JOIN render_texture_columns tcol ON tcol.texture = tex.name
            AND tcol.u = ((CAST(floor(w.dist + w.nxoff) AS BIGINT) % tex.width + tex.width) % tex.width)
        WHERE w.one_sided
        UNION ALL
        -- upper wall: skip when both ceilings are sky; G4 flag 8 controls pegging
        SELECT w.c, w.t, w.line_id, 2, greatest(w.cyn, w.window_top),
               least(w.cyf, w.window_bottom), 'upper', 'w', tcol.tex_offset, tcol.tex_length,
               CASE WHEN (w.flags & 8) <> 0 THEN w.nc ELSE w.xc + tex.height END,
               w.nyoff, tex.height, NULL::DOUBLE, w.dx, w.dy, NULL::BIGINT, w.wall_level
        FROM windows w
        LEFT JOIN textures tex ON tex.name = w.upper_texture
        LEFT JOIN render_texture_columns tcol ON tcol.texture = tex.name
            AND tcol.u = ((CAST(floor(w.dist + w.nxoff) AS BIGINT) % tex.width + tex.width) % tex.width)
        WHERE NOT w.one_sided AND w.cyf > w.cyn AND NOT (w.nct = 'F_SKY1' AND w.xct = 'F_SKY1')
        UNION ALL
        -- lower wall: G4 top = near ceiling with flag 16, else far floor
        SELECT w.c, w.t, w.line_id, 3, greatest(w.fyf, w.window_top),
               least(w.fyn, w.window_bottom), 'lower', 'w', tcol.tex_offset, tcol.tex_length,
               CASE WHEN (w.flags & 16) <> 0 THEN w.nc ELSE w.xf END,
               w.nyoff, tex.height, NULL::DOUBLE, w.dx, w.dy, NULL::BIGINT, w.wall_level
        FROM windows w
        LEFT JOIN textures tex ON tex.name = w.lower_texture
        LEFT JOIN render_texture_columns tcol ON tcol.texture = tex.name
            AND tcol.u = ((CAST(floor(w.dist + w.nxoff) AS BIGINT) % tex.width + tex.width) % tex.width)
        WHERE NOT w.one_sided AND w.fyf < w.fyn
        UNION ALL
        -- near floor [fy_n, window bottom): the near floor flat (G5)
        SELECT w.c, w.t, w.line_id, 4, greatest(w.fyn, w.window_top), w.window_bottom,
               'floor', 'f', flatf.tex_offset, 4096, NULL::DOUBLE, NULL::DOUBLE, NULL::INTEGER,
               w.nf, w.dx, w.dy, w.flat_startmap, NULL::INTEGER
        FROM windows w
        LEFT JOIN render_flats flatf ON flatf.name = w.nft
    ) all_spans
    WHERE hi > lo
),
rows_px AS (
    -- D6: a span [a, b) covers pixel rows ceil(a - 0.5) .. ceil(b - 0.5) - 1. G5: rows
    -- on the horizon are skipped (the flat depth d would divide by zero there).
    SELECT s.*, g.y,
           (s.h - camera.eye) * camera.f / (height / 2.0 - g.y - 0.5) AS d
    FROM spans s CROSS JOIN camera
    CROSS JOIN generate_series(CAST(ceil(s.lo - 0.5) AS BIGINT),
                               CAST(ceil(s.hi - 0.5) AS BIGINT) - 1) g(y)
    WHERE s.kind <> 'f' OR height / 2.0 - g.y - 0.5 <> 0
),
samples AS (
    -- Original G4/G5/G6 coordinates, now an index within an atlas image.
    SELECT r.c, r.y, r.surface, r.t, r.line_id, r.prio,
           r.tex_offset, r.tex_length,
           CASE
             WHEN r.kind = 'w' THEN
                 (((CAST(floor(r.top - (camera.eye - (r.y + 0.5 - height / 2.0) * r.t / camera.f)
                              + r.yoff) AS BIGINT) % r.texh) + r.texh) % r.texh) + 1
             WHEN r.kind = 'f' THEN
                 (((CAST(floor(-(px + r.d * r.dy)) AS BIGINT) % 64 + 64) % 64) * 64
                  + ((CAST(floor(px + r.d * r.dx) AS BIGINT) % 64 + 64) % 64)) + 1
             ELSE
                 (((CAST(floor((r.y + 0.5) * 200.0 / height) AS BIGINT) % 128) + 128) % 128) + 1
           END AS tex_index,
           CASE WHEN r.kind = 'w' THEN r.level
                WHEN r.kind = 'f' THEN least(31, greatest(0, r.startmap
                     - 80 // (least(127, CAST(floor(r.d / 16.0) AS BIGINT)) + 1)))
                ELSE 0 END AS level
    FROM rows_px r CROSS JOIN camera
),
pixels AS (
    SELECT c, y, surface, t, line_id, prio,
           CAST(CASE WHEN tex_index BETWEEN 1 AND tex_length THEN
               list_extract(atlas.pixels, tex_offset + tex_index)
           END AS INTEGER) AS texel,
           level
    FROM samples CROSS JOIN render_texels atlas
),
best AS (
    -- D6: if two spans still claim one pixel, the nearest hit wins. (t, line_id) is
    -- unique per column, so ordering by it equals the old rank order; arg_min is a
    -- hash aggregate, cheaper than the old row_number window over every pixel.
    -- G7: the pixel color is palette[colormaps[level][texel]], read from render_lit;
    -- a missing texel (no texture) draws palette index 0.
    SELECT c, y, pick.surface AS surface, pick.packed // 65536 AS r,
           pick.packed // 256 % 256 AS g, pick.packed % 256 AS b
    FROM (
        SELECT c, y, arg_min(struct_pack(surface := surface, packed := packed),
                             struct_pack(t := t, line_id := line_id, prio := prio)) AS pick
        FROM (
            SELECT p.c, p.y, p.surface, p.t, p.line_id, p.prio,
                   coalesce(list_extract(lit.colors,
                       p.level * 256 + coalesce(p.texel, 0) + 1), 0) AS packed
            FROM pixels p CROSS JOIN render_lit lit
        )
        GROUP BY c, y
    )
)
SELECT x.c AS x, y.y AS y,
       coalesce(best.surface, 'empty') AS surface,
       coalesce(best.r, 0) AS r, coalesce(best.g, 0) AS g, coalesce(best.b, 0) AS b
FROM generate_series(0, width - 1) x(c)
CROSS JOIN generate_series(0, height - 1) y(y)
LEFT JOIN best ON best.c = x.c AND best.y = y.y;

-- T3 (D8): the same framebuffer as a terminal frame: the view rendered at cols x
-- rows*2 pixels, two pixel rows packed into one text row of ▀ cells (foreground =
-- top pixel, background = bottom pixel). 24-bit SGR codes are emitted only when the
-- color differs from the previous cell in the same text row (always on the first
-- cell, where there is no predecessor); every row ends with ESC[0m and \n.
CREATE OR REPLACE TEMP MACRO render_ansi(map_name, px, py, angle, cols, rows) AS TABLE
WITH pair AS MATERIALIZED (
    -- Pixel rows y = 2*t and 2*t + 1 become text row t; the two colors are packed
    -- into one integer each (r << 16 | g << 8 | b) so the per-cell change test below
    -- is a single comparison.
    SELECT y // 2 AS text_row, x,
           max(r * 65536 + g * 256 + b) FILTER (WHERE y % 2 = 0) AS fg,
           max(r * 65536 + g * 256 + b) FILTER (WHERE y % 2 = 1) AS bg
    FROM render_pixels(map_name, px, py, angle, cols, rows * 2)
    GROUP BY y // 2, x
),
cells AS (
    -- D8: emit an SGR code only when this cell differs from the previous one (always
    -- on the first cell, where the self-join has no predecessor). The packed colors
    -- unpack back into the same r; g; b triplets, so the bytes are exactly D8's.
    SELECT p.text_row, p.x,
           CASE WHEN p.x = 0 OR p.fg <> prev.fg
                THEN chr(27) || '[38;2;' || p.fg // 65536 || ';' || p.fg // 256 % 256 || ';'
                     || p.fg % 256 || 'm'
                ELSE '' END AS fg_code,
           CASE WHEN p.x = 0 OR p.bg <> prev.bg
                THEN chr(27) || '[48;2;' || p.bg // 65536 || ';' || p.bg // 256 % 256 || ';'
                     || p.bg % 256 || 'm'
                ELSE '' END AS bg_code
    FROM pair p LEFT JOIN pair prev ON prev.text_row = p.text_row AND prev.x = p.x - 1
),
lines AS (
    SELECT text_row, string_agg(fg_code || bg_code || '▀', '' ORDER BY x) AS line
    FROM cells
    GROUP BY text_row
)
SELECT string_agg(line || chr(27) || '[0m' || chr(10), '' ORDER BY text_row) AS text
FROM lines;

-- T2: the same framebuffer as one row-major RGB BLOB, like automap_rgb.
CREATE OR REPLACE TEMP MACRO render_rgb(map_name, px, py, angle, width, height) AS TABLE
SELECT unhex(string_agg(
    lpad(to_hex(r), 2, '0') || lpad(to_hex(g), 2, '0') || lpad(to_hex(b), 2, '0'),
    '' ORDER BY y, x)) AS rgb
FROM render_pixels(map_name, px, py, angle, width, height);
