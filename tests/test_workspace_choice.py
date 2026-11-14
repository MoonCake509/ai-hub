"""Этап 3: рабочая папка проекта выбирается человеком — диск и папка.

Решение владельца: корень R:, внутри — папки проектов. Сервер не даёт выйти
за разрешённые корни, не принимает относительные пути и «..».

Запуск: python tests/test_workspace_choice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    from hub.app import workspace

    roots = workspace.workspace_roots()
    check("корни из конфига", bool(roots), ", ".join(roots))
    check("R: разрешён", any(item.upper().startswith("R:") for item in roots))
    check("корни — пути, а не строка списка", all(item == item.strip() and not item.startswith("[") for item in roots), ", ".join(roots))

    good = r"R:\Projects\coffee-finance"
    check("путь внутри R: принимается", workspace.validate_workdir(good) == good)

    bad_cases = {
        "относительный": "Projects\\x",
        "выход через ..": r"R:\Projects\..\Windows",
        "чужой диск": r"C:\Users\user\dev\x",
    }
    for label, value in bad_cases.items():
        try:
            workspace.validate_workdir(value)
            check(f"{label} отвергнут", False)
        except workspace.OpError:
            check(f"{label} отвергнут", True)

    main_py = (ROOT / "hub" / "app" / "main.py").read_text(encoding="utf-8")
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    conf = (ROOT / "config" / "hub.yaml").read_text(encoding="utf-8")

    check("эндпоинт корней", '/workspace/roots' in main_py)
    check("workdir из запроса проходит проверку", "workspace.validate_workdir(requested)" in main_py)
    check("в форме есть выбор корня", 'id="project-root"' in html)
    check("в форме есть папка проекта", 'id="project-folder"' in html)
    check("путь собирается из корня и папки", "wsComposeWorkdir" in app_js)
    check("корни перечислены в конфиге", "allowed_roots:" in conf)

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())