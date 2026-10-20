"""sqldoom-view: walk around a map in the terminal. SQL renders, Python only drives.

The viewer owns the terminal (design D9 in odd/tasks/first-person.md): every frame is
one render_ansi query on a read-only connection, and the terminal is put into raw input
mode inside the alternate screen with synchronized updates, always restored on exit,
Ctrl-C, error, or a kill signal (SIGTERM/SIGHUP). Without --size the frame follows
terminal resizes (SIGWINCH, redrawn from the self-pipe in the main loop).

The player lives in SQL (Part 5a, M7 in odd/tasks/player.md): the game runs Doom's
fixed 35 Hz tics, each one UPDATE over game_step, on its own single-threaded DuckDB
instance (a tic is a small query, where thread scheduling costs more than it saves),
and the renderer draws the player row once per frame on the parallel connection.
"""

import argparse
import os
import select
import signal
import sys
import termios
import time
from pathlib import Path

import duckdb

from .common import MIN_SIZE, MAX_SIZE, NoTextures, fail, load_render, open_map, parse_pos, parse_size

from .game import load_game, player, tic

KEY_HELP = "w/s move  a/d turn  ,/. strafe  shift run  q quit"
TICRATE = 35  # Doom's game tics per second
MAX_TICS_PER_FRAME = 10  # a slower frame drops tics (the game slows) instead of spiraling

# Terminals report key presses and auto-repeats but never releases. A fresh press counts
# as held long enough to bridge the usual delay before auto-repeat starts (~0.5 s);
# each repeat keeps the key held a little past it.
FIRST_HOLD, REPEAT_HOLD = 0.5, 0.15

# Raw key bytes -> actions. Arrow keys arrive as three-byte CSI sequences and Shift +
# arrow as CSI 1;2 A-D; uppercase letters (Shift) run. A lone ESC (nothing else
# buffered) and Ctrl-C (0x03, since ISIG is off) quit.
KEY_ACTIONS = {
    b"w": ("forward",), b"\x1b[A": ("forward",), b"W": ("run", "forward"),
    b"s": ("back",), b"\x1b[B": ("back",), b"S": ("run", "back"),
    b"a": ("left",), b"\x1b[D": ("left",), b"A": ("run", "left"),
    b"d": ("right",), b"\x1b[C": ("right",), b"D": ("run", "right"),
    b"\x1b[1;2A": ("run", "forward"), b"\x1b[1;2B": ("run", "back"),
    b"\x1b[1;2D": ("run", "left"), b"\x1b[1;2C": ("run", "right"),
    b",": ("strafe_left",), b".": ("strafe_right",),
    b"q": ("quit",), b"\x1b": ("quit",), b"\x03": ("quit",),
}


def parse_keys(data: bytes) -> list[str]:
    """Actions for one raw read, in order; unknown bytes are ignored.

    ESC + [A/B/C/D is an arrow sequence; an ESC followed by anything else starts an
    unknown sequence and is dropped; a lone trailing ESC quits.
    """
    actions: list[str] = []
    i = 0
    while i < len(data):
        if data[i:i + 6] in KEY_ACTIONS:  # Shift + arrow sequences first
            actions.extend(KEY_ACTIONS[data[i:i + 6]])
            i += 6
        elif data[i:i + 3] in KEY_ACTIONS:  # then arrow sequences
            actions.extend(KEY_ACTIONS[data[i:i + 3]])
            i += 3
        elif data[i:i + 1] in KEY_ACTIONS:
            if data[i:i + 1] == b"\x1b" and i + 1 < len(data):
                i += 1  # ESC starting an unknown sequence: drop it, keep parsing
            else:
                actions.extend(KEY_ACTIONS[data[i:i + 1]])
                i += 1
        else:
            i += 1  # unknown byte
    return actions


class Keys:
    """Which actions count as held, from press times alone (terminals send no releases)."""

    def __init__(self) -> None:
        self.until: dict[str, float] = {}

    def press(self, actions: list[str], now: float) -> None:
        for action in actions:
            held_until = self.until.get(action, float("-inf"))
            # A press while the key still counts as held is an auto-repeat.
            hold = REPEAT_HOLD if held_until >= now else FIRST_HOLD
            self.until[action] = max(held_until, now + hold)

    def controls(self, now: float) -> tuple[int, int, int, bool]:
        """(forward, side, turn, run) for game_step: opposite keys cancel out."""
        held = {action for action, until in self.until.items() if until > now}
        return (("forward" in held) - ("back" in held),
                ("strafe_right" in held) - ("strafe_left" in held),
                ("left" in held) - ("right" in held),
                "run" in held)


def due_tics(start: float, now: float, done: int, cap: int = MAX_TICS_PER_FRAME) -> int:
    """How many tics the 35 Hz clock started at `start` owes after `done`, at most cap."""
    return max(0, min(cap, int((now - start) * TICRATE) - done))


