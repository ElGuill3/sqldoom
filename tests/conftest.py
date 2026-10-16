import subprocess
import sys
from pathlib import Path

import duckdb
import pytest


def run_import(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sqldoom.cli", *map(str, args)],
        capture_output=True,
        text=True,
    )


def query(db: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql).fetchall()


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path
