-- Part 5a (player): the player's state and one game tic in SQL (odd/tasks/player.md, M1-M6).
-- Units are map units and degrees (0 = east, counterclockwise); one tic is 1/35 s.

-- M1: the player, one row, filled by sqldoom.game.load_game. TEMP: the imported database
-- stays read-only and every session starts a new game.
CREATE OR REPLACE TEMP TABLE player (
    map VARCHAR, x DOUBLE, y DOUBLE, z DOUBLE, angle DOUBLE,
    momx DOUBLE, momy DOUBLE, tic BIGINT
);

-- Static line data for collisions, built once per load: the segment, its bounding box,
-- whether it blocks (one-sided or flag 1, impassable) and both sectors' heights.
CREATE OR REPLACE TEMP TABLE game_lines AS
SELECT linedefs.map, linedefs.id AS line_id,
       a.x::DOUBLE AS ax, a.y::DOUBLE AS ay,
       (b.x - a.x)::DOUBLE AS ex, (b.y - a.y)::DOUBLE AS ey,
       ((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y))::DOUBLE AS len2,
       least(a.x, b.x) AS minx, greatest(a.x, b.x) AS maxx,
       least(a.y, b.y) AS miny, greatest(a.y, b.y) AS maxy,
       linedefs.back_sidedef IS NULL OR linedefs.flags & 1 <> 0 AS blocking,
       secf.floor_height AS front_floor, secf.ceiling_height AS front_ceiling,
       secb.floor_height AS back_floor, secb.ceiling_height AS back_ceiling
FROM linedefs
JOIN vertexes a ON a.map = linedefs.map AND a.id = linedefs.v1
JOIN vertexes b ON b.map = linedefs.map AND b.id = linedefs.v2
LEFT JOIN sidedefs sf ON sf.map = linedefs.map AND sf.id = linedefs.front_sidedef
LEFT JOIN sectors secf ON secf.map = linedefs.map AND secf.id = sf.sector
LEFT JOIN sidedefs sb ON sb.map = linedefs.map AND sb.id = linedefs.back_sidedef
LEFT JOIN sectors secb ON secb.map = linedefs.map AND secb.id = sb.sector;

-- The floor and ceiling of the sector containing (px, py): cast a ray east, take the
-- nearest line it crosses and the side facing the point (front is the right side of
-- v1 -> v2). No row, or NULL heights, when the point is outside the map. Faster than the
-- renderer's BSP walk (point_sector) for the few points a tic checks. load_game uses it
-- for the start; game_step applies the same rule inline (its `checked` step): keep the
-- two in step.
CREATE OR REPLACE TEMP MACRO game_sector(map_name, px, py) AS TABLE
SELECT CASE WHEN front THEN front_floor ELSE back_floor END AS floor,
       CASE WHEN front THEN front_ceiling ELSE back_ceiling END AS ceiling
FROM (
    SELECT front_floor, front_ceiling, back_floor, back_ceiling,
           (px - ax) * ey - (py - ay) * ex >= 0 AS front,
           ax + (py - ay) * ex / ey AS hit_x, line_id
    FROM game_lines
    WHERE map = map_name AND (ay > py) <> (ay + ey > py) AND maxx >= px
)
WHERE hit_x >= px
ORDER BY hit_x, line_id
LIMIT 1;

