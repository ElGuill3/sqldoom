"""Parse the Doom WAD container format.

A WAD is a flat archive of named blobs ("lumps"):

    header     12 bytes   magic "IWAD"/"PWAD", int32 lump count, int32 directory offset
    lump data  ...        raw bytes, referenced by the directory
    directory  16 bytes   per lump: int32 file offset, int32 size, char[8] name

All integers are little-endian.
"""

import struct
from dataclasses import dataclass

HEADER = struct.Struct("<4sii")
DIRECTORY_ENTRY = struct.Struct("<ii8s")


class WadError(Exception):
    """Raised for input that is not a usable WAD file."""


class NotAWad(WadError):
    pass


class CorruptWad(WadError):
    pass


@dataclass(frozen=True)
class Lump:
    idx: int
    name: str
    filepos: int
    size: int
    data: bytes


def decode_name(raw: bytes) -> str:
    """Lump and texture names are 8 ASCII bytes, NUL-padded."""
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace").upper()


def read_lumps(blob: bytes) -> list[Lump]:
    if len(blob) < HEADER.size:
        raise NotAWad()
    magic, count, dir_offset = HEADER.unpack_from(blob)
    if magic not in (b"IWAD", b"PWAD"):
        raise NotAWad()
    if count < 0 or dir_offset < HEADER.size or dir_offset + count * DIRECTORY_ENTRY.size > len(blob):
        raise CorruptWad()

    lumps = []
    for idx, (filepos, size, raw_name) in enumerate(
        DIRECTORY_ENTRY.iter_unpack(blob[dir_offset : dir_offset + count * DIRECTORY_ENTRY.size])
    ):
        if filepos < 0 or size < 0 or filepos + size > len(blob):
            raise CorruptWad()
        lumps.append(Lump(idx, decode_name(raw_name), filepos, size, blob[filepos : filepos + size]))
    return lumps


def find_maps(lumps: list[Lump]) -> list[int]:
    """A map is an empty marker lump (E1M1, MAP01, ...) directly followed by THINGS."""
    return [i for i in range(len(lumps) - 1) if lumps[i + 1].name == "THINGS"]
