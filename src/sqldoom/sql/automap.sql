-- Automap renderer: a top-down view of one map, computed entirely in SQL.
--
-- Pipeline (one CTE per step):
--   bounds       -> bounding box of the map's vertexes
--   frame        -> one scale factor that fits the box into the screen, keeping proportions
--   walls        -> every linedef with world coordinates and an automap kind
--   screen_walls -> the same lines in pixel coordinates
--   line_pixels  -> rasterization: each line becomes one row per pixel it covers
--   player       -> the player 1 start as a small cross
--   best         -> where several things cover one pixel, the highest priority wins
--   final SELECT -> the full width x height grid, empty where nothing was drawn
--
-- Screen y grows downwards while world y grows upwards, hence (max_y - y).
-- `aspect` is the pixel height/width ratio: 1 for images, 2 for terminal cells.

CREATE OR REPLACE TEMP MACRO automap_pixels(map_name, width, height, aspect) AS TABLE
WITH
bounds AS (
    SELECT min(x) AS min_x, max(x) AS max_x, min(y) AS min_y, max(y) AS max_y
    FROM vertexes
    WHERE map = map_name
),
frame AS (
    -- 1 pixel margin on each side; the usable area is centered.
    SELECT *,
           width - 3 AS usable_w,
           height - 3 AS usable_h,
           least((width - 3) / greatest(max_x - min_x, 1),
                 (height - 3) * aspect / greatest(max_y - min_y, 1)) AS scale
    FROM bounds
),
walls AS (
    SELECT CASE
               WHEN l.back_sidedef IS NULL THEN 'wall'
               WHEN fs.floor_height <> bs.floor_height THEN 'floor'
               WHEN fs.ceiling_height <> bs.ceiling_height THEN 'ceiling'
               ELSE 'plain'
           END AS kind,
           a.x AS x1, a.y AS y1, b.x AS x2, b.y AS y2
    FROM linedefs l
    JOIN vertexes a ON a.map = l.map AND a.id = l.v1
    JOIN vertexes b ON b.map = l.map AND b.id = l.v2
    JOIN sidedefs fsd ON fsd.map = l.map AND fsd.id = l.front_sidedef
    JOIN sectors fs ON fs.map = l.map AND fs.id = fsd.sector
    LEFT JOIN sidedefs bsd ON bsd.map = l.map AND bsd.id = l.back_sidedef
    LEFT JOIN sectors bs ON bs.map = l.map AND bs.id = bsd.sector
    WHERE l.map = map_name
),
screen_walls AS (
    SELECT kind,
           1 + (x1 - min_x) * scale + (usable_w - (max_x - min_x) * scale) / 2 AS sx1,
           1 + (max_y - y1) * scale / aspect + (usable_h - (max_y - min_y) * scale / aspect) / 2 AS sy1,
           1 + (x2 - min_x) * scale + (usable_w - (max_x - min_x) * scale) / 2 AS sx2,
           1 + (max_y - y2) * scale / aspect + (usable_h - (max_y - min_y) * scale / aspect) / 2 AS sy2
    FROM walls, frame
),
line_pixels AS (
    -- DDA: walk max(|dx|, |dy|) steps along the line, one pixel per step.
    -- generate_series in a LATERAL join is SQL's loop: it fans each line out into rows.
    SELECT kind,
           CAST(round(sx1 + (sx2 - sx1) * t / n) AS INTEGER) AS x,
           CAST(round(sy1 + (sy2 - sy1) * t / n) AS INTEGER) AS y
    FROM (SELECT *, greatest(CAST(ceil(greatest(abs(sx2 - sx1), abs(sy2 - sy1))) AS BIGINT), 1) AS n
          FROM screen_walls) w,
         LATERAL generate_series(0, n) AS s(t)
),
player AS (
    -- A cross of radius width // 160: a single pixel in a terminal, visible in a 640 px image.
    SELECT 'player' AS kind, p.x + dx AS x, p.y + dy AS y
    FROM (SELECT CAST(round(1 + (t.x - min_x) * scale + (usable_w - (max_x - min_x) * scale) / 2) AS INTEGER) AS x,
                 CAST(round(1 + (max_y - t.y) * scale / aspect
                            + (usable_h - (max_y - min_y) * scale / aspect) / 2) AS INTEGER) AS y
          FROM things t, frame
          WHERE t.map = map_name AND t.type = 1) p,
         LATERAL (SELECT d AS dx, 0 AS dy FROM generate_series(-(width // 160), width // 160) g(d)
                  UNION ALL
                  SELECT 0, d FROM generate_series(-(width // 160), width // 160) g(d))
),
drawn AS (
    SELECT kind, x, y FROM line_pixels
    UNION ALL
    SELECT kind, x, y FROM player
),
best AS (
    SELECT x, y,
           arg_min(kind, CASE kind WHEN 'player' THEN 0 WHEN 'wall' THEN 1 WHEN 'floor' THEN 2
                                   WHEN 'ceiling' THEN 3 ELSE 4 END) AS kind
    FROM drawn
    GROUP BY x, y
)
SELECT gx.x, gy.y, coalesce(best.kind, 'empty') AS kind
FROM generate_series(0, width - 1) AS gx(x)
CROSS JOIN generate_series(0, height - 1) AS gy(y)
LEFT JOIN best ON best.x = gx.x AND best.y = gy.y;


-- The automap as text: one character per pixel, one line per screen row.
CREATE OR REPLACE TEMP MACRO automap_ascii(map_name, width, height) AS TABLE
SELECT string_agg(line, '' ORDER BY y) AS text
FROM (
    SELECT y,
           string_agg(CASE kind WHEN 'player' THEN '@' WHEN 'wall' THEN '#' WHEN 'floor' THEN '+'
                                WHEN 'ceiling' THEN '=' WHEN 'plain' THEN '.' ELSE ' ' END,
                      '' ORDER BY x) || chr(10) AS line
    FROM automap_pixels(map_name, width, height, 2.0)
    GROUP BY y
);


-- The automap as a framebuffer: width * height * 3 bytes of RGB, row by row, in a single BLOB.
-- Each pixel becomes 6 hex digits; one string_agg in screen order and unhex() turn them into bytes.
CREATE OR REPLACE TEMP MACRO automap_rgb(map_name, width, height) AS TABLE
SELECT unhex(string_agg(CASE kind WHEN 'player' THEN '00ff00' WHEN 'wall' THEN 'ff0000'
                                  WHEN 'floor' THEN 'bc7848' WHEN 'ceiling' THEN 'fcfc00'
                                  WHEN 'plain' THEN '808080' ELSE '000000' END,
                        '' ORDER BY y, x)) AS rgb
FROM automap_pixels(map_name, width, height, 1.0);
