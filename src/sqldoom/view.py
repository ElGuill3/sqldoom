"""sqldoom-view: walk around a map in the terminal. SQL renders, Python only drives.

The viewer owns the terminal and the camera (design D9 in odd/tasks/first-person.md):
every frame is one render_ansi query on a single read-only connection, and the
terminal is put into raw input mode inside the alternate screen with synchronized
updates, always restored on exit, Ctrl-C, error, or a kill signal (SIGTERM/SIGHUP).
Without --size the frame follows terminal resizes (SIGWINCH, redrawn from the
self-pipe in the main loop). Part 5 moves the player state into SQL.
"""

import argparse
import math
import os
import select
import signal
import sys
import termios
import time
from pathlib import Path

from .common import MIN_SIZE, MAX_SIZE, NoTextures, fail, load_render, open_map, parse_pos, parse_size

MOVE, TURN = 16.0, 7.5
KEY_HELP = "w/s move  a/d turn  q quit"

# Raw key bytes -> actions. Arrow keys arrive as three-byte CSI sequences; a lone
# ESC (nothing else buffered) and Ctrl-C (0x03, since ISIG is off) quit.
KEY_ACTIONS = {
    b"w": "forward", b"\x1b[A": "forward",
    b"s": "back", b"\x1b[B": "back",
    b"a": "left", b"\x1b[D": "left",
    b"d": "right", b"\x1b[C": "right",
    b"q": "quit", b"\x1b": "quit", b"\x03": "quit",
}


def parse_keys(data: bytes) -> list[str]:
    """Actions for one raw read, in order; unknown bytes are ignored.

    ESC + [A/B/C/D is an arrow sequence; an ESC followed by anything else starts an
    unknown sequence and is dropped; a lone trailing ESC quits.
    """
    actions: list[str] = []
    i = 0
    while i < len(data):
        if data[i:i + 3] in KEY_ACTIONS:  # arrow sequences first
            actions.append(KEY_ACTIONS[data[i:i + 3]])
            i += 3
        elif data[i:i + 1] in KEY_ACTIONS:
            if data[i:i + 1] == b"\x1b" and i + 1 < len(data):
                i += 1  # ESC starting an unknown sequence: drop it, keep parsing
            else:
                actions.append(KEY_ACTIONS[data[i:i + 1]])
                i += 1
        else:
            i += 1  # unknown byte
    return actions


def step(camera: tuple[float, float, float], action: str) -> tuple[float, float, float]:
    """One camera update (noclip): forward/back 16 units along the facing,
    left/right 7.5 degrees, angle kept in [0, 360)."""
    x, y, angle = camera
    if action == "forward":
        x += MOVE * math.cos(math.radians(angle))
        y += MOVE * math.sin(math.radians(angle))
    elif action == "back":
        x -= MOVE * math.cos(math.radians(angle))
        y -= MOVE * math.sin(math.radians(angle))
    elif action == "left":
        angle += TURN
    elif action == "right":
        angle -= TURN
    return (x, y, angle % 360.0)


def frame(con, map_name: str, camera: tuple[float, float, float], cols: int, rows: int,
          clear: bool = False) -> str:
    """One synchronized frame: cursor home, the render_ansi text, the status line.

    With clear (a resize redraw) the screen is erased first so no cells of the old
    frame size remain. The render time doubles as the fps the status line reports.
    """
    start = time.perf_counter()
    (text,) = con.execute(
        "SELECT text FROM render_ansi(?, ?, ?, ?, ?, ?)",
        [map_name, *camera, cols, rows],
    ).fetchone()
    fps = 1.0 / (time.perf_counter() - start)
    x, y, angle = camera
    status = (f"{map_name}  x={int(round(x))} y={int(round(y))} angle={int(round(angle))}"
              f"  {fps:.1f} fps  {KEY_HELP}")
    return f"\x1b[?2026h{'\x1b[2J' if clear else ''}\x1b[H{text}{status}\x1b[?2026l"


def raw_input_mode(fd: int):
    """Raw input (no echo, canonical mode, or signals) with terminal output
    post-processing (OPOST/ONLCR) kept on, so the \\n that ends every render_ansi
    row still returns the cursor to column 1 for the next row. Returns the old
    termios settings for restore_termios."""
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
    new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    return old


def restore_termios(fd: int, old, out=None) -> None:
    """Put the terminal back the way it was; never raises. If the terminal is
    already gone (SIGHUP after the tab closed) the write/tcsetattr fail with
    OSError, and there is nothing left to restore anyway."""
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        (out or sys.stdout).write("\x1b[?25h\x1b[?1049l")  # show cursor, leave alt screen
        (out or sys.stdout).flush()
    except OSError:
        pass


def default_terminal_size() -> tuple[int, int]:
    """Terminal columns x rows minus one status row, clamped to the 8..4096 bounds."""
    cols, rows = os.get_terminal_size()
    return (min(MAX_SIZE, max(MIN_SIZE, cols)),
            min(MAX_SIZE, max(MIN_SIZE, rows - 1)))


def read_input(fd: int) -> bytes | None:
    """One raw read of up to 4096 bytes; None on EOF (end of stdin)."""
    data = os.read(fd, 4096)
    return data or None


def run_viewer(con, map_name: str, camera: tuple[float, float, float], cols: int, rows: int,
               *, size_locked: bool = False, fd: int | None = None, out=None,
               reader=read_input) -> int:
    """The input loop: re-render after input or a terminal resize changed the frame.
    Always restores the terminal exactly once, including on quit (return 0), EOF,
    Ctrl-C, exceptions, and SIGTERM/SIGHUP (return 128 + signal number).

    Signals are delivered through a self-pipe: the handlers only write the signal
    number, and the main loop (selecting on both stdin and the pipe) does all the
    drawing and restoring, so it never runs inside a signal handler.
    """
    fd = sys.stdin.fileno() if fd is None else fd
    out = sys.stdout if out is None else out
    pipe_r, pipe_w = os.pipe()
    os.set_blocking(pipe_w, False)  # a full pipe must never block inside a signal handler
    handled = (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)
    old_handlers = {sig: signal.getsignal(sig) for sig in handled}

    def on_signal(signum, _frame):
        try:
            os.write(pipe_w, bytes([signum]))
        except OSError:
            pass

    for sig in handled:
        signal.signal(sig, on_signal)
    old_termios = raw_input_mode(fd)
    exit_code: int | None = None
    try:
        out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        out.write(frame(con, map_name, camera, cols, rows))  # once at start
        out.flush()
        dirty = False
        while exit_code is None:
            ready, _, _ = select.select([fd, pipe_r], [], [])
            if pipe_r in ready:
                for signum in os.read(pipe_r, 4096):
                    if signum == signal.SIGWINCH:
                        if not size_locked:
                            cols, rows = default_terminal_size()
                            dirty = True
                    else:
                        exit_code = 128 + signum  # SIGTERM -> 143, SIGHUP -> 129
            if exit_code is None and fd in ready:
                data = reader(fd)
                if data is None:
                    return 0  # EOF on stdin: quit cleanly
                actions = parse_keys(data)
                if "quit" in actions:
                    return 0
                for action in actions:  # all pending keys, in order
                    camera = step(camera, action)
                if actions:
                    # A resize in the same batch still needs the screen cleared.
                    out.write(frame(con, map_name, camera, cols, rows, clear=dirty))
                    out.flush()
                    dirty = False
            if exit_code is None and dirty:
                out.write(frame(con, map_name, camera, cols, rows, clear=True))
                out.flush()
                dirty = False
        return exit_code
    finally:
        restore_termios(fd, old_termios, out)  # exactly once, on every path
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(pipe_r)
        os.close(pipe_w)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-view",
        description="Walk around a map in the terminal, rendered entirely in SQL.",
    )
    parser.add_argument("database", type=Path, help="database created by sqldoom-import")
    parser.add_argument("map", help="map name, e.g. E1M1 (case-insensitive)")
    parser.add_argument(
        "--size", type=parse_size, metavar="COLSxROWS",
        help=f"terminal size in characters (default: the terminal size minus one status "
        f"row, and the frame follows terminal resizes), both between {MIN_SIZE} and {MAX_SIZE}",
    )
    parser.add_argument(
        "--pos", type=parse_pos, metavar="X,Y",
        help="camera position (default: the map's player 1 start)",
    )
    parser.add_argument(
        "--angle", type=float, metavar="DEG",
        help="camera angle in degrees, 0 = east, 90 = north (default: the player 1 start angle)",
    )
    args = parser.parse_args(argv)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return fail("sqldoom-view needs an interactive terminal")
    map_name = args.map.upper()

    opened = open_map(args.database, map_name)
    if opened is None:
        return 1
    con, map_found = opened
    try:
        if not map_found:
            return fail(f"map not found: {map_name}")
        try:
            load_render(con)  # once
        except NoTextures as exc:
            return fail(str(exc))
        # Same camera resolution as sqldoom-render: each component defaults to the
        # player 1 start, --pos and --angle override independently.
        player = con.execute(
            "SELECT x, y, angle FROM things WHERE map = ? AND type = 1 ORDER BY id LIMIT 1",
            [map_name],
        ).fetchone()
        if args.pos is None and player is None:
            return fail(f"map has no player start: {map_name}")
        px, py = args.pos if args.pos is not None else (player[0], player[1])
        angle = args.angle if args.angle is not None else (player[2] if player else 0.0)
        cols, rows = args.size if args.size else default_terminal_size()
        return run_viewer(con, map_name, (px, py, angle), cols, rows,
                          size_locked=args.size is not None)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
