"""Build tiny synthetic WAD files byte by byte for tests."""

import struct
from pathlib import Path


def name8(name: str) -> bytes:
    return name.encode("ascii").ljust(8, b"\0")


def build_wad(lumps: list[tuple[str, bytes]], magic: bytes = b"IWAD") -> bytes:
    """Header (12 bytes), lump data, then the directory (16 bytes per lump)."""
    body = b""
    directory = b""
    offset = 12
    for name, data in lumps:
        directory += struct.pack("<ii", offset, len(data)) + name8(name)
        body += data
        offset += len(data)
    header = magic + struct.pack("<ii", len(lumps), offset)
    return header + body + directory


def write_wad(path: Path, lumps: list[tuple[str, bytes]], magic: bytes = b"IWAD") -> Path:
    path.write_bytes(build_wad(lumps, magic))
    return path


def sample_map_lumps(marker: str = "E1M1") -> list[tuple[str, bytes]]:
    """One small map: a square room split by one BSP node into two subsectors."""
    things = struct.pack("<5h", 64, 96, 90, 1, 7) + struct.pack("<5h", -32, 16, 270, 3004, 4)
    vertexes = struct.pack("<2h", 0, 0) + struct.pack("<2h", 128, 0) + struct.pack("<2h", 128, -128)
    # v1, v2, flags, special, tag, front sidedef, back sidedef (0xFFFF = none)
    linedefs = struct.pack("<HHhhhHH", 0, 1, 1, 0, 0, 0, 0xFFFF) + struct.pack(
        "<HHhhhHH", 1, 2, 4, 11, 3, 0, 1
    )
    sidedefs = (
        struct.pack("<hh", 0, 0) + name8("-") + name8("-") + name8("STARTAN3") + struct.pack("<H", 0)
        + struct.pack("<hh", 16, -8) + name8("BROWN1") + name8("STEP6") + name8("-") + struct.pack("<H", 1)
    )
    # v1, v2, angle (BAM, unsigned), linedef, direction, offset
    segs = struct.pack("<HHHHhh", 0, 1, 0, 0, 0, 0) + struct.pack("<HHHHhh", 1, 2, 49152, 1, 1, 32)
    ssectors = struct.pack("<HH", 1, 0) + struct.pack("<HH", 1, 1)
    # x, y, dx, dy, right bbox (top, bottom, left, right), left bbox, right child, left child
    nodes = struct.pack(
        "<4h4h4hHH", 128, 0, 0, -128, 0, -128, 128, 256, 0, -128, 0, 128, 0x8000 | 0, 0x8000 | 1
    )
    sectors = (
        struct.pack("<hh", 0, 72) + name8("FLOOR4_8") + name8("CEIL3_5") + struct.pack("<hhh", 160, 0, 0)
        + struct.pack("<hh", -16, 128) + name8("NUKAGE1") + name8("F_SKY1") + struct.pack("<hhh", 255, 7, 2)
    )
    return [
        (marker, b""),
        ("THINGS", things),
        ("LINEDEFS", linedefs),
        ("SIDEDEFS", sidedefs),
        ("VERTEXES", vertexes),
        ("SEGS", segs),
        ("SSECTORS", ssectors),
        ("NODES", nodes),
        ("SECTORS", sectors),
        ("REJECT", b"\0"),
        ("BLOCKMAP", b"\0\0"),
    ]
