-- First-person renderer, all SQL: which sector a point is in (T1) and the RGB
-- framebuffer of the view from a camera (T2). Python only wraps the bytes in a PNG.
--
-- Conventions (design D1): world angles in degrees, 0 = east, 90 = north
-- (counterclockwise, same as THINGS); screen y grows downwards; FOV 90 degrees with
-- focal f = width / 2 used for both axes.
--
-- Per-frame work only shades: everything static is put into TEMP tables when this
-- file executes (built once per connection, for all maps, keyed by map; no change to
-- the database schema, and executing this file twice on one connection rebuilds them
-- with OR REPLACE). The BSP point_sector walks of D5 become precomputed half-plane
-- test chains (a), and linedefs are culled by an exact near-plane test (b).

-- One hex byte of an md5 digest as an integer (md5() returns lowercase hex).
CREATE OR REPLACE TEMP MACRO hex_byte(hex, pos) AS
    (instr('0123456789abcdef', substr(hex, pos, 1)) - 1) * 16
    + (instr('0123456789abcdef', substr(hex, pos + 1, 1)) - 1);

-- D7 shaded channel: flats by light only, walls by light * greatest(0.35, 1 - t/1600);
-- rounded and clamped to 0..255.
CREATE OR REPLACE TEMP MACRO shade(value, light, depth, wall) AS
    CAST(round(least(255.0, greatest(0.0, value * light / 255.0
        * (CASE WHEN wall THEN greatest(0.35, 1 - depth / 1600.0) ELSE 1.0 END)))) AS INTEGER);

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

-- (c) Static line geometry for all maps, built once per connection.
CREATE OR REPLACE TEMP TABLE render_lines AS
SELECT linedefs.map, linedefs.id AS line_id,
       a.x AS ax, a.y AS ay, b.x AS bx, b.y AS by,
       b.x - a.x AS ex, b.y - a.y AS ey,
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
       sf.lower_texture AS front_lower, secf.floor_height AS front_floor,
       secf.ceiling_height AS front_ceiling, secf.light_level AS front_light,
       secf.floor_texture AS front_ftex, secf.ceiling_texture AS front_ctex,
       sb.id IS NOT NULL AND secb.id IS NOT NULL AS back_side,
       sb.middle_texture AS back_middle, sb.upper_texture AS back_upper,
       sb.lower_texture AS back_lower, secb.floor_height AS back_floor,
       secb.ceiling_height AS back_ceiling, secb.light_level AS back_light,
       secb.floor_texture AS back_ftex, secb.ceiling_texture AS back_ctex
FROM linedefs
LEFT JOIN sidedefs sf ON sf.map = linedefs.map AND sf.id = linedefs.front_sidedef
LEFT JOIN sectors secf ON secf.map = linedefs.map AND secf.id = sf.sector
LEFT JOIN sidedefs sb ON sb.map = linedefs.map AND sb.id = linedefs.back_sidedef
LEFT JOIN sectors secb ON secb.map = linedefs.map AND secb.id = sb.sector;

-- (c) D7 base RGB of every texture name (md5 hex bytes 1-3, each mapped through
-- 64 + byte % 160), built once per connection so per-frame work only shades.
CREATE OR REPLACE TEMP TABLE render_texture_colors AS
SELECT name,
       64 + hex_byte(md5(name), 1) % 160 AS r,
       64 + hex_byte(md5(name), 3) % 160 AS g,
       64 + hex_byte(md5(name), 5) % 160 AS b
FROM (
    SELECT DISTINCT coalesce(texture, '-') AS name FROM (
        SELECT middle_texture AS texture FROM sidedefs
        UNION SELECT upper_texture FROM sidedefs
        UNION SELECT lower_texture FROM sidedefs
        UNION SELECT floor_texture FROM sectors
        UNION SELECT ceiling_texture FROM sectors
    ) names
);

