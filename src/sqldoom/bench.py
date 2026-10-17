"""sqldoom-bench: time the first-person renderer on this machine.

It separates the two costs of the viewer: the SQL that computes a frame
(render_ansi) and the terminal that has to consume the frame's text. Run it on
each machine to see which one limits the frame rate there.
"""

import argparse
import os
import statistics
import sys
import termios
import time
from pathlib import Path

from .common import NoTextures, fail, load_render, open_map, parse_pos, parse_size

TERMINAL_SIZE = (160, 50)
DEFAULT_FRAMES = 20


def parse_frames(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 1000:
        raise argparse.ArgumentTypeError(f"expected a whole number between 1 and 1000, got '{value}'")
    return int(value)


def summary(label: str, seconds: list[float], extra: str) -> str:
    ms = [s * 1000 for s in seconds]
    return f"{label:<20}avg {statistics.mean(ms):.1f} ms  min {min(ms):.1f}  max {max(ms):.1f}  ({extra})"


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def time_terminal(fd: int, frame: bytes, frames: int) -> list[float]:
    """Write the frame like the viewer does and wait until the terminal has read it."""
    times = []
    write_all(fd, b"\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        for _ in range(frames):
            start = time.perf_counter()
            write_all(fd, b"\x1b[?2026h\x1b[H" + frame + b"\x1b[?2026l")
            termios.tcdrain(fd)  # returns once the terminal has consumed the bytes
            times.append(time.perf_counter() - start)
    finally:
        write_all(fd, b"\x1b[?25h\x1b[?1049l")  # show cursor, leave alternate screen
    return times


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-bench",
        description="Time the first-person renderer: SQL per frame and, when stdout is a terminal, "
        "the time the terminal needs to display each frame.",
    )
    parser.add_argument("database", type=Path, help="database created by sqldoom-import")
    parser.add_argument("map", help="map name, e.g. E1M1 (case-insensitive)")
    parser.add_argument(
        "--size", type=parse_size, metavar="COLSxROWS",
        help=f"frame size in terminal characters (default {TERMINAL_SIZE[0]}x{TERMINAL_SIZE[1]}), "
        "both between 8 and 4096",
    )
    parser.add_argument(
        "--frames", type=parse_frames, default=DEFAULT_FRAMES, metavar="N",
        help=f"frames to time, after one warm-up frame (default {DEFAULT_FRAMES}, at most 1000)",
    )
    parser.add_argument("--pos", type=parse_pos, metavar="X,Y", help="camera position (default: player 1 start)")
    parser.add_argument("--angle", type=float, metavar="DEG", help="camera angle (default: player 1 start angle)")
    args = parser.parse_args(argv)
    map_name = args.map.upper()
    cols, rows = args.size or TERMINAL_SIZE

    opened = open_map(args.database, map_name)
    if opened is None:
        return 1
    con, map_found = opened
    with con:
        if not map_found:
            return fail(f"map not found: {map_name}")
        start = time.perf_counter()
        try:
            load_render(con)
        except NoTextures as exc:
            return fail(str(exc))
        load_seconds = time.perf_counter() - start

        player = con.execute(
            "SELECT x, y, angle FROM things WHERE map = ? AND type = 1 ORDER BY id LIMIT 1", [map_name]
        ).fetchone()
        if args.pos is None and player is None:
            return fail(f"map has no player start: {map_name}")
        px, py = args.pos if args.pos is not None else (player[0], player[1])
        angle = args.angle if args.angle is not None else (player[2] if player else 0.0)

        query = "SELECT text FROM render_ansi(?, ?, ?, ?, ?, ?)"
        params = [map_name, px, py, angle, cols, rows]
        (text,) = con.execute(query, params).fetchone()  # warm-up
        sql_times = []
        for _ in range(args.frames):
            start = time.perf_counter()
            con.execute(query, params).fetchone()
            sql_times.append(time.perf_counter() - start)
        threads = con.execute("SELECT current_setting('threads')").fetchone()[0]

    frame = text.encode()
    stdout = sys.stdout.fileno()
    terminal_times = time_terminal(stdout, frame, args.frames) if os.isatty(stdout) else None

    sql_avg = statistics.mean(sql_times)
    lines = [
        f"map {map_name}  size {cols}x{rows} ({cols}x{rows * 2} pixels)  frames {args.frames}",
        f"{'load render.sql':<20}{load_seconds * 1000:.1f} ms",
        summary("sql per frame", sql_times, f"{1 / sql_avg:.1f} fps"),
    ]
    if terminal_times is None:
        lines.append(f"{'terminal per frame':<20}skipped (stdout is not a terminal)")
        lines.append(f"{'viewer estimate':<20}{1 / sql_avg:.1f} fps (sql only)")
    else:
        lines.append(summary("terminal per frame", terminal_times, f"{len(frame)} bytes"))
        lines.append(f"{'viewer estimate':<20}{1 / (sql_avg + statistics.mean(terminal_times)):.1f} fps")
    lines.append(f"{'duckdb threads':<20}{threads} (cpu count {os.cpu_count()})")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
