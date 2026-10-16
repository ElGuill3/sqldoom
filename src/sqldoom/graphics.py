"""Decode WAD graphics lumps: PLAYPAL, COLORMAP, composite wall textures and flats.

Decoding stays in the Python importer (like the map lumps) so that rendering stays
pure SQL. Pixels are palette indices throughout; each texture is stored as one row
per column so a frame only needs small joins plus `list_extract`.
"""

import struct
from dataclasses import dataclass

from .wad import CorruptWad, Lump, decode_name

PALETTE_BYTES = 768  # 256 entries of (r, g, b); doom1.wad carries several palettes back to back
COLORMAP_LEVELS = 34
COLORMAP_LEVEL_BYTES = 256
FLAT_BYTES = 4096  # a 64x64 flat, one byte per texel, row-major

PICTURE_HEADER = struct.Struct("<4h")  # width, height, leftoffset, topoffset
TEXTURE_ENTRY = struct.Struct("<8sIhhIh")  # name, masked, width, height, obsolete, patch count
PATCH_REF = struct.Struct("<5h")  # originx, originy, PNAMES index, stepdir, colormap

POST_END = 0xFF  # topdelta byte that terminates a picture column


@dataclass(frozen=True)
class Texture:
    name: str
    width: int
    height: int
    columns: list[list[int]]  # columns[u] has exactly `height` palette indices


@dataclass(frozen=True)
class Graphics:
    palette: list[tuple[int, int, int]]  # 256 (r, g, b) rows, empty without PLAYPAL
    colormaps: list[list[int]]  # 34 levels of 256 palette indices, empty without COLORMAP
    textures: list[Texture]  # empty without TEXTURE1/TEXTURE2 + PNAMES
    flats: list[tuple[str, list[int]]]  # empty without F_START/F_END


def decode_palette(data: bytes) -> list[tuple[int, int, int]]:
    """Palette 0 of PLAYPAL; a multi-palette lump (doom1.wad) uses its first 768 bytes.
    A lump too short to hold one palette is ignored, leaving the table empty."""
    if len(data) < PALETTE_BYTES:
        return []
    return [tuple(data[o:o + 3]) for o in range(0, PALETTE_BYTES, 3)]


def decode_colormap(data: bytes) -> list[list[int]]:
    if len(data) < COLORMAP_LEVELS * COLORMAP_LEVEL_BYTES:
        return []
    return [
        list(data[k * COLORMAP_LEVEL_BYTES:(k + 1) * COLORMAP_LEVEL_BYTES])
        for k in range(COLORMAP_LEVELS)
    ]


def decode_pnames(data: bytes) -> list[str]:
    if len(data) < 4:
        raise CorruptWad()
    (count,) = struct.unpack_from("<i", data)
    if count < 0 or 4 + 8 * count > len(data):
        raise CorruptWad()
    return [decode_name(data[4 + 8 * i:12 + 8 * i]) for i in range(count)]


def decode_texture_map(data: bytes) -> list[tuple[str, int, int, list[tuple[int, int, int]]]]:
    """TEXTURE1/TEXTURE2: (name, width, height, [(originx, originy, PNAMES index), ...]) per entry."""
    if len(data) < 4:
        raise CorruptWad()
    (count,) = struct.unpack_from("<i", data)
    if count < 0 or 4 + 4 * count > len(data):
        raise CorruptWad()
    entries = []
    for entry_offset in struct.unpack_from(f"<{count}i", data, 4):
        if entry_offset < 0 or entry_offset + TEXTURE_ENTRY.size > len(data):
            raise CorruptWad()
        raw_name, _, width, height, _, patch_count = TEXTURE_ENTRY.unpack_from(data, entry_offset)
        if width < 0 or height < 0:
            raise CorruptWad()
        patches_offset = entry_offset + TEXTURE_ENTRY.size
        if patches_offset + patch_count * PATCH_REF.size > len(data):
            raise CorruptWad()
        patches = [
            PATCH_REF.unpack_from(data, patches_offset + i * PATCH_REF.size)[:3]
            for i in range(patch_count)
        ]
        entries.append((decode_name(raw_name), width, height, patches))
    return entries


