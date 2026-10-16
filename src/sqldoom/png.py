"""Wrap a raw RGB framebuffer in a PNG file, using only the standard library.

A PNG is a signature followed by chunks (length, type, body, CRC32): IHDR with the
image size and format, IDAT with the zlib-compressed rows (each row prefixed by a
filter byte, 0 = none), and IEND.
"""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def encode_rgb(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError(f"expected {stride * height} bytes for {width}x{height} RGB, got {len(rgb)}")
    rows = b"".join(b"\0" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit depth, truecolor RGB
    return SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
