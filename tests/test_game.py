"""Part 5a (player): the player's state and movement in SQL (odd/tasks/player.md, M1-M6)."""

import duckdb
import pytest

from sqldoom.game import load_game, player, tic
from test_render import make_db
from wadbuilder import map_lumps

WALK, RUN = 0.78125, 1.5625
FRICTION, STOPSPEED = 0.90625, 0.0625


def room_lumps(start=(0, 0, 0), floor=0, ceiling=128, half=128):
    """A square room, walls at x = +-half and y = +-half (clockwise, so fronts face in)."""
    return map_lumps(
        "E1M1",
        vertexes=[(-half, -half), (-half, half), (half, half), (half, -half)],
        linedefs=[(0, 1, 0, None), (1, 2, 1, None), (2, 3, 2, None), (3, 0, 3, None)],
        sidedefs=[0, 0, 0, 0],
        sectors=[(floor, ceiling)],
        things=[] if start is None else [(*start, 1)],
    )


def step_lumps(east_floor=0, east_ceiling=128, flags=0, start=(0, 0, 0)):
    """West sector 0 (x -128..64, floor 0, ceiling 128) and east sector 1 (x 64..256),
    split by a two-sided line at x = 64 whose front faces east."""
    return map_lumps(
        "E1M1",
        vertexes=[(-128, -128), (-128, 128), (64, 128), (256, 128), (256, -128), (64, -128)],
        linedefs=[
            (0, 1, 0, None), (1, 2, 1, None), (2, 3, 2, None),
            (3, 4, 3, None), (4, 5, 4, None), (5, 0, 5, None),
            (5, 2, 6, 7, flags),  # split: front (east) -> sector 1, back (west) -> sector 0
        ],
        sidedefs=[0, 0, 1, 1, 1, 0, 1, 0],
        sectors=[(0, 128), (east_floor, east_ceiling)],
        things=[(*start, 1)],
    )


@pytest.fixture
def game(tmp_path):
    """Open a synthetic map and start the game on it; yields the connection."""
    cons = []

    def start(lumps, **kwargs):
        con = duckdb.connect(str(make_db(tmp_path / f"db{len(cons)}", lumps)), read_only=True)
        cons.append(con)
        load_game(con, "E1M1", **kwargs)
        return con

    yield start
    for con in cons:
        con.close()


def run(con, tics, forward=0, side=0, turn=0, running=False):
    states = []
    for _ in range(tics):
        tic(con, forward, side, turn, running)
        states.append(player(con))
    return states


# M1: state


def test_start_takes_the_player_1_start_and_its_sector_floor(game):
    con = game(room_lumps(start=(-32, 48, 270), floor=8))
    assert player(con) == {"map": "E1M1", "x": -32.0, "y": 48.0, "z": 8.0, "angle": 270.0,
                           "momx": 0.0, "momy": 0.0, "tic": 0}


def test_pos_and_angle_override_the_start(game):
    con = game(room_lumps(), pos=(10.5, -20.0), angle=45.0)
    assert (player(con)["x"], player(con)["y"], player(con)["angle"]) == (10.5, -20.0, 45.0)


def test_a_map_without_player_start_needs_a_position(game, tmp_path):
    with pytest.raises(ValueError, match="map has no player start: E1M1"):
        game(room_lumps(start=None))


def test_a_position_outside_the_map_is_rejected(game):
    with pytest.raises(ValueError, match="position is outside the map: 500.0,0.0"):
        game(room_lumps(), pos=(500.0, 0.0))


def test_starting_again_resets_the_state(game):
    con = game(room_lumps())
    run(con, 3, forward=1)
    load_game(con, "E1M1")
    assert player(con)["x"] == 0.0 and player(con)["tic"] == 0


# M2, M3: turning, thrust, friction


def test_turning_walks_and_runs_at_doom_speed_and_wraps(game):
    con = game(room_lumps())
    assert run(con, 1, turn=1)[-1]["angle"] == 3.515625
    assert run(con, 1, turn=1, running=True)[-1]["angle"] == 3.515625 + 7.03125
    assert run(con, 4, turn=-1)[-1]["angle"] == pytest.approx(360 - 3.515625)
    assert 0 <= player(con)["angle"] < 360


def test_thrust_and_friction_follow_doom_from_rest(game):
    con = game(room_lumps(start=(-100, 0, 0)))
    x, mom = -100.0, 0.0
    for state in run(con, 12, forward=1):
        mom += WALK          # thrust, then the move, then friction
        x += mom
        mom *= FRICTION
        assert state["x"] == pytest.approx(x, abs=1e-9)
        assert state["momx"] == pytest.approx(mom, abs=1e-9)
        assert state["y"] == 0.0 and state["momy"] == 0.0
    assert player(con)["tic"] == 12


def test_walking_converges_to_doom_top_speed(game):
    # Fixed point of mom = (mom + thrust) * friction: thrust * f / (1 - f) after friction,
    # so the move per tic tends to thrust / (1 - f) = 8.333 walking, 16.667 running.
    for running, thrust in ((False, WALK), (True, RUN)):
        con = game(room_lumps(start=(-1000, 0, 0), half=1024))
        states = run(con, 60, forward=1, running=running)  # 0.90625^60 < 0.3%
        assert states[-1]["momx"] == pytest.approx(thrust * FRICTION / (1 - FRICTION), rel=0.01)
        assert states[-1]["x"] - states[-2]["x"] == pytest.approx(thrust / (1 - FRICTION), rel=0.01)


