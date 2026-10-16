import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import duckdb
import pytest


def run_module(module: str, *args, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, *map(str, args)],
        capture_output=True,
        text=text,
    )


def run_import(*args) -> subprocess.CompletedProcess:
    return run_module("sqldoom.cli", *args)


def run_automap(*args, text: bool = True) -> subprocess.CompletedProcess:
    return run_module("sqldoom.automap", *args, text=text)


def run_render(*args) -> subprocess.CompletedProcess:
    return run_module("sqldoom.render", *args)


def query(db: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql).fetchall()


def render_query(db: Path, sql: str) -> list[tuple]:
    """Run SQL with the render macros loaded (TEMP macros live per connection)."""
    with duckdb.connect(str(db)) as con:
        con.execute(files("sqldoom").joinpath("sql/render.sql").read_text())
        return con.execute(sql).fetchall()


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path
