"""Этап 1: цветовая иерархия панели AI Hub.

Проверяет не «красиво ли», а измеримые правила из docs/decisions:
границы интерактивных элементов различимы (>= 3:1), мелкий текст читаем
(>= 4.5:1), primary отличим от обычной кнопки, и в файле не осталось
литеральных hex-поверхностей, из-за которых панели сливались.

Запуск: python tests/test_ui_contrast.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "web" / "styles.css"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    l1, l2 = sorted((_lum(a), _lum(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def tokens(css: str) -> dict[str, str]:
    head = css.split("{", 1)[1].split("}", 1)[0]
    out: dict[str, str] = {}
    for name, value in re.findall(r"(--[a-z-]+):(#[0-9a-fA-F]{6})", head):
        out[name] = value
    return out


def main() -> int:
    css = CSS.read_text(encoding="utf-8")
    t = tokens(css)

    required = ["--bg", "--panel", "--surface", "--surface-hi", "--line",
                "--line-strong", "--text", "--muted", "--faint", "--accent", "--accent-ink"]
    check("токены объявлены", all(k in t for k in required),
          "нет: " + ", ".join(k for k in required if k not in t))

    if all(k in t for k in required):
        check("граница кнопки к её фону >= 3:1", contrast(t["--line-strong"], t["--surface"]) >= 3.0,
              f'{contrast(t["--line-strong"], t["--surface"]):.2f}:1')
        check("граница кнопки к панели >= 3:1", contrast(t["--line-strong"], t["--panel"]) >= 3.0,
              f'{contrast(t["--line-strong"], t["--panel"]):.2f}:1')
        check("мелкий текст (--faint) >= 4.5:1", contrast(t["--faint"], t["--panel"]) >= 4.5,
              f'{contrast(t["--faint"], t["--panel"]):.2f}:1')
        check("основной текст >= 7:1", contrast(t["--text"], t["--panel"]) >= 7.0,
              f'{contrast(t["--text"], t["--panel"]):.2f}:1')
        check("primary отличим от обычной кнопки (>= 3:1)", contrast(t["--accent"], t["--surface"]) >= 3.0,
              f'{contrast(t["--accent"], t["--surface"]):.2f}:1')
        check("подпись на primary читаема (>= 4.5:1)", contrast(t["--accent-ink"], t["--accent"]) >= 4.5,
              f'{contrast(t["--accent-ink"], t["--accent"]):.2f}:1')
        check("surface и panel различимы (>= 1.10:1)", contrast(t["--surface"], t["--panel"]) >= 1.10,
              f'{contrast(t["--surface"], t["--panel"]):.2f}:1')

    # Кнопка и поле обязаны иметь видимую границу, а не сливаться с панелью.
    check("кнопка использует --line-strong",
          re.search(r"button\{[^}]*border:1px solid var\(--line-strong\)", css) is not None)
    check("поля ввода используют --line-strong",
          re.search(r"input,select,textarea\{[^}]*border:1px solid var\(--line-strong\)", css) is not None)
    check("ghost не прозрачный без границы",
          re.search(r"\.ghost\{[^}]*border-color:var\(--line-strong\)", css) is not None)

    # Литеральные hex-поверхности прошлого дизайна не должны вернуться.
    legacy = ["#10141c", "#171d28", "#1d2532", "#111925", "#202c39", "#23313f", "#141b25", "#111720"]
    found = [h for h in legacy if h in css]
    check("старые литеральные поверхности удалены", not found, ", ".join(found))

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
