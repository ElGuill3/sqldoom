"""Decode a map's binary lumps into relational rows.

Each map lump is an array of fixed-size little-endian records. Records refer to
each other by array index (a linedef points at vertex #3), so the record index
becomes the `id` column and every table is keyed by (map, id).
"""

import struct
from collections.abc import Callable
from dataclasses import dataclass

from .wad import CorruptWad, Lump, decode_name

NO_INDEX = 0xFFFF  # "no sidedef" in LINEDEFS
SUBSECTOR_FLAG = 0x8000  # NODES child points at a subsector, not another node

MAP_LUMP_NAMES = {
    "THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS",
    "SSECTORS", "NODES", "SECTORS", "REJECT", "BLOCKMAP",
}


def optional_index(value: int) -> int | None:
    return None if value == NO_INDEX else value


def texture(raw: bytes) -> str | None:
    name = decode_name(raw)
    return None if name in ("", "-") else name


def node_child(value: int) -> tuple[int, bool]:
    return value & ~SUBSECTOR_FLAG, bool(value & SUBSECTOR_FLAG)


@dataclass(frozen=True)
class RecordFormat:
    lump: str
    table: str
    layout: struct.Struct
    columns: str  # SQL column definitions after (map, id)
    decode: Callable[[tuple], tuple] = lambda r: r


FORMATS = [
    RecordFormat(
        "THINGS", "things", struct.Struct("<5h"),
        "x INTEGER, y INTEGER, angle INTEGER, type INTEGER, flags INTEGER",
    ),
    RecordFormat(
        "VERTEXES", "vertexes", struct.Struct("<2h"),
        "x INTEGER, y INTEGER",
    ),
    RecordFormat(
        "LINEDEFS", "linedefs", struct.Struct("<HHhhhHH"),
        "v1 INTEGER, v2 INTEGER, flags INTEGER, special INTEGER, tag INTEGER, "
        "front_sidedef INTEGER, back_sidedef INTEGER",
        lambda r: (*r[:5], optional_index(r[5]), optional_index(r[6])),
    ),
    RecordFormat(
        "SIDEDEFS", "sidedefs", struct.Struct("<hh8s8s8sH"),
        "x_offset INTEGER, y_offset INTEGER, upper_texture VARCHAR, lower_texture VARCHAR, "
        "middle_texture VARCHAR, sector INTEGER",
        lambda r: (r[0], r[1], texture(r[2]), texture(r[3]), texture(r[4]), r[5]),
    ),
    RecordFormat(
        "SECTORS", "sectors", struct.Struct("<hh8s8shhh"),
        "floor_height INTEGER, ceiling_height INTEGER, floor_texture VARCHAR, ceiling_texture VARCHAR, "
        "light_level INTEGER, special INTEGER, tag INTEGER",
        lambda r: (r[0], r[1], texture(r[2]), texture(r[3]), *r[4:]),
    ),
    RecordFormat(
        "SEGS", "segs", struct.Struct("<HHHHhh"),
        "v1 INTEGER, v2 INTEGER, angle INTEGER, linedef INTEGER, direction INTEGER, line_offset INTEGER",
    ),
    RecordFormat(
        "SSECTORS", "ssectors", struct.Struct("<HH"),
        "seg_count INTEGER, first_seg INTEGER",
    ),
    RecordFormat(
        "NODES", "nodes", struct.Struct("<4h4h4hHH"),
        "x INTEGER, y INTEGER, dx INTEGER, dy INTEGER, "
        "right_top INTEGER, right_bottom INTEGER, right_left INTEGER, right_right INTEGER, "
        "left_top INTEGER, left_bottom INTEGER, left_left INTEGER, left_right INTEGER, "
        "right_child INTEGER, right_is_ssector BOOLEAN, left_child INTEGER, left_is_ssector BOOLEAN",
        lambda r: (*r[:12], *node_child(r[12]), *node_child(r[13])),
    ),
]


@dataclass(frozen=True)
class MapData:
    name: str
    lump_idx: int
    rows: dict[str, list[tuple]]  # table -> rows including (map, id, ...)


def decode_records(fmt: RecordFormat, map_name: str, data: bytes) -> list[tuple]:
    if len(data) % fmt.layout.size:
        raise CorruptWad()
    return [(map_name, i, *fmt.decode(rec)) for i, rec in enumerate(fmt.layout.iter_unpack(data))]


def decode_map(lumps: list[Lump], marker: int) -> MapData:
    """Decode the lumps that follow a map marker, up to the first non-map lump."""
    name = lumps[marker].name
    by_name = {}
    for lump in lumps[marker + 1 :]:
        if lump.name not in MAP_LUMP_NAMES:
            break
        by_name[lump.name] = lump.data
    rows = {fmt.table: decode_records(fmt, name, by_name.get(fmt.lump, b"")) for fmt in FORMATS}
    return MapData(name, marker, rows)
