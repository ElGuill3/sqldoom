-- First-person renderer, all SQL: which sector a point is in (T1) and the RGB
-- framebuffer of the view from a camera (T2). Python only wraps the bytes in a PNG.
--
-- Conventions (design D1): world angles in degrees, 0 = east, 90 = north
-- (counterclockwise, same as THINGS); screen y grows downwards; FOV 90 degrees with
-- focal f = width / 2 used for both axes.

-- One hex byte of an md5 digest as an integer (md5() returns lowercase hex).
CREATE OR REPLACE TEMP MACRO hex_byte(hex, pos) AS
    (instr('0123456789abcdef', substr(hex, pos, 1)) - 1) * 16
    + (instr('0123456789abcdef', substr(hex, pos + 1, 1)) - 1);

-- D7 base RGB of a texture name: md5 hex bytes 1-3, each mapped through 64 + byte % 160.
CREATE OR REPLACE TEMP MACRO tex_channel(tex, i) AS
    64 + hex_byte(md5(coalesce(tex, '-')), i) % 160;

-- D7 shaded channel: flats by light only, walls by light * greatest(0.35, 1 - t/1600);
-- rounded and clamped to 0..255.
CREATE OR REPLACE TEMP MACRO shade(value, light, depth, wall) AS
    CAST(round(least(255.0, greatest(0.0, value * light / 255.0
        * (CASE WHEN wall THEN greatest(0.35, 1 - depth / 1600.0) ELSE 1.0 END)))) AS INTEGER);

-- D7 final RGB of a textured surface. Sky is handled by the caller (fixed color).
CREATE OR REPLACE TEMP MACRO texel(tex, light, depth, wall) AS {
    'r': shade(tex_channel(tex, 1), light, depth, wall),
    'g': shade(tex_channel(tex, 3), light, depth, wall),
    'b': shade(tex_channel(tex, 5), light, depth, wall),
};

-- T1 (D5): which sector is a world point in? Walk the BSP from the root node (highest
-- node id), going to the right (front) child when (py - n.y) * n.dx < n.dy * (px - n.x),
-- until a subsector; its first seg names a linedef, and seg direction 0/1 picks the
-- front/back sidedef. A map without nodes is subsector 0.
CREATE OR REPLACE TEMP MACRO point_sector(map_name, px, py) AS TABLE
WITH RECURSIVE walk(is_node, id) AS (
    SELECT true, id FROM nodes
    WHERE map = map_name AND id = (SELECT max(id) FROM nodes WHERE map = map_name)
    UNION ALL
    SELECT NOT CASE WHEN (py - n.y) * n.dx < n.dy * (px - n.x)
                    THEN n.right_is_ssector ELSE n.left_is_ssector END,
           CASE WHEN (py - n.y) * n.dx < n.dy * (px - n.x)
                THEN n.right_child ELSE n.left_child END
    FROM walk w
    JOIN nodes n ON n.map = map_name AND n.id = w.id
    WHERE w.is_node
)
SELECT sidedefs.sector AS sector
FROM walk
JOIN ssectors ON ssectors.map = map_name AND ssectors.id = walk.id AND NOT walk.is_node
JOIN segs ON segs.map = map_name AND segs.id = ssectors.first_seg
JOIN linedefs ON linedefs.map = map_name AND linedefs.id = segs.linedef
JOIN sidedefs ON sidedefs.map = map_name AND sidedefs.id =
    CASE segs.direction WHEN 0 THEN linedefs.front_sidedef ELSE linedefs.back_sidedef END
UNION ALL
SELECT sidedefs.sector
FROM ssectors
JOIN segs ON segs.map = map_name AND segs.id = ssectors.first_seg
JOIN linedefs ON linedefs.map = map_name AND linedefs.id = segs.linedef
JOIN sidedefs ON sidedefs.map = map_name AND sidedefs.id =
    CASE segs.direction WHEN 0 THEN linedefs.front_sidedef ELSE linedefs.back_sidedef END
