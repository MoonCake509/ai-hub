"""Quick channel check: a short, budgeted probe set with partial results.

The quick mode is deliberately separate from the extended suite: it asks for one
to three short probes, never the whole catalogue, and it reports what actually
happened (graded, timed out, skipped by budget) instead of a single score.
"""
from __future__ import annotations

import hashlib
import re
import struct
import zlib
from typing import Any

QUICK_SUITE_ID = "aihub-channel-quick"
QUICK_SUITE_VERSION = "1.0.0"
QUICK_GRADER_VERSION = "deterministic-v1"
QUICK_MAX_CALLS_TEXT = 3
QUICK_MAX_CALLS_IMAGE = 1
DEFAULT_BUDGET_S = 45
MIN_BUDGET_S = 10
MAX_BUDGET_S = 120

# The OCR fixture is generated here, not stored: the bytes are reproducible, so
# an image check stays honest without shipping a test asset inside the image.
OCR_FIXTURE_TEXT = "LUNA 73"
OCR_PROMPT = "Read the text printed in the image. Reply with that text only."

_FONT: dict[str, tuple[str, ...]] = {
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    " ": ("00000",) * 7,
}


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def ocr_fixture_png(text: str = OCR_FIXTURE_TEXT, scale: int = 8, margin: int = 16) -> bytes:
    """Render a deterministic black-on-white PNG with a 5x7 pixel font."""
    glyphs = [text[i] if text[i] in _FONT else " " for i in range(len(text))]
    cell = 6  # five columns plus one column of spacing
    width = margin * 2 + cell * scale * len(glyphs) - scale
    height = margin * 2 + 7 * scale
    pixels = bytearray([255]) * (width * height)
    for index, glyph in enumerate(glyphs):
        rows = _FONT[glyph]
        origin_x = margin + index * cell * scale
        for row_index, row in enumerate(rows):
            for column_index, bit in enumerate(row):
                if bit != "1":
                    continue
                x0 = origin_x + column_index * scale
                y0 = margin + row_index * scale
                for y in range(y0, y0 + scale):
                    start = y * width + x0
                    pixels[start:start + scale] = b"\x00" * scale
    raw = b"".join(b"\x00" + bytes(pixels[y * width:(y + 1) * width]) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b""))


def ocr_fixture_sha(text: str = OCR_FIXTURE_TEXT) -> str:
    return hashlib.sha256(ocr_fixture_png(text)).hexdigest()[:16]


