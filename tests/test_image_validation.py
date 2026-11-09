from __future__ import annotations

import struct
import unittest
import zlib

from app.image_validation import validate_image


def png(width: int = 8, height: int = 8) -> bytes:
    def chunk(kind: bytes, value: bytes) -> bytes:
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressor = zlib.compressobj()
    row = b"\x00" + b"\xff\xff\xff\xff" * width
    compressed = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


def jpeg(width: int = 8, height: int = 8) -> bytes:
    sof_data = bytes((8,)) + struct.pack(">HH", height, width) + bytes((1, 1, 0x11, 0))
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof_data) + 2) + sof_data + b"\xff\xd9"


class ImageValidationTests(unittest.TestCase):
    def test_valid_png_returns_metadata_and_hash(self):
        data = png(48, 24)
        result = validate_image(data, "image/png", "sample.png")
        self.assertEqual((result["width"], result["height"]), (48, 24))
        self.assertEqual(result["kind"], "image")
        self.assertEqual(len(result["sha256"]), 64)

    def test_valid_jpeg_accepts_jpg_extension(self):
        result = validate_image(jpeg(31, 19), "image/jpeg", "scan.JPG")
        self.assertEqual((result["width"], result["height"]), (31, 19))

    def test_rejects_mime_extension_and_magic_mismatch(self):
        with self.assertRaisesRegex(ValueError, "MIME type"):
            validate_image(png(), "image/png", "sample.jpg")
        with self.assertRaisesRegex(ValueError, "MIME type"):
            validate_image(png(), "image/png", "sample.txt")
        with self.assertRaisesRegex(ValueError, "signature"):
            validate_image(b"not-image", "image/png", "sample.png")

    def test_rejects_bad_png_crc_and_truncated_jpeg(self):
        bad_png = bytearray(png())
        bad_png[29] ^= 1
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_image(bytes(bad_png), "image/png", "bad.png")
        with self.assertRaisesRegex(ValueError, "truncated"):
            validate_image(b"\xff\xd8\xff\xd9", "image/jpeg", "bad.jpg")

    def test_rejects_oversized_dimensions(self):
        with self.assertRaisesRegex(ValueError, "pixel count"):
            validate_image(png(5001, 5000), "image/png", "large.png")

    def test_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            validate_image(b"\x00" * 2_000_001, "image/png", "large.png")


if __name__ == "__main__":
    unittest.main()
