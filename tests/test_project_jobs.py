"""Этап 6: архивариус как задача проекта по расписанию, а не строка в списке ролей.

Решение владельца: запуск только по расписанию, по умолчанию 05:00 МСК;
удаление запрещено — только предложения в отчёте. Проверяем расчёт слота,
защиту от двойного запуска и наличие ручного «Запустить сейчас».

Запуск: python tests/test_project_jobs.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    from hub.app import db

    src = (ROOT / "hub" / "app" / "db.py").read_text(encoding="utf-8")
    main_py = (ROOT / "hub" / "app" / "main.py").read_text(encoding="utf-8")
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    check("таблица задач проекта", "create table if not exists aihub.project_jobs" in src)
    check("история запусков", "create table if not exists aihub.project_job_runs" in src)
    check("слот уникален", "unique (job_id, slot)" in src)
    check("внешний ключ на проект", "references aihub.projects(id) on delete cascade" in src)

    # 14.11.2026 00:00 UTC = 03:00 МСК → ближайший запуск 05:00 МСК = 02:00 UTC.
    now = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp()
    got = db._next_daily_run("0 5 * * *", now)
    check("05:00 МСК в тот же день", got == now + 7200, f"{got - now}")

    # После запуска в 05:00 МСК следующий — через сутки.
    after = now + 7200
    check("следующий слот — через сутки",
          db._next_daily_run("0 5 * * *", after) == after + 86400,
          f"{db._next_daily_run('0 5 * * *', after) - after}")

    for bad in ("5 * * * *", "0 25 * * *", "*/5 * * * *", ""):
        try:
            db._parse_daily(bad)
            check(f"расписание {bad!r} отвергнуто", False)
        except ValueError:
            check(f"расписание {bad!r} отвергнуто", True)

    check("по умолчанию 05:00 МСК", '"0 5 * * *"' in main_py)
    check("пробный запуск без ожидания", "/jobs/{job_id}/run" in main_py)
    check("тик планировщика подключён", "await _schedule_project_jobs()" in main_py)
    check("промпт запрещает удаление", "ничего не удалять" in main_py)
    check("отчёты по дням в ops/archivist", "ops/archivist/<YYYY-MM-DD>/" in main_py)
    check("основной канал — дешёвый router.cheap 1017",
          'ARCHIVIST_MODEL = "deepseek-v4.1-flash-1017"' in main_py)
    check("резерв — Qwen", 'ARCHIVIST_FALLBACK_AGENT = "qwen-server"' in main_py
          and 'ARCHIVIST_FALLBACK_MODEL = "qwen3.8-flash"' in main_py)
    check("резерв включается один раз", "and not j.fallback_used limit 20" in src)
    check("резерв ставится в очередь тиком", "await _poll_project_fallbacks()" in main_py)
    check("простой 10 минут считается сбоем", "STUCK_RUN_S = 600" in src)
    check("интерфейс: список задач", "wsLoadJobs" in app_js)
    check("интерфейс: маршрут основной → резерв", "fallback_used" in app_js)
    check("интерфейс: кнопка добавления", "#job-add" in app_js)

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())