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


def map_lumps(
    marker, vertexes, linedefs, sidedefs, sectors, things,
    segs=(), ssectors=(), nodes=(),
) -> list[tuple[str, bytes]]:
    """A map from plain tuples.

    linedefs: (v1, v2, front_sidedef, back_sidedef or None); sidedefs: sector, or
    (sector, middle, upper, lower) textures; sectors: (floor, ceiling), or
    (floor, ceiling, floor_texture, ceiling_texture, light_level); things:
    (x, y, type), or (x, y, angle, type) with angle in degrees. BSP lumps default to
    empty (the automap does not need them): segs: (v1, v2, angle_bam, linedef,
    direction, offset); ssectors: (seg_count, first_seg); nodes: (x, y, dx, dy,
    right_child, left_child, right_is_ssector, left_is_ssector).
    """
    def sidedef(s) -> bytes:
        # WAD SIDEDEFS store upper, lower, middle; tuples here are (sector, middle, upper, lower).
        sector, *textures = s if isinstance(s, tuple) else (s,)
        middle, upper, lower = (*textures, *(["-"] * 3))[:3]
        return (struct.pack("<hh", 0, 0)
                + name8(upper) + name8(lower) + name8(middle)
                + struct.pack("<H", sector))

    return [
        (marker, b""),
        ("THINGS", b"".join(
            struct.pack("<5h", x, y, *(rest if len(rest) == 2 else (90, rest[0])), 7)
            for x, y, *rest in things
        )),
        ("LINEDEFS", b"".join(
            struct.pack("<HHhhhHH", a, b, 0, 0, 0, front, 0xFFFF if back is None else back)
            for a, b, front, back in linedefs
        )),
        ("SIDEDEFS", b"".join(sidedef(s) for s in sidedefs)),
        ("VERTEXES", b"".join(struct.pack("<2h", x, y) for x, y in vertexes)),
        ("SEGS", b"".join(struct.pack("<HHHHhh", *s) for s in segs)),
        ("SSECTORS", b"".join(struct.pack("<HH", *ss) for ss in ssectors)),
        ("NODES", b"".join(
            struct.pack("<4h4h4h", x, y, dx, dy, 0, 0, 0, 0, 0, 0, 0, 0)
            + struct.pack("<HH",
                          right | (0x8000 if right_is_ssector else 0),
                          left | (0x8000 if left_is_ssector else 0))
            for x, y, dx, dy, right, left, right_is_ssector, left_is_ssector in nodes
        )),
        ("SECTORS", b"".join(
            struct.pack("<hh", rec[0], rec[1])
            + name8(rec[2] if len(rec) > 2 else "FLOOR4_8")
            + name8(rec[3] if len(rec) > 3 else "CEIL3_5")
            + struct.pack("<hhh", rec[4] if len(rec) > 4 else 160, 0, 0)
            for rec in sectors
        )),
    ]


def room_map_lumps(marker: str = "E1M1", right_floor: int = 0, right_ceiling: int = 128):
    """A 180x160 room split at x=90 by a two-sided line into two sectors.

    The left sector has floor 0 / ceiling 128; the right one uses the given heights.
    Player 1 starts at (40, 80); an imp (type 3004) stands at (135, 80).
    """
    return map_lumps(
        marker,
        vertexes=[(0, 0), (180, 0), (180, 160), (0, 160), (90, 0), (90, 160)],
        linedefs=[(0, 1, 0, None), (1, 2, 0, None), (2, 3, 0, None), (3, 0, 0, None), (4, 5, 1, 2)],
        sidedefs=[0, 0, 1],
        sectors=[(0, 128), (right_floor, right_ceiling)],
        things=[(40, 80, 1), (135, 80, 3004)],
    )


# Part 4a: graphics lumps. PLAYPAL is 768 bytes per palette (doom1.wad carries several
# back to back; the importer uses palette 0 only). COLORMAP is 34 levels of 256 entries.


def playpal(palettes: list[list[tuple[int, int]]] | None = None) -> bytes:
    """PLAYPAL: one or more 256-entry palettes; entry i defaults to (i, 255-i, i//2)."""
    if palettes is None:
        palettes = [[(i, 255 - i, i // 2) for i in range(256)]]
    return bytes(b for pal in palettes for rgb in pal for b in rgb)


def colormap(levels: list[list[int]] | None = None) -> bytes:
    """COLORMAP: 34 lighting levels; level k defaults to entry c shifted by k."""
    if levels is None:
        levels = [[(c + k) % 256 for c in range(256)] for k in range(34)]
    return bytes(b for level in levels for b in level)


def picture(width: int, height: int, columns: dict[int, list[tuple[int, list[int]]]],
            leftoffset: int = 0, topoffset: int = 0) -> bytes:
    """A picture-format patch (wall texture or sprite): header, per-column offsets, then
    each column as posts of (topdelta, length, pad, pixels, pad), 0xFF ending the column.
    columns maps u to a list of (topdelta, pixels) posts; a column with no posts is
    transparent (still gets an offset, unlike some real WADs that reuse the previous)."""
    header = struct.pack("<4h", width, height, leftoffset, topoffset)
    offsets: list[int] = []
    blobs: list[bytes] = []
    pos = PICTURE_HEADER_SIZE + 4 * width
    for u in range(width):
        offsets.append(pos)
        blob = b""
        for topdelta, pixels in columns.get(u, []):
            blob += struct.pack("<2B", topdelta, len(pixels)) + b"\0" + bytes(pixels) + b"\0"
        blob += b"\xff"
        blobs.append(blob)
        pos += len(blob)
    return header + b"".join(struct.pack("<i", o) for o in offsets) + b"".join(blobs)


PICTURE_HEADER_SIZE = 8  # int16 width, height, leftoffset, topoffset


def pnames(names: list[str]) -> bytes:
    """PNAMES: int32 count, then 8-char patch lump names."""
    return struct.pack("<i", len(names)) + b"".join(name8(n) for n in names)


def texture_defs(entries: list[tuple[str, int, int, list[tuple[int, int, int]]]]) -> bytes:
    """A TEXTURE1/TEXTURE2 lump. Each entry is (name, width, height, patches) with
    patches as (originx, originy, PNAMES index); masked/obsolete/stepdir/colormap are 0."""
    offsets: list[int] = []
    blobs: list[bytes] = []
    pos = 4 + 4 * len(entries)
    for name, width, height, patches in entries:
        offsets.append(pos)
        blob = (name8(name) + struct.pack("<IhhIh", 0, width, height, 0, len(patches))
                + b"".join(struct.pack("<5h", ox, oy, p, 0, 0) for ox, oy, p in patches))
        blobs.append(blob)
        pos += len(blob)
    return struct.pack("<i", len(entries)) + b"".join(struct.pack("<i", o) for o in offsets) + b"".join(blobs)


FLAT_SIZE = 4096  # a flat is a 64x64 lump, one byte per texel, row-major


def flat(overrides: dict[int, int] | None = None) -> bytes:
    """A 4096-byte flat; overrides map a row-major texel index to a value."""
    data = bytearray(FLAT_SIZE)
    for i, v in (overrides or {}).items():
        data[i] = v
    return bytes(data)


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
