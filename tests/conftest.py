import subprocess
import sys
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


def query(db: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql).fetchall()


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path