-- M2-M5: the next player row for one tic of input. forward: 1 forward, -1 back; side:
-- 1 strafe right, -1 left; turn: 1 turn left (counterclockwise), -1 right; run doubles
-- the turn and the thrust. Order as in Doom: turn, thrust, move, friction.
--
-- Moving (M5): a move longer than the radius goes in two half steps. Each step tries
-- the full step, then x alone, then y alone (Doom's stairstep fallback); the first that
-- fits wins, an axis that had to be dropped loses its momentum, and when nothing fits
-- the player stays and loses both. A position fits (M4) when it is inside the map, no
-- blocking line is closer than the radius 16, the lowest ceiling and the highest floor
-- of its sector and of every touching line leave 56 units, and that floor is at most 24
-- above the player's feet; the player then stands on that floor.
--
-- Every position a tic may need is known up front: 3 first-step candidates and, from each
-- possible first outcome (or from not moving), 3 second-step candidates. All of them are
-- checked against the lines in one join, and the outcome is then picked from that small
-- table: one pass over the lines instead of a chain of dependent subqueries.
CREATE OR REPLACE TEMP MACRO game_step(forward, side, turn, run) AS TABLE
WITH turned AS (
    SELECT *, ((angle + turn * CASE WHEN run THEN 7.03125 ELSE 3.515625 END) % 360 + 360) % 360
                  AS new_angle,
           CASE WHEN run THEN 1.5625 ELSE 0.78125 END AS thrust
    FROM player
), pushed AS (
    -- Thrust along F = (cos a, sin a) and R = (sin a, -cos a); MAXMOVE clamps each axis.
    SELECT *, least(30.0, greatest(-30.0, momx + forward * thrust * cos(radians(new_angle))
                                               + side * thrust * sin(radians(new_angle)))) AS mx,
              least(30.0, greatest(-30.0, momy + forward * thrust * sin(radians(new_angle))
                                               - side * thrust * cos(radians(new_angle)))) AS my
    FROM turned
), p AS (
    SELECT *, CASE WHEN abs(mx) > 16 OR abs(my) > 16 THEN 2 ELSE 1 END AS steps,
           mx / CASE WHEN abs(mx) > 16 OR abs(my) > 16 THEN 2 ELSE 1 END AS hx,
           my / CASE WHEN abs(mx) > 16 OR abs(my) > 16 THEN 2 ELSE 1 END AS hy
    FROM pushed
), tries(k, use_x, use_y) AS (
    -- Candidate k moves along x when use_x and along y when use_y.
    VALUES (0, true, true), (1, true, false), (2, false, true)
), first_try AS (
    SELECT k AS k1, -1 AS k2,
           CASE WHEN use_x THEN x + hx ELSE x END AS cx,
           CASE WHEN use_y THEN y + hy ELSE y END AS cy
    FROM p, tries
), second_from AS (
    -- Where the second step starts for each first outcome k1 (3: nothing fitted), and
    -- which axes it still moves along.
    SELECT k1, cx AS from_x, cy AS from_y, k1 IN (0, 1) AS keep_x, k1 IN (0, 2) AS keep_y
    FROM first_try
    UNION ALL
    SELECT 3, x, y, false, false FROM p
), points AS (
    SELECT * FROM first_try
    UNION ALL
    SELECT k1, k AS k2,
           CASE WHEN use_x AND keep_x THEN from_x + hx ELSE from_x END,
           CASE WHEN use_y AND keep_y THEN from_y + hy ELSE from_y END
    FROM second_from, tries, p
    WHERE steps = 2
), lines AS (
    SELECT * FROM game_lines WHERE map = (SELECT map FROM player)
), near AS (
    -- Line/point pairs that touch (closer than the radius) or that the ray cast east
    -- from the point crosses (to find the point's sector).
    SELECT k1, k2, cx, cy, blocking, front_floor, front_ceiling, back_floor, back_ceiling,
           ax, ay, ex, ey, line_id,
           cx > minx - 16 AND cx < maxx + 16 AND cy > miny - 16 AND cy < maxy + 16 AS in_box,
           (ay > cy) <> (ay + ey > cy) AND maxx >= cx AS crosses,
           greatest(0.0, least(1.0, CASE WHEN len2 = 0 THEN 0.0
               ELSE ((cx - ax) * ex + (cy - ay) * ey) / len2 END)) AS t
    FROM points, lines
    WHERE (cx > minx - 16 AND cx < maxx + 16 AND cy > miny - 16 AND cy < maxy + 16)
       OR ((ay > cy) <> (ay + ey > cy) AND maxx >= cx)
), pairs AS (
    SELECT *,
           in_box AND (ax + t * ex - cx) * (ax + t * ex - cx)
                    + (ay + t * ey - cy) * (ay + t * ey - cy) < 256 AS touches,
           CASE WHEN crosses THEN ax + (cy - ay) * ex / ey END AS hit_x,
           (cx - ax) * ey - (cy - ay) * ex >= 0 AS front
    FROM near
), checked AS (
    -- The point's sector: the nearest crossed line, on the side facing the point (front
    -- is the right side of v1 -> v2). No crossing at all: outside the map.
    SELECT k1, k2,
           bool_or(blocking) FILTER (touches) AS blocked,
           greatest(arg_min(CASE WHEN front THEN front_floor ELSE back_floor END,
                            {'x': hit_x, 'line': line_id}) FILTER (hit_x >= cx),
                    max(greatest(front_floor, back_floor)) FILTER (touches)) AS floorz,
           least(arg_min(CASE WHEN front THEN front_ceiling ELSE back_ceiling END,
                         {'x': hit_x, 'line': line_id}) FILTER (hit_x >= cx),
                 min(least(front_ceiling, back_ceiling)) FILTER (touches)) AS ceilingz,
           count(*) FILTER (hit_x >= cx) > 0 AS inside
    FROM pairs
    GROUP BY k1, k2
), evaluated AS (
    SELECT points.k1, points.k2, cx, cy, floorz,
           coalesce(inside, false) AND NOT coalesce(blocked, false)
           AND ceilingz - floorz >= 56 AS roomy
    FROM points LEFT JOIN checked ON checked.k1 = points.k1 AND checked.k2 = points.k2
), first_step AS (
    SELECT coalesce(min(e.k1) FILTER (fits), 3) AS c1,
           coalesce(arg_min(cx, e.k1) FILTER (fits), any_value(p.x)) AS x1,
           coalesce(arg_min(cy, e.k1) FILTER (fits), any_value(p.y)) AS y1,
           coalesce(arg_min(floorz, e.k1) FILTER (fits), any_value(p.z)) AS z1
    FROM (SELECT evaluated.*, roomy AND floorz - p.z <= 24 AS fits
          FROM evaluated, p WHERE k2 = -1) e, p
), second_step AS (
    SELECT any_value(c1) AS c1, min(e.k2) FILTER (fits) AS c2,
           coalesce(arg_min(cx, e.k2) FILTER (fits), any_value(x1)) AS x2,
           coalesce(arg_min(cy, e.k2) FILTER (fits), any_value(y1)) AS y2,
           coalesce(arg_min(floorz, e.k2) FILTER (fits), any_value(z1)) AS z2
    FROM (SELECT first_step.*, e.k2, e.cx, e.cy, e.floorz,
                 e.roomy AND e.floorz - first_step.z1 <= 24 AS fits
          FROM first_step LEFT JOIN evaluated e ON e.k1 = first_step.c1 AND e.k2 >= 0) e
), moved AS (
    SELECT map, new_angle, tic, x2 AS x, y2 AS y, z2 AS z,
           CASE WHEN c1 IN (2, 3) OR (steps = 2 AND coalesce(c2, 3) IN (2, 3))
                THEN 0.0 ELSE mx * 0.90625 END AS fx,
           CASE WHEN c1 IN (1, 3) OR (steps = 2 AND coalesce(c2, 3) IN (1, 3))
                THEN 0.0 ELSE my * 0.90625 END AS fy
    FROM p, second_step
)
-- STOPSPEED: without input, a crawl below 1/16 unit per tic stops.
SELECT map, x, y, z, new_angle AS angle,
       CASE WHEN forward = 0 AND side = 0 AND abs(fx) < 0.0625 THEN 0.0 ELSE fx END AS momx,
       CASE WHEN forward = 0 AND side = 0 AND abs(fy) < 0.0625 THEN 0.0 ELSE fy END AS momy,
       tic + 1 AS tic
FROM moved;
