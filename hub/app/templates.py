"""FLOW-02: шаблон пяти ролей и правила перехода без LLM на каждом шаге.

Каждая роль получает свой вход (свой пакет и свои инструкции), а не общую
переписку. Переход между шагами решается правилами по типизированному выходу:
подошёл по схеме — идём дальше, не подошёл — шаг падает с понятной причиной.
"""
from __future__ import annotations

import json
import re

FIVE_ROLES_ID = "five-roles"
FIVE_ROLES_VERSION = 1

# Роли и их выходы. prompt — инструкция роли, schema — обязательные поля выхода.
ROLES = {
    "planner": {
        "name": "Планировщик",
        "goal": "разложить задачу на проверяемые шаги",
        "output": "plan",
        "required": ["tasks"],
        "prompt": (
            "Ты планировщик. Разложи задачу на шаги. Ответь JSON: "
            '{"tasks":[{"key":"...","title":"...","files":["..."],"acceptance":"..."}],'
            '"risks":["..."]}. Без пояснений вне JSON.'
        ),
    },
    "coder": {
        "name": "Кодер",
        "goal": "внести изменения по плану",
        "output": "change",
        "required": ["summary", "files"],
        "prompt": (
            "Ты кодер. Выполни план. Ответь JSON: "
            '{"summary":"что сделано","files":["изменённые пути"],"notes":"..."}. '
            "Меняй только то, что нужно для плана."
        ),
    },
    "tester": {
        "name": "Тестировщик",
        "goal": "доказать или опровергнуть работу проверками",
        "output": "evidence",
        "required": ["checks", "ok"],
        "prompt": (
            "Ты тестировщик. Запусти проверки и приведи доказательства. Ответь JSON: "
            '{"ok":true|false,"checks":[{"name":"...","cmd":"...","ok":true|false,'
            '"output":"..."}]}. Не пересказывай код — покажи результат проверок.'
        ),
    },
    "reviewer": {
        "name": "Ревьюер",
        "goal": "найти дефекты и оценить риск",
        "output": "findings",
        "required": ["verdict", "findings"],
        "prompt": (
            "Ты ревьюер. Проверь изменение. Ответь JSON: "
            '{"verdict":"approve|changes_requested","findings":[{"severity":"...",'
            '"issue":"...","where":"..."}]}.'
        ),
    },
    "archivist": {
        "name": "Архивариус",
        "goal": "зафиксировать итог и артефакты",
        "output": "record",
        "required": ["summary", "artifacts"],
        "prompt": (
            "Ты архивариус. Зафиксируй итог. Ответь JSON: "
            '{"summary":"...","artifacts":[{"path":"...","kind":"..."}],'
            '"decisions":["..."]}. Исходники не меняй.'
        ),
    },
}

CHAIN = [
    {"key": "plan", "role": "planner", "depends_on": []},
    {"key": "code", "role": "coder", "depends_on": ["plan"]},
    {"key": "test", "role": "tester", "depends_on": ["code"]},
    {"key": "review", "role": "reviewer", "depends_on": ["test"]},
    {"key": "archive", "role": "archivist", "depends_on": ["review"]},
]

# Каждой роли — свой вход: у планировщика контракты и точка входа, у тестировщика
# тесты, у ревьюера изменение, у архивариуса всё итоговое.
CONTEXT = {
    "planner": {"paths": ["docs/*", "*.md"], "budget_tokens": 3000},
    "coder": {"paths": ["src/*"], "budget_tokens": 6000},
    "tester": {"paths": ["tests/*"], "budget_tokens": 4000},
    "reviewer": {"paths": ["src/*"], "budget_tokens": 4000},
    "archivist": {"paths": ["docs/*"], "budget_tokens": 2000},
}


def template(project_entry: str = "", contracts: list[str] | None = None) -> dict:
    """Шаблон запуска: цепочка ролей, схемы выходов и вход каждой роли."""
    steps = []
    for index, item in enumerate(CHAIN):
        role = ROLES[item["role"]]
        spec = dict(CONTEXT[item["role"]])
        steps.append({
            "key": item["key"],
            "role": item["role"],
            "agent": "codex",
            "prompt": role["prompt"],
            "depends_on": list(item["depends_on"]),
            "output": role["output"],
            "context": {
                "goal": role["goal"],
                "entry": project_entry if index == 0 and project_entry else "",
                "contracts": list(contracts or []) if index == 0 else [],
                "paths": spec["paths"],
                "budget_tokens": spec["budget_tokens"],
            },
        })
    return {"id": FIVE_ROLES_ID, "version": FIVE_ROLES_VERSION, "steps": steps}


def describe() -> dict:
    return {
        "id": FIVE_ROLES_ID,
        "version": FIVE_ROLES_VERSION,
        "roles": [{"key": key, **{k: v for k, v in spec.items() if k != "prompt"}}
                  for key, spec in ROLES.items()],
        "steps": [{"key": s["key"], "role": s["role"], "depends_on": s["depends_on"],
                   "context": s["context"]} for s in template()["steps"]],
    }


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(body[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def validate_output(role_key: str, text: str) -> tuple[bool, dict | None, str | None]:
    """Правило перехода: выход роли обязан быть JSON с обязательными полями."""
    spec = ROLES.get(role_key)
    if not spec:
        return False, None, f"неизвестная роль: {role_key}"
    parsed = _extract_json(text or "")
    if parsed is None:
        return False, None, "выход роли не является JSON-объектом"
    missing = [field for field in spec["required"] if field not in parsed]
    if missing:
        return False, parsed, f"в выходе нет обязательных полей: {', '.join(missing)}"
    if role_key == "tester" and parsed.get("ok") is not True:
        return False, parsed, "проверки не подтвердили работу"
    if role_key == "reviewer" and parsed.get("verdict") != "approve":
        return False, parsed, f"ревью просит изменения: {parsed.get('verdict')}"
    return True, parsed, None


def next_step_for_failure(role_key: str) -> str | None:
    """Куда возвращаться при провале проверки/ревью — правило, без LLM."""
    if role_key in ("tester", "reviewer"):
        return "code"
    return None