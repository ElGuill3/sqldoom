"""T1: the WAD directory becomes the `lumps` table; bad input is rejected."""

from conftest import query, run_import
from wadbuilder import write_wad


def test_imports_every_lump_with_its_bytes(tmp):
    wad = write_wad(tmp / "a.wad", [("PLAYPAL", b"\x01\x02\x03"), ("F_START", b""), ("DEMO1", b"xyz!")])
    db = tmp / "a.duckdb"

    result = run_import(wad, db)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == f"imported 3 lumps and 0 maps into {db}\n"
    assert query(db, "SELECT idx, name, filepos, size, data FROM lumps ORDER BY idx") == [
        (0, "PLAYPAL", 12, 3, b"\x01\x02\x03"),
        (1, "F_START", 15, 0, b""),
        (2, "DEMO1", 15, 4, b"xyz!"),
    ]


def test_pwad_is_accepted(tmp):
    wad = write_wad(tmp / "p.wad", [("DEMO1", b"a")], magic=b"PWAD")
    db = tmp / "p.duckdb"

    result = run_import(wad, db)

    assert result.returncode == 0
    assert query(db, "SELECT name FROM lumps") == [("DEMO1",)]


def test_rejects_file_that_is_not_a_wad(tmp):
    bogus = tmp / "bogus.wad"
    bogus.write_bytes(b"PK\x03\x04 definitely a zip")
    db = tmp / "x.duckdb"

    result = run_import(bogus, db)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: not a WAD file: {bogus}\n"
    assert not db.exists()


def test_rejects_truncated_directory(tmp):
    wad = tmp / "cut.wad"
    wad.write_bytes(write_wad(tmp / "full.wad", [("DEMO1", b"abcd")]).read_bytes()[:-4])
    db = tmp / "x.duckdb"

    result = run_import(wad, db)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: corrupt WAD file: {wad}\n"
    assert not db.exists()


def test_rejects_missing_file(tmp):
    missing = tmp / "nope.wad"
    db = tmp / "x.duckdb"

    result = run_import(missing, db)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: file not found: {missing}\n"
    assert not db.exists()


def test_refuses_to_overwrite_existing_database(tmp):
    wad = write_wad(tmp / "a.wad", [("DEMO1", b"a")])
    db = tmp / "a.duckdb"
    db.write_bytes(b"precious")

    result = run_import(wad, db)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"error: database already exists: {db} (use --force to replace)\n"
    assert db.read_bytes() == b"precious"


def test_force_replaces_existing_database(tmp):
    db = tmp / "a.duckdb"
    run_import(write_wad(tmp / "old.wad", [("OLD", b"1"), ("OLDER", b"2")]), db)

    result = run_import(write_wad(tmp / "new.wad", [("NEW", b"3")]), db, "--force")

    assert result.returncode == 0
    assert result.stdout == f"imported 1 lumps and 0 maps into {db}\n"
    assert query(db, "SELECT name FROM lumps") == [("NEW",)]
