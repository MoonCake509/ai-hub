"""FLOW-02: шаблон пяти ролей и строгие типизированные схемы выходов.

Каждая роль получает свой вход (свой пакет и свои инструкции), а не общую
переписку. Переход между шагами решается правилами по типизированному выходу:
выход обязан быть JSON, поля — строго нужных типов (список задач — это список
объектов с строковыми полями, а не любое значение под ключом). Наличия ключа
недостаточно: {"tasks":"not-a-list"} отклоняется схемой.

«Тест пройден» со слов модели не принимается: схема тестировщика требует
ссылки на кандидата, а факт прохождения подтверждает программная ворота
flow_gates по receipt изолированного runner'а.
"""
from __future__ import annotations

import json
import re

FIVE_ROLES_ID = "five-roles"
FIVE_ROLES_VERSION = 1

# Поля, которые проверяются по типу, только если присутствуют в выходе.
OPTIONAL_FIELDS = {"notes", "risks"}


def _str(value) -> bool:
    return isinstance(value, str)


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _opt_str(value) -> bool:
    return value is None or isinstance(value, str)


# Строгие схемы: validate(name, value) -> причина отказа или None.
def _schema_plan(value) -> str | None:
    if not isinstance(value, list) or not value:
        return "tasks: нужен непустой список задач"
    for i, task in enumerate(value):
        if not isinstance(task, dict):
            return f"tasks[{i}]: задача должна быть объектом"
        for field in ("key", "title"):
            if not _str(task.get(field)) or not task[field]:
                return f"tasks[{i}].{field}: ожидалась непустая строка"
        if not _str_list(task.get("files")):
            return f"tasks[{i}].files: ожидался список строк"
        if not _opt_str(task.get("acceptance")):
            return f"tasks[{i}].acceptance: ожидалась строка"
    return None


def _schema_findings(value) -> str | None:
    if not isinstance(value, list):
        return "findings: ожидался список"
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return f"findings[{i}]: замечание должно быть объектом"
        if not _str(item.get("severity")):
            return f"findings[{i}].severity: ожидалась строка"
        if not _str(item.get("issue")):
            return f"findings[{i}].issue: ожидалась строка"
        if not _opt_str(item.get("where")):
            return f"findings[{i}].where: ожидалась строка"
    return None


def _schema_artifacts(value) -> str | None:
    if not isinstance(value, list):
        return "artifacts: ожидался список"
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return f"artifacts[{i}]: артефакт должен быть объектом"
        if not _str(item.get("path")) or not item["path"]:
            return f"artifacts[{i}].path: ожидалась непустая строка"
        if not _str(item.get("kind")):
            return f"artifacts[{i}].kind: ожидалась строка"
    return None


def _schema_checks(value) -> str | None:
    if not isinstance(value, list) or not value:
        return "checks: нужен непустой список проверок"
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return f"checks[{i}]: проверка должна быть объектом"
        if not _str(item.get("name")) or not item["name"]:
            return f"checks[{i}].name: ожидалась непустая строка"
        cmd = item.get("cmd")
        if not (_str(cmd) or (isinstance(cmd, list) and all(isinstance(x, str) for x in cmd))):
            return f"checks[{i}].cmd: ожидалась строка или список строк"
        if not isinstance(item.get("ok"), bool):
            return f"checks[{i}].ok: ожидался boolean"
    return None


def _candidate_digest(value) -> str | None:
    return None if _str(value) and value else "candidate_digest: ожидалась непустая строка"