def composite(
    name: str, width: int, height: int, patches: list[tuple[int, int, int]],
    pnames: list[str], lumps_by_name: dict[str, bytes],
) -> Texture:
    """Draw the patches in order (later ones overwrite), clipped to the texture."""
    columns = [[0] * height for _ in range(width)]  # texels no patch covers stay 0
    for originx, originy, index in patches:
        if not 0 <= index < len(pnames):
            raise CorruptWad()
        data = lumps_by_name.get(pnames[index])
        if data is None:
            raise CorruptWad()
        draw_patch(data, columns, width, height, originx, originy)
    return Texture(name, width, height, columns)


def draw_patch(
    data: bytes, columns: list[list[int]], width: int, height: int, originx: int, originy: int,
) -> None:
    if len(data) < PICTURE_HEADER.size:
        raise CorruptWad()
    patch_width, _, _, _ = PICTURE_HEADER.unpack_from(data)
    if patch_width < 0 or PICTURE_HEADER.size + 4 * patch_width > len(data):
        raise CorruptWad()
    for u in range(patch_width):
        (column_offset,) = struct.unpack_from("<i", data, PICTURE_HEADER.size + 4 * u)
        if column_offset < 0 or column_offset >= len(data):
            raise CorruptWad()
        tex_u = originx + u
        if not 0 <= tex_u < width:
            continue
        column = columns[tex_u]
        pos = column_offset
        while True:
            if pos >= len(data):
                raise CorruptWad()
            topdelta = data[pos]
            if topdelta == POST_END:
                break
            if pos + 2 > len(data):
                raise CorruptWad()
            length = data[pos + 1]
            if pos + 4 + length > len(data):  # post: 2 bytes + pad + pixels + pad
                raise CorruptWad()
            for i in range(length):
                v = originy + topdelta + i
                if 0 <= v < height:
                    column[v] = data[pos + 3 + i]
            pos += 4 + length


def flat_lumps(lumps: list[Lump]) -> list[Lump]:
    """Every lump strictly between F_START and F_END; 0-byte inner markers (F1_START...) skipped."""
    names = [lump.name for lump in lumps]
    if "F_START" not in names or "F_END" not in names:
        return []
    start, end = names.index("F_START"), names.index("F_END")
    return [lump for lump in lumps[start + 1:end] if lump.size > 0]


def decode_graphics(lumps: list[Lump]) -> Graphics:
    """Decode the graphics tables. Every lump is optional: a WAD without PLAYPAL, COLORMAP,
    TEXTURE1/TEXTURE2 or PNAMES still imports and leaves the matching tables empty (G3).
    A PLAYPAL/COLORMAP too short to hold one palette/level is treated the same way."""
    by_name: dict[str, bytes] = {}
    for lump in lumps:
        by_name.setdefault(lump.name, lump.data)

    palette = decode_palette(by_name["PLAYPAL"]) if "PLAYPAL" in by_name else []
    colormaps = decode_colormap(by_name["COLORMAP"]) if "COLORMAP" in by_name else []

    textures: list[Texture] = []
    if "TEXTURE1" in by_name or "TEXTURE2" in by_name:
        if "PNAMES" not in by_name:
            raise CorruptWad()
        pnames = decode_pnames(by_name["PNAMES"])
        for texture_lump in ("TEXTURE1", "TEXTURE2"):
            if texture_lump not in by_name:
                continue
            for name, width, height, patches in decode_texture_map(by_name[texture_lump]):
                textures.append(composite(name, width, height, patches, pnames, by_name))

    flats = []
    for lump in flat_lumps(lumps):
        if len(lump.data) != FLAT_BYTES:
            raise CorruptWad()
        flats.append((lump.name, list(lump.data)))

    return Graphics(palette, colormaps, textures, flats)
