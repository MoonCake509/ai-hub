"""Bounded PNG/JPEG validation for workspace image attachments."""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_DIMENSION = 10_000
MAX_IMAGE_PIXELS = 25_000_000

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError(f"image dimensions exceed {MAX_IMAGE_DIMENSION}px")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"image pixel count exceeds {MAX_IMAGE_PIXELS}")


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 45 or not data.startswith(PNG_SIGNATURE):
        raise ValueError("invalid PNG signature or truncated image")
    offset = len(PNG_SIGNATURE)
    width = height = 0
    seen_ihdr = seen_idat = seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise ValueError("invalid PNG chunk checksum")
        if not seen_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("PNG must begin with one valid IHDR chunk")
            width, height = struct.unpack_from(">II", chunk_data, 0)
            seen_ihdr = True
        elif chunk_type == b"IHDR":
            raise ValueError("PNG contains duplicate IHDR")
        if chunk_type == b"IDAT":
            seen_idat = True
        if chunk_type == b"IEND":
            if length != 0:
                raise ValueError("invalid PNG IEND chunk")
            seen_iend = True
            offset = end
            break
        offset = end
    if not seen_ihdr or not seen_idat or not seen_iend or offset != len(data):
        raise ValueError("PNG is incomplete")
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 16 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("invalid JPEG signature or truncated image")
    offset = 2
    width = height = 0
    while offset < len(data) - 2:
        if data[offset] != 0xFF:
            raise ValueError("invalid JPEG marker sequence")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            if marker == 0xD9:
                break
            continue
        if offset + 2 > len(data):
            break
        segment_length = struct.unpack_from(">H", data, offset)[0]
        if segment_length < 2 or offset + segment_length > len(data):
            raise ValueError("invalid JPEG segment length")
        if marker in JPEG_SOF_MARKERS:
            if segment_length < 7:
                raise ValueError("invalid JPEG frame header")
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return width, height
        if marker == 0xDA:
            break
        offset += segment_length
    raise ValueError("JPEG has no supported frame header")


def validate_image(data: bytes, mime: str, filename: str) -> dict:
    """Validate actual bytes, declared MIME, extension, dimensions and size."""
    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large: {len(data)} > {MAX_IMAGE_BYTES} bytes")
    mime = (mime or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename).suffix.lower()
    allowed_ext = {"image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}}
    if mime not in allowed_ext or suffix not in allowed_ext[mime]:
        raise ValueError("image MIME type and filename extension must match PNG or JPEG")
    if mime == "image/png":
        width, height = _png_dimensions(data)
    else:
        width, height = _jpeg_dimensions(data)
    _check_dimensions(width, height)
    return {
        "kind": "image",
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