-- T2 (D2-D7): every visible pixel of the view from (px, py) at `angle` degrees, as
-- (x, y, surface, r, g, b) rows. Surfaces: 'ceiling', 'wall', 'upper', 'lower',
-- 'floor', 'sky', 'empty'.
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
    -- perpendicular depth.
    SELECT g.c,
           (g.c + 0.5 - width / 2.0) / f AS u,
           cos(rad) + (g.c + 0.5 - width / 2.0) / f * sin(rad) AS dx,
           sin(rad) - (g.c + 0.5 - width / 2.0) / f * cos(rad) AS dy
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
    SELECT line_id, ax, ay, ex, ey, one_sided
    FROM (
        SELECT render_lines.line_id, render_lines.ax, render_lines.ay,
               render_lines.ex, render_lines.ey, render_lines.one_sided,
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
    SELECT rays.c, lines.line_id, lines.one_sided,
           rays.dx * lines.ey - rays.dy * lines.ex AS den,
           (lines.ax - px) * lines.ey - (lines.ay - py) * lines.ex AS cwe,
           (lines.ax - px) * rays.dy - (lines.ay - py) * rays.dx AS cwd
    FROM rays CROSS JOIN lines
),
hits AS (
    SELECT c, line_id, one_sided, cwe / den AS t,
           CASE WHEN one_sided OR cwe < 0 THEN 0 ELSE 1 END AS near_side
    FROM raw
    WHERE den <> 0 AND cwe / den >= 1 AND cwd / den BETWEEN 0 AND 1
),
sides AS (
    -- (c): the per-map rows of the static wide sides table.
    SELECT line_id, front_side, front_middle, front_upper, front_lower,
           front_floor, front_ceiling, front_light, front_ftex, front_ctex,
           back_side, back_middle, back_upper, back_lower,
           back_floor, back_ceiling, back_light, back_ftex, back_ctex
    FROM render_sides
    WHERE map = map_name
),
geom AS (
    -- D4: the near side faces the camera (the front side for one-sided lines, whose
    -- front sector is the near sector); the far side is the other one. (The far
    -- light level and floor texture are never read by the spans below.)
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
           CASE WHEN hits.near_side = 0 THEN sides.back_ctex ELSE sides.front_ctex END AS xct
    FROM hits
    JOIN sides ON sides.line_id = hits.line_id
        -- the near side must have resolved (the old INNER JOIN); the far side stays
        -- NULL when it did not (the old LEFT JOIN)
        AND CASE WHEN hits.near_side = 0 THEN sides.front_side ELSE sides.back_side END
),
projected AS (
    -- D6: y(z) = height/2 - (z - eye) * f / t, for the near and far sector bounds.
    -- (t, line_id) identifies a hit within a column and sorts nearest-first, so it
    -- plays the role of the rank the old row_number window computed.
    SELECT geom.*, height / 2.0 - (nc - eye) * f / t AS cyn,
           height / 2.0 - (nf - eye) * f / t AS fyn,
           height / 2.0 - (xc - eye) * f / t AS cyf,
           height / 2.0 - (xf - eye) * f / t AS fyf
    FROM geom, camera
),
openings AS (
    -- D6: a two-sided hit opens the window [greatest(cy_n, cy_f), least(fy_n, fy_f));
    -- a solid hit closes the column (empty opening).
    SELECT c, t, line_id, one_sided, middle_texture, upper_texture, lower_texture,
           cyn, fyn, cyf, fyf, nl, nft, nct, xct,
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
    -- near floor. A span [a, b) only exists when b > a. Base colors come from the
    -- static D7 table (a); only shading runs per frame.
    SELECT * FROM (
        -- near ceiling [window top, cy_n): sky when the ceiling texture is F_SKY1
        SELECT c, t, line_id, 0 AS prio, window_top AS lo, least(cyn, window_bottom) AS hi,
               CASE WHEN nct = 'F_SKY1' THEN 'sky' ELSE 'ceiling' END AS surface,
               CASE WHEN nct = 'F_SKY1' THEN 100 ELSE shade(cn.r, nl, t, false) END AS r,
               CASE WHEN nct = 'F_SKY1' THEN 150 ELSE shade(cn.g, nl, t, false) END AS g,
               CASE WHEN nct = 'F_SKY1' THEN 220 ELSE shade(cn.b, nl, t, false) END AS b
        FROM windows
        JOIN render_texture_colors cn ON cn.name = coalesce(nct, '-')
        UNION ALL
        -- solid wall [cy_n, fy_n), textured with the near side's middle texture
        SELECT c, t, line_id, 1, greatest(cyn, window_top) AS lo, least(fyn, window_bottom) AS hi, 'wall',
               shade(cw.r, nl, t, true), shade(cw.g, nl, t, true), shade(cw.b, nl, t, true)
        FROM windows
        JOIN render_texture_colors cw ON cw.name = coalesce(middle_texture, '-')
        WHERE one_sided
        UNION ALL
        -- upper wall [cy_n, cy_f), skipped when both ceilings are sky
        SELECT c, t, line_id, 2, greatest(cyn, window_top), least(cyf, window_bottom), 'upper',
               shade(cu.r, nl, t, true), shade(cu.g, nl, t, true), shade(cu.b, nl, t, true)
        FROM windows
        JOIN render_texture_colors cu ON cu.name = coalesce(upper_texture, '-')
        WHERE NOT one_sided AND cyf > cyn AND NOT (nct = 'F_SKY1' AND xct = 'F_SKY1')
        UNION ALL
        -- lower wall [fy_f, fy_n)
        SELECT c, t, line_id, 3, greatest(fyf, window_top), least(fyn, window_bottom), 'lower',
               shade(cl.r, nl, t, true), shade(cl.g, nl, t, true), shade(cl.b, nl, t, true)
        FROM windows
        JOIN render_texture_colors cl ON cl.name = coalesce(lower_texture, '-')
        WHERE NOT one_sided AND fyf < fyn
        UNION ALL
        -- near floor [fy_n, window bottom)
        SELECT c, t, line_id, 4, greatest(fyn, window_top), window_bottom, 'floor',
               shade(cf.r, nl, t, false), shade(cf.g, nl, t, false), shade(cf.b, nl, t, false)
        FROM windows
        JOIN render_texture_colors cf ON cf.name = coalesce(nft, '-')
    ) all_spans
    WHERE hi > lo
),
pixels AS (
    -- D6: a span [a, b) covers pixel rows ceil(a - 0.5) .. ceil(b - 0.5) - 1.
    SELECT spans.c, g.y, spans.surface, spans.r, spans.g, spans.b, spans.t, spans.line_id, spans.prio
    FROM spans, generate_series(CAST(ceil(spans.lo - 0.5) AS BIGINT),
                                CAST(ceil(spans.hi - 0.5) AS BIGINT) - 1) g(y)
),
best AS (
    -- D6: if two spans still claim one pixel, the nearest hit wins. (t, line_id) is
    -- unique per column, so ordering by it equals the old rank order; arg_min is a
    -- hash aggregate, cheaper than the old row_number window over every pixel.
    SELECT c, y, pick.surface AS surface, pick.r AS r, pick.g AS g, pick.b AS b
    FROM (
        SELECT c, y, arg_min(struct_pack(surface := surface, r := r, g := g, b := b),
                             struct_pack(t := t, line_id := line_id, prio := prio)) AS pick
        FROM pixels GROUP BY c, y
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
           max(r) FILTER (WHERE y % 2 = 0) * 65536
               + max(g) FILTER (WHERE y % 2 = 0) * 256
               + max(b) FILTER (WHERE y % 2 = 0) AS fg,
           max(r) FILTER (WHERE y % 2 = 1) * 65536
               + max(g) FILTER (WHERE y % 2 = 1) * 256
               + max(b) FILTER (WHERE y % 2 = 1) AS bg
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