def frame(con, state: dict, cols: int, rows: int, started: float, clear: bool = False) -> str:
    """One synchronized frame: cursor home, the render_ansi text, the status line.

    With clear (a resize redraw) the screen is erased first so no cells of the old
    frame size remain. The fps reported is that of this frame's work since `started`
    (perf_counter): its tics and its render, back to back.
    """
    (text,) = con.execute(
        "SELECT text FROM render_ansi(?, ?, ?, ?, ?, ?)",
        [state["map"], state["x"], state["y"], state["angle"], cols, rows],
    ).fetchone()
    fps = 1.0 / max(time.perf_counter() - started, 1e-6)
    speed = (state["momx"] ** 2 + state["momy"] ** 2) ** 0.5
    status = (f"{state['map']}  x={int(round(state['x']))} y={int(round(state['y']))}"
              f" angle={int(round(state['angle']))} z={int(round(state['z']))}"
              f" speed={speed:.1f}  {fps:.1f} fps  {KEY_HELP}")
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
               *, game=None, size_locked: bool = False, fd: int | None = None, out=None,
               reader=read_input) -> int:
    """The game loop: run the tics the 35 Hz clock owes with the keys held now, then draw
    a frame when the player changed or the terminal was resized. Always restores the
    terminal exactly once, including on quit (return 0), EOF, Ctrl-C, exceptions, and
    SIGTERM/SIGHUP (return 128 + signal number).

    `game` is a connection with the game already started (load_game); without it the
    game starts on `con` at `camera`. While no key is held and the player is at rest no
    tic can change anything, so the clock runs without querying.

    Signals are delivered through a self-pipe: the handlers only write the signal
    number, and the main loop (selecting on stdin, the pipe and the tic clock) does all
    the drawing and restoring, so it never runs inside a signal handler.
    """
    if game is None:
        game = con
        load_game(game, map_name, pos=camera[:2], angle=camera[2])
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

    def draw(state: dict, started: float, clear: bool = False) -> None:
        out.write(frame(con, state, cols, rows, started, clear))
        out.flush()

    for sig in handled:
        signal.signal(sig, on_signal)
    old_termios = raw_input_mode(fd)
    exit_code: int | None = None
    try:
        out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        state = player(game)
        draw(state, time.perf_counter())  # once at start
        keys, start, done, dirty = Keys(), time.monotonic(), 0, False
        while exit_code is None:
            wait = max(0.0, start + (done + 1) / TICRATE - time.monotonic())
            ready, _, _ = select.select([fd, pipe_r], [], [], wait)
            if pipe_r in ready:
                for signum in os.read(pipe_r, 4096):
                    if signum == signal.SIGWINCH:
                        if not size_locked:
                            cols, rows = default_terminal_size()
                            dirty = True
                    else:
                        exit_code = 128 + signum  # SIGTERM -> 143, SIGHUP -> 129
            if exit_code is not None:
                break
            if fd in ready:
                data = reader(fd)
                if data is None:
                    return 0  # EOF on stdin: quit cleanly
                actions = parse_keys(data)
                if "quit" in actions:
                    return 0
                keys.press(actions, time.monotonic())
            work_started = time.perf_counter()
            now = time.monotonic()
            behind = int((now - start) * TICRATE) - done
            if behind > MAX_TICS_PER_FRAME:
                done += behind - MAX_TICS_PER_FRAME  # drop what a slow frame cannot catch up
            before = state
            for _ in range(due_tics(start, now, done)):
                forward, side, turn, run = keys.controls(now)
                if (forward, side, turn) != (0, 0, 0) or state["momx"] or state["momy"]:
                    tic(game, forward, side, turn, run)
                    state = player(game)
                done += 1
            if dirty or state is not before:
                draw(state, work_started, clear=dirty)  # a resize clears the old frame
                dirty = False
        return exit_code
    finally:
        restore_termios(fd, old_termios, out)  # exactly once, on every path
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(pipe_r)
        os.close(pipe_w)


def open_game(database: Path):
    """A second, single-threaded DuckDB instance for the game tics, reading the same
    database file read-only (M7)."""
    game = duckdb.connect(":memory:", config={"threads": 1})
    quoted = str(database).replace("'", "''")
    game.execute(f"ATTACH '{quoted}' AS doom (READ_ONLY)")
    game.execute("USE doom")
    return game


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqldoom-view",
        description="Play a map in the terminal: the player and the frame are computed in SQL.",
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
        help="start position (default: the map's player 1 start)",
    )
    parser.add_argument(
        "--angle", type=float, metavar="DEG",
        help="start angle in degrees, 0 = east, 90 = north (default: the player 1 start angle)",
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
        game = open_game(args.database)
        try:
            try:
                load_game(game, map_name, pos=(px, py), angle=angle)
            except ValueError as exc:
                return fail(str(exc))
            cols, rows = args.size if args.size else default_terminal_size()
            return run_viewer(con, map_name, (px, py, angle), cols, rows, game=game,
                              size_locked=args.size is not None)
        finally:
            game.close()
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
