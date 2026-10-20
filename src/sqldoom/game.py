"""Part 5a (player): the player's state lives in a TEMP table and every game tic is one
SQL UPDATE over the game_step macro (sql/game.sql). TEMP keeps the imported database
read-only: the state belongs to the session and resets with it."""

from importlib.resources import files as resource_files

GAME_SQL = resource_files("sqldoom").joinpath("sql/game.sql").read_text()
COLUMNS = ("map", "x", "y", "z", "angle", "momx", "momy", "tic")


def load_game(con, map_name: str, pos=None, angle=None) -> None:
    """Create the game macros and (re)start the player on map_name: at the player 1
    start, or at pos/angle (each overrides independently, as in sqldoom-render), standing
    on its sector's floor and at rest. Raises ValueError when there is no start to fall
    back on or the position is outside the map."""
    con.execute(GAME_SQL)
    start = con.execute(
        "SELECT x, y, angle FROM things WHERE map = ? AND type = 1 ORDER BY id LIMIT 1",
        [map_name],
    ).fetchone()
    if pos is None and start is None:
        raise ValueError(f"map has no player start: {map_name}")
    x, y = (float(pos[0]), float(pos[1])) if pos is not None else (float(start[0]), float(start[1]))
    a = float(angle if angle is not None else (start[2] if start else 0.0))
    floor = con.execute("SELECT floor FROM game_sector(?, ?, ?)", [map_name, x, y]).fetchone()
    if floor is None or floor[0] is None:
        raise ValueError(f"position is outside the map: {x},{y}")
    con.execute("INSERT INTO player VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 0)",
                [map_name, x, y, float(floor[0]), a])


def tic(con, forward: int, side: int, turn: int, run: bool) -> None:
    """Advance the game one tic with this input (M2): forward/side/turn in -1, 0, 1."""
    for name, value in (("forward", forward), ("side", side), ("turn", turn)):
        if type(value) is not int or value not in (-1, 0, 1):
            raise ValueError(f"{name} must be -1, 0 or 1, got {value!r}")
    if type(run) is not bool:
        raise ValueError(f"run must be a bool, got {run!r}")
    # Validated literals: the values are spliced in so the macro sees constants.
    con.execute(
        "UPDATE player SET x = s.x, y = s.y, z = s.z, angle = s.angle, momx = s.momx, "
        f"momy = s.momy, tic = s.tic FROM game_step({forward}, {side}, {turn}, {run}) s"
    )


def player(con) -> dict:
    """The current player row as a dict (M1 columns)."""
    return dict(zip(COLUMNS, con.execute(f"SELECT {', '.join(COLUMNS)} FROM player").fetchone()))