WHERE ssectors.map = map_name AND ssectors.id = 0
  AND NOT EXISTS (SELECT 1 FROM nodes WHERE map = map_name);

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
    SELECT linedefs.id AS line_id,
           a.x AS ax, a.y AS ay, b.x - a.x AS ex, b.y - a.y AS ey,
           linedefs.back_sidedef IS NULL AS one_sided
    FROM linedefs
    JOIN vertexes a ON a.map = linedefs.map AND a.id = linedefs.v1
    JOIN vertexes b ON b.map = linedefs.map AND b.id = linedefs.v2
    WHERE linedefs.map = map_name
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
    -- Both sides of every linedef, with their sidedef textures and sector data.
    SELECT linedefs.id AS line_id, 0 AS side_no,
           sidedefs.middle_texture, sidedefs.upper_texture, sidedefs.lower_texture, sectors.*
    FROM linedefs
    JOIN sidedefs ON sidedefs.map = linedefs.map AND sidedefs.id = linedefs.front_sidedef
    JOIN sectors ON sectors.map = linedefs.map AND sectors.id = sidedefs.sector
    WHERE linedefs.map = map_name
    UNION ALL
    SELECT linedefs.id, 1,
           sidedefs.middle_texture, sidedefs.upper_texture, sidedefs.lower_texture, sectors.*
    FROM linedefs
    JOIN sidedefs ON sidedefs.map = linedefs.map AND sidedefs.id = linedefs.back_sidedef
    JOIN sectors ON sectors.map = linedefs.map AND sectors.id = sidedefs.sector
    WHERE linedefs.map = map_name AND linedefs.back_sidedef IS NOT NULL
),
geom AS (
    -- D4: the near side faces the camera (the front side for one-sided lines, whose
    -- front sector is the near sector); the far side is the other one.
    SELECT hits.c, hits.t, hits.line_id, hits.one_sided,
           near.middle_texture, near.upper_texture, near.lower_texture,
           near.floor_height AS nf, near.ceiling_height AS nc,
           near.light_level AS nl, near.floor_texture AS nft, near.ceiling_texture AS nct,
           far.floor_height AS xf, far.ceiling_height AS xc,
           far.light_level AS xl, far.floor_texture AS xft, far.ceiling_texture AS xct
    FROM hits
    JOIN sides near ON near.line_id = hits.line_id AND near.side_no = hits.near_side
    LEFT JOIN sides far ON far.line_id = hits.line_id AND far.side_no = 1 - hits.near_side
),
ordered AS (
    -- D6: hits ordered by t, nearest first, per column.
    SELECT *, row_number() OVER (PARTITION BY c ORDER BY t, line_id) AS rn
    FROM geom
),
projected AS (
    -- D6: y(z) = height/2 - (z - eye) * f / t, for the near and far sector bounds.
    SELECT ordered.*, height / 2.0 - (nc - eye) * f / t AS cyn,
           height / 2.0 - (nf - eye) * f / t AS fyn,
           height / 2.0 - (xc - eye) * f / t AS cyf,
           height / 2.0 - (xf - eye) * f / t AS fyf
    FROM ordered, camera
),
openings AS (
    -- D6: a two-sided hit opens the window [greatest(cy_n, cy_f), least(fy_n, fy_f));
    -- a solid hit closes the column (empty opening).
    SELECT *,
           CASE WHEN one_sided THEN 0.0 ELSE greatest(cyn, cyf) END AS opening_top,
           CASE WHEN one_sided THEN 0.0 ELSE least(fyn, fyf) END AS opening_bottom
    FROM projected
),
windows AS (
    -- D6: the visible window of a hit is bounded by the openings of all nearer hits
    -- (max of tops, min of bottoms), clamped to the screen.
    SELECT openings.*,
           greatest(0.0, coalesce(max(opening_top) OVER w, 0.0)) AS window_top,
           least(CAST(height AS DOUBLE),
                 coalesce(min(opening_bottom) OVER w, CAST(height AS DOUBLE))) AS window_bottom
    FROM openings
    WINDOW w AS (PARTITION BY c ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
spans AS (
    -- D6: each hit draws, clipped to its window, its near ceiling, its walls, and its
    -- near floor. A span [a, b) only exists when b > a.
    SELECT * FROM (
        -- near ceiling [window top, cy_n): sky when the ceiling texture is F_SKY1
        SELECT c, rn, 0 AS prio, window_top AS lo, least(cyn, window_bottom) AS hi,
               CASE WHEN nct = 'F_SKY1' THEN 'sky' ELSE 'ceiling' END AS surface,
               CASE WHEN nct = 'F_SKY1' THEN 100 ELSE (texel(nct, nl, t, false)).r END AS r,
               CASE WHEN nct = 'F_SKY1' THEN 150 ELSE (texel(nct, nl, t, false)).g END AS g,
               CASE WHEN nct = 'F_SKY1' THEN 220 ELSE (texel(nct, nl, t, false)).b END AS b
        FROM windows
        UNION ALL
        -- solid wall [cy_n, fy_n), textured with the near side's middle texture
        SELECT c, rn, 1, greatest(cyn, window_top) AS lo, least(fyn, window_bottom) AS hi, 'wall',
               (texel(middle_texture, nl, t, true)).r,
               (texel(middle_texture, nl, t, true)).g,
               (texel(middle_texture, nl, t, true)).b
        FROM windows WHERE one_sided
        UNION ALL
        -- upper wall [cy_n, cy_f), skipped when both ceilings are sky
        SELECT c, rn, 2, greatest(cyn, window_top), least(cyf, window_bottom), 'upper',
               (texel(upper_texture, nl, t, true)).r,
               (texel(upper_texture, nl, t, true)).g,
               (texel(upper_texture, nl, t, true)).b
        FROM windows
        WHERE NOT one_sided AND cyf > cyn AND NOT (nct = 'F_SKY1' AND xct = 'F_SKY1')
        UNION ALL
        -- lower wall [fy_f, fy_n)
        SELECT c, rn, 3, greatest(fyf, window_top), least(fyn, window_bottom), 'lower',
               (texel(lower_texture, nl, t, true)).r,
               (texel(lower_texture, nl, t, true)).g,
               (texel(lower_texture, nl, t, true)).b
        FROM windows WHERE NOT one_sided AND fyf < fyn
        UNION ALL
        -- near floor [fy_n, window bottom)
        SELECT c, rn, 4, greatest(fyn, window_top), window_bottom, 'floor',
               (texel(nft, nl, t, false)).r, (texel(nft, nl, t, false)).g,
               (texel(nft, nl, t, false)).b
        FROM windows
    ) all_spans
    WHERE hi > lo
),
pixels AS (
    -- D6: a span [a, b) covers pixel rows ceil(a - 0.5) .. ceil(b - 0.5) - 1.
    SELECT spans.c, g.y, spans.surface, spans.r, spans.g, spans.b, spans.rn, spans.prio
    FROM spans, generate_series(CAST(ceil(spans.lo - 0.5) AS BIGINT),
                                CAST(ceil(spans.hi - 0.5) AS BIGINT) - 1) g(y)
),
best AS (
    -- D6: if two spans still claim one pixel, the nearest hit wins.
    SELECT c, y, surface, r, g, b
    FROM (SELECT *, row_number() OVER (PARTITION BY c, y ORDER BY rn, prio) AS pick FROM pixels)
    WHERE pick = 1
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
-- cell, where lag() has nothing to compare); every row ends with ESC[0m and \n.
CREATE OR REPLACE TEMP MACRO render_ansi(map_name, px, py, angle, cols, rows) AS TABLE
WITH pair AS (
    -- Pixel rows y = 2*t and 2*t + 1 become text row t.
    SELECT y // 2 AS text_row, x,
           max(r) FILTER (WHERE y % 2 = 0) AS fr,
           max(g) FILTER (WHERE y % 2 = 0) AS fgn,
           max(b) FILTER (WHERE y % 2 = 0) AS fb,
           max(r) FILTER (WHERE y % 2 = 1) AS br,
           max(g) FILTER (WHERE y % 2 = 1) AS bgn,
           max(b) FILTER (WHERE y % 2 = 1) AS bb
    FROM render_pixels(map_name, px, py, angle, cols, rows * 2)
    GROUP BY y // 2, x
),
cells AS (
    -- D8: emit an SGR code only when this cell differs from the previous one.
    SELECT text_row, x,
           CASE WHEN x = 0
                     OR fr <> lag(fr) OVER w OR fgn <> lag(fgn) OVER w OR fb <> lag(fb) OVER w
                THEN chr(27) || '[38;2;' || fr || ';' || fgn || ';' || fb || 'm'
                ELSE '' END AS fg_code,
           CASE WHEN x = 0
                     OR br <> lag(br) OVER w OR bgn <> lag(bgn) OVER w OR bb <> lag(bb) OVER w
                THEN chr(27) || '[48;2;' || br || ';' || bgn || ';' || bb || 'm'
                ELSE '' END AS bg_code
    FROM pair
    WINDOW w AS (PARTITION BY text_row ORDER BY x)
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
