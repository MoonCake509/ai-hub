from __future__ import annotations

import struct
import zlib
from pathlib import Path


FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    " ": ("000", "000", "000", "000", "000", "000", "000"),
}


def make_png(text: str = "LUNA OCR 73", scale: int = 12) -> bytes:
    rows = [""] * 7
    for index, char in enumerate(text.upper()):
        glyph = FONT[char]
        for row in range(7):
            rows[row] += glyph[row] + ("0" if index < len(text) - 1 else "")
    width = len(rows[0]) * scale + 2 * scale
    height = 7 * scale + 2 * scale
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        row_index = (y - scale) // scale
        for x in range(width):
            ix = (x - scale) // scale
            on = (scale <= y < scale * 8 and scale <= x < width - scale
                  and 0 <= row_index < 7 and 0 <= ix < len(rows[row_index])
                  and rows[row_index][ix] == "1")
            value = 0 if on else 255
            raw.extend((value, value, value))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    output = Path(__file__).with_name("synthetic_ocr.png")
    output.write_bytes(make_png())
    print(f"{output} ({output.stat().st_size} bytes)")