def test_without_input_momentum_decays_and_stops_below_stopspeed(game):
    con = game(room_lumps(start=(-100, 0, 0)))
    con.execute("UPDATE player SET momx = 1.0")
    x, mom = -100.0, 1.0
    for state in run(con, 40):
        x += mom
        mom *= FRICTION
        if abs(mom) < STOPSPEED:
            mom = 0.0
        assert state["x"] == pytest.approx(x, abs=1e-9)
        assert state["momx"] == pytest.approx(mom, abs=1e-12)
    assert player(con)["momx"] == 0.0


def test_momentum_is_clamped_to_maxmove(game):
    con = game(room_lumps(start=(-100, 0, 0)))
    con.execute("UPDATE player SET momx = 100.0, momy = -100.0")
    state = run(con, 1)[-1]
    assert (state["x"], state["y"]) == (-70.0, -30.0)
    assert (state["momx"], state["momy"]) == (30 * FRICTION, -30 * FRICTION)


def test_strafing_moves_along_the_right_vector(game):
    con = game(room_lumps(start=(0, 0, 90)))
    state = run(con, 1, side=1)[-1]
    assert state["x"] == pytest.approx(WALK) and state["y"] == pytest.approx(0, abs=1e-9)
    state = run(con, 1, side=-1)[-1]
    assert state["x"] < WALK


# M4, M5: collisions


def test_a_wall_stops_the_player_at_its_radius(game):
    con = game(room_lumps())
    states = run(con, 200, forward=1)
    assert 112 - 16 < states[-1]["x"] <= 112
    assert states[-1]["momx"] == 0.0
    assert all(s["x"] <= 112 for s in states)


def test_walking_diagonally_into_a_wall_slides_along_it(game):
    con = game(room_lumps(start=(0, -100, 30)))
    states = run(con, 300, forward=1)
    slid = [b for a, b in zip(states, states[1:]) if b["x"] == a["x"] and b["y"] > a["y"]]
    assert slid, "the player never slid along the east wall"
    assert 96 < states[-1]["x"] <= 112 and 96 < states[-1]["y"] <= 112


def test_a_step_of_24_is_climbed_as_soon_as_the_player_touches_it(game):
    con = game(step_lumps(east_floor=24))
    states = run(con, 100, forward=1)
    assert states[-1]["z"] == 24.0 and states[-1]["x"] > 64
    assert all(s["z"] == (24.0 if s["x"] > 48 else 0.0) for s in states)


def test_a_step_of_25_blocks(game):
    con = game(step_lumps(east_floor=25))
    states = run(con, 100, forward=1)
    assert all(s["x"] <= 48 and s["z"] == 0.0 for s in states)


@pytest.mark.parametrize("ceiling,passes", [(55, False), (56, True)])
def test_the_player_needs_56_units_of_headroom(game, ceiling, passes):
    con = game(step_lumps(east_ceiling=ceiling))
    assert (run(con, 100, forward=1)[-1]["x"] > 64) is passes


def test_an_impassable_two_sided_line_blocks(game):
    con = game(step_lumps(flags=1))
    assert all(s["x"] <= 48 for s in run(con, 100, forward=1))


def test_stepping_down_any_drop_is_allowed(game):
    con = game(step_lumps(east_floor=-200))
    states = run(con, 200, forward=1)
    assert states[-1]["z"] == -200.0 and states[-1]["x"] > 80


def test_running_fast_into_a_thin_line_never_tunnels(game):
    con = game(step_lumps(flags=1), pos=(40.0, 0.0), angle=0.0)
    con.execute("UPDATE player SET momx = 30.0")
    assert all(s["x"] <= 48 for s in run(con, 50, forward=1, running=True))


def test_moves_longer_than_the_radius_are_split_in_half_steps(game):
    # An impassable line at x = 64 and a move of 30 from x = 20: one step to x = 50 would
    # touch it and be refused; in half steps the first (to 35) is free, the second blocks.
    con = game(step_lumps(flags=1), pos=(20.0, 0.0), angle=0.0)
    con.execute("UPDATE player SET momx = 30.0")
    state = run(con, 1)[-1]
    assert (state["x"], state["momx"]) == (35.0, 0.0)


# M6: purity


def test_game_step_is_pure_and_tic_updates_the_one_row(game):
    con = game(room_lumps())
    before = player(con)
    first = con.execute("SELECT * FROM game_step(1, 0, 0, false)").fetchall()
    assert con.execute("SELECT * FROM game_step(1, 0, 0, false)").fetchall() == first
    assert player(con) == before
    tic(con, 1, 0, 0, False)
    assert con.execute("SELECT count(*) FROM player").fetchone() == (1,)
    assert player(con)["tic"] == 1


@pytest.mark.parametrize("args", [(2, 0, 0, False), (0, 0.5, 0, False), (0, 0, 0, "yes")])
def test_tic_rejects_inputs_outside_doom_controls(game, args):
    con = game(room_lumps())
    with pytest.raises(ValueError):
        tic(con, *args)
    assert player(con)["tic"] == 0