# Роли и их выходы. prompt — инструкция роли, schema — типизированный контракт
# выхода; candidate=True — выход обязан ссылаться на digest кандидата (ворота).
ROLES = {
    "planner": {
        "name": "Планировщик",
        "goal": "разложить задачу на проверяемые шаги",
        "output": "plan",
        "schema": {"tasks": _schema_plan,
                   "risks": lambda v: None if _str_list(v) else "risks: ожидался список строк"},
        "prompt": (
            "Ты планировщик. Разложи задачу на шаги. Ответь JSON: "
            '{"tasks":[{"key":"...","title":"...","files":["..."],"acceptance":"..."}],'
            '"risks":["..."]}. Без пояснений вне JSON. '
            "Не включай в план слияние в основную ветку, push или переключение "
            "основной рабочей копии: слияние проверенного коммита выполняет хаб "
            "после тестов и ревью."
        ),
    },
    "coder": {
        "name": "Кодер",
        "goal": "внести изменения по плану",
        "output": "change",
        "schema": {"summary": lambda v: None if _str(v) else "summary: ожидалась строка",
                   "files": lambda v: None if _str_list(v) else "files: ожидался список строк",
                   "notes": lambda v: None if _opt_str(v) else "notes: ожидалась строка"},
        "prompt": (
            "Ты кодер. Выполни план. Ответь JSON: "
            '{"summary":"что сделано","files":["изменённые пути"],"notes":"..."}. '
            "Меняй только то, что нужно для плана. Работай только в своей рабочей "
            "копии на ветке задания: не сливай в основную ветку, не делай push и не "
            "переключай основную копию. Оставь изменения одним коммитом в ветке задания."
        ),
    },
    "tester": {
        "name": "Тестировщик",
        "goal": "проанализировать доказательства проверок кандидата",
        "output": "evidence",
        "candidate": True,
        "schema": {"checks": _schema_checks,
                   "ok": lambda v: None if isinstance(v, bool) else "ok: ожидался boolean",
                   "candidate_digest": _candidate_digest},
        "prompt": (
            "Ты тестировщик. Проанализируй приложенный receipt изолированного runner'а. "
            "Ответь JSON: {\"ok\":true|false,\"candidate_digest\":\"sha256:...\","
            '"checks":[{"name":"...","cmd":"...","ok":true|false}]}. '
            "Прохождение тестов объявляет только receipt, не твой JSON."
        ),
    },
    "reviewer": {
        "name": "Ревьюер",
        "goal": "найти дефекты и оценить риск",
        "output": "findings",
        "candidate": True,
        "schema": {"verdict": lambda v: None if v in ("approve", "changes_requested")
                   else "verdict: approve|changes_requested",
                   "findings": _schema_findings,
                   "candidate_digest": _candidate_digest},
        "prompt": (
            "Ты ревьюер. Проверь изменение и receipt проверок. Ответь JSON: "
            '{"verdict":"approve|changes_requested","candidate_digest":"sha256:...",'
            '"findings":[{"severity":"...","issue":"...","where":"..."}]}.'
        ),
    },
    "archivist": {
        "name": "Архивариус",
        "goal": "зафиксировать итог и артефакты",
        "output": "record",
        "schema": {"summary": lambda v: None if _str(v) else "summary: ожидалась строка",
                   "artifacts": _schema_artifacts,
                   "decisions": lambda v: None if _str_list(v) else "decisions: ожидался список строк"},
        "prompt": (
            "Ты архивариус. Зафиксируй итог. Ответь JSON: "
            '{"summary":"...","artifacts":[{"path":"...","kind":"..."}],'
            '"decisions":["..."]}. Исходники не меняй. '
            "Не сливай ветку задания в основную, не делай push и не переключай "
            "основную рабочую копию: слияние проверенного коммита выполняет хаб."
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
# Тестер, ревьюер и архивариус работают в рабочей копии задачи и получают
# receipt проверок и digest кандидата отдельными секциями задания. Пакет
# контекста, собранный из основной копии на базовом SHA, для них вреден: он
# показывал бы код до изменений. Поэтому контекст у этих шагов пустой.
CONTEXT = {
    "planner": {"paths": ["docs/*", "*.md"], "budget_tokens": 3000},
    "coder": {"paths": ["src/*"], "budget_tokens": 6000},
    "tester": {},
    "reviewer": {},
    "archivist": {},
}


def template(project_entry: str = "", contracts: list[str] | None = None,
             agent: str = "codex") -> dict:
    """Шаблон запуска: цепочка ролей, схемы выходов и вход каждой роли."""
    steps = []
    for index, item in enumerate(CHAIN):
        role = ROLES[item["role"]]
        spec = dict(CONTEXT[item["role"]])
        context = {}
        if spec:
            context = {
                "goal": role["goal"],
                "entry": project_entry if index == 0 and project_entry else "",
                "contracts": list(contracts or []) if index == 0 else [],
                "paths": spec["paths"],
                "budget_tokens": spec["budget_tokens"],
            }
        steps.append({
            "key": item["key"],
            "role": item["role"],
            "agent": agent or "codex",
            "prompt": role["prompt"],
            "depends_on": list(item["depends_on"]),
            "output": role["output"],
            "context": context,
        })
    return {"id": FIVE_ROLES_ID, "version": FIVE_ROLES_VERSION, "steps": steps}


def describe() -> dict:
    return {
        "id": FIVE_ROLES_ID,
        "version": FIVE_ROLES_VERSION,
        "roles": [{"key": key, "name": spec["name"], "goal": spec["goal"],
                   "output": spec["output"], "candidate": bool(spec.get("candidate"))}
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
    """Строгая типизированная проверка выхода роли: JSON + схема полей.

    Поля из OPTIONAL проверяются по типу, только если присутствуют; остальные
    обязательны и строго типизированы.
    """
    spec = ROLES.get(role_key)
    if not spec:
        return False, None, f"неизвестная роль: {role_key}"
    parsed = _extract_json(text or "")
    if parsed is None:
        return False, None, "выход роли не является JSON-объектом"
    problems = []
    for field, check in spec["schema"].items():
        if field not in parsed:
            if field in OPTIONAL_FIELDS:
                continue
            problems.append(f"нет обязательного поля: {field}")
            continue
        reason = check(parsed[field])
        if reason:
            problems.append(reason)
    if problems:
        return False, parsed, "; ".join(problems)
    if role_key == "reviewer" and parsed.get("verdict") != "approve":
        return False, parsed, f"ревью просит изменения: {parsed.get('verdict')}"
    return True, parsed, None


def next_step_for_failure(role_key: str) -> str | None:
    """Куда возвращаться при провале проверки/ревью — правило, без LLM."""
    if role_key in ("tester", "reviewer"):
        return "code"
    return None