def _normalize_ocr(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def max_calls(requires_image: bool) -> int:
    return QUICK_MAX_CALLS_IMAGE if requires_image else QUICK_MAX_CALLS_TEXT


def build_cases(nonce: str, requires_image: bool) -> list[dict[str, Any]]:
    """Return the short probe set for a channel modality.

    A vision channel gets one OCR probe instead of text probes: it cannot answer
    a text-only case without an image, and pretending otherwise would inflate
    the denominator.
    """
    clean = re.sub(r"[^A-Z0-9]", "", (nonce or "").upper())[:8] or "0000"
    if requires_image:
        return [{
            "id": "quick-ocr",
            "title": "Текст на изображении",
            "category": "vision",
            "description": "Синтетическое изображение с известным текстом; проверяется распознавание символов.",
            "prompt": OCR_PROMPT,
            "fixture": {"kind": "image/png", "text": OCR_FIXTURE_TEXT, "sha256_16": ocr_fixture_sha()},
            "check": {"type": "ocr-normalized", "value": OCR_FIXTURE_TEXT},
        }]
    return [
        {
            "id": "quick-nonce",
            "title": "Точное следование инструкции",
            "category": "instruction",
            "description": "Короткий ответ с уникальной меткой запуска.",
            "prompt": f"Reply with exactly this token and nothing else: QK-{clean}",
            "check": {"type": "exact", "value": f"QK-{clean}"},
        },
        {
            "id": "quick-shape",
            "title": "Структура JSON",
            "category": "format",
            "description": "Минимальный структурированный ответ без пояснений.",
            "prompt": "Return only a JSON object with exactly these keys: ok, n. Set ok to true and n to 7.",
            "check": {"type": "json-object", "keys": ["ok", "n"], "equals": {"ok": True, "n": 7}},
        },
        {
            "id": "quick-context",
            "title": "Ответ по короткому контексту",
            "category": "retrieval",
            "description": "Извлечение метки из одной синтетической строки.",
            "prompt": (f"Note: the depot code is KITE-{clean}. "
                       "What is the depot code? Reply with the code only."),
            "check": {"type": "exact", "value": f"KITE-{clean}"},
        },
    ]


def build_suite(nonce: str, requires_image: bool) -> dict[str, Any]:
    cases = build_cases(nonce, requires_image)
    return {
        "id": QUICK_SUITE_ID,
        "version": QUICK_SUITE_VERSION,
        "grader_version": QUICK_GRADER_VERSION,
        "mode": "quick",
        "title": "Быстрая проверка канала",
        "description": ("До трёх коротких проб в пределах общего бюджета времени. "
                        "Результат — наблюдение о маршруте, не оценка модели."),
        "max_calls": len(cases),
        "cases": cases,
    }


def normalize_budget(value: Any) -> int:
    if value is None:
        return DEFAULT_BUDGET_S
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("budget_s must be an integer")
    if not MIN_BUDGET_S <= value <= MAX_BUDGET_S:
        raise ValueError(f"budget_s must be between {MIN_BUDGET_S} and {MAX_BUDGET_S}")
    return value


SKIPPED_STATES = {"not_applicable", "budget_exhausted"}
PENDING_STATES = {"pending", "queued", "starting", "running"}


def graded_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items
            if item.get("status") not in SKIPPED_STATES and item.get("passed") is not None]


def verdict(run: dict[str, Any]) -> dict[str, Any]:
    """Summarise a quick run without inventing a score for unfinished probes."""
    items = run.get("items") or []
    done = graded_items(items)
    skipped = [item for item in items if item.get("status") in SKIPPED_STATES]
    pending = [item for item in items if item.get("status") in PENDING_STATES]
    failed = [item for item in done if item.get("passed") is False]
    passed = [item for item in done if item.get("passed") is True]
    if run.get("status") in ("starting", "queued", "running"):
        state = "running"
    elif not done:
        state = "insufficient"
    elif failed:
        state = "deviations"
    else:
        state = "ok"
    reasons: list[str] = []
    for item in failed:
        case = item.get("case") or {}
        reason = item.get("grader_reason") or item.get("error") or "проверка не пройдена"
        reasons.append(f"{case.get('title') or item.get('case_id')}: {reason}")
    for item in skipped:
        case = item.get("case") or {}
        reasons.append(f"{case.get('title') or item.get('case_id')}: "
                       + (item.get("grader_reason") or "проверка не выполнялась"))
    for item in pending:
        case = item.get("case") or {}
        reasons.append(f"{case.get('title') or item.get('case_id')}: ещё не завершена")
    if state == "insufficient" and not reasons:
        reasons.append("Нет завершённых проверок: данных для вывода недостаточно.")
    labels = {"ok": "без замечаний", "deviations": "есть отклонения",
              "insufficient": "данных недостаточно", "running": "проверка идёт"}
    return {
        "state": state,
        "label": labels[state],
        "reasons": reasons,
        "graded_count": len(done),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "pending_count": len(pending),
        "complete": state != "running",
    }


def budget_exhausted(run: dict[str, Any], elapsed_seconds: float | None) -> bool:
    budget = run.get("budget_s")
    if not isinstance(budget, int) or elapsed_seconds is None:
        return False
    if run.get("status") in ("completed", "failed"):
        return False
    return elapsed_seconds > float(budget)


def skip_reason(run: dict[str, Any]) -> str:
    budget = run.get("budget_s")
    return (f"быстрая проверка не завершилась за {budget} с: "
            "общий бюджет исчерпан, результат частичный")
