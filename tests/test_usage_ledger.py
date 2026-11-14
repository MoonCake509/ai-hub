"""Этап 4: расход в токенах вместо нерабочего блока «Расходы и лимиты».

Решение владельца: единица учёта — токены (вход/выход/размышление), разрез
провайдер → модель → задача; денег и блокировок нет. Проверяем: журнал
append-only и идемпотентен по попытке, агрегат отдаёт токены, а не «баланс»,
и интерфейс больше не показывает захардкоженную строку.

Запуск: python tests/test_usage_ledger.py
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
    from hub.app import db

    src = (ROOT / "hub" / "app" / "db.py").read_text(encoding="utf-8")
    check("таблица usage_ledger в DDL", "create table if not exists aihub.usage_ledger" in src)
    check("ключ идемпотентности — attempt_id", "attempt_id text primary key" in src.split("usage_ledger", 1)[1][:400])
    check("повтор не удваивает запись", "on conflict (attempt_id) do nothing" in src)
    check("запись внутри закрытия попытки", "_record_usage(conn, job, attempt_id, usage)" in src)
    check("агрегат по проекту есть", "def usage_summary" in src)

    # Разбор отчёта канала: разные имена полей у разных провайдеров.
    cases = [
        ({"tokens_in": 10, "tokens_out": 5, "tokens_reasoning": 2}, (10, 5, 2, "reported")),
        ({"input_tokens": 7, "output_tokens": 3}, (7, 3, 0, "reported")),
        ({"prompt_tokens": 4, "completion_tokens": 6, "reasoning_tokens": 1}, (4, 6, 1, "reported")),
        ({}, (0, 0, 0, "absent")),
        (None, (0, 0, 0, "absent")),
        ({"model": "qwen4-max-preview"}, (0, 0, 0, "absent")),
    ]
    for usage, expected in cases:
        got = db._usage_numbers(usage)
        check(f"разбор usage {usage}", got == expected, f"{got} != {expected}")

    main_py = (ROOT / "hub" / "app" / "main.py").read_text(encoding="utf-8")
    check("эндпоинт расхода по проекту", '/projects/{project_id}/usage' in main_py)

    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("интерфейс запрашивает токены", "wsLoadRoleQuota" in app_js)
    check("нет старой заглушки про баланс",
          "Провайдер не сообщает данные баланса или лимитов." not in app_js)
    check("честная пометка «нет данных»", "канал не сообщает usage" in app_js)

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())