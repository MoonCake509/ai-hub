"""Этап 3: рабочая папка проекта.

Решение владельца (14.11): выбор диска, папки и устройства убран. Все проекты
создаются на сервере в едином пространстве R:\\AIHub\\WorkSpace, папка проекта
называется по проекту. Сервер по-прежнему не даёт выйти за разрешённые корни.

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
    check("выбор устройства убран из формы", 'id="project-host"' not in html)
    check("выбор корня убран из формы", 'id="project-root"' not in html and 'id="project-folder"' not in html)
    check("рабочая папка только показывается", 'id="project-path"' in html and "wsProjectPath" in app_js)
    check("проект создаётся на сервере, путь не принимается из формы",
          "DEFAULT_PROJECT_HOST" in main_py and "workspace.validate_workdir(requested)" not in main_py)
    check("единое пространство WorkSpace в конфиге", "WorkSpace" in conf)
    check("анимация создания рабочей зоны", "ws-progress" in html and "ws-slide" in (ROOT / "web" / "styles.css").read_text(encoding="utf-8"))

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())