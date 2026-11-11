"""FLOW-P1: durable контроллер запусков, связанный с целью и назначениями.

Запуск — это дерево задач с зависимостями и журналом событий. Инварианты:
  * запуск создан под конкретную цель и критерии; policy, снимок назначений
    (карточки ролей проекта на момент создания) и снимок project/workspace
    хранятся в строке запуска — правка карточки роли уже созданный запуск
    не меняет;
  * шаг не выдаётся без готового обязательного входа: пакет контекста должен
    быть собран (state sealed), кандидат — зафиксирован снимком, проверки —
    пройдены receipt'ом настоящего изолированного runner'а. Ошибка сборки
    контекста блокирует шаг и его зависимые, а не «задание выдано без входа»;
  * «тест пройден» не принимается со слов модели: тестировщик и ревьюер
    обязаны ссылаться на digest кандидата, по которому есть программный
    receipt; несовпадение — возврат кодеру или отказ;
  * выдача идемпотентна по ключу задания, приём результата — CAS по состоянию
    задачи: повтор результата не перезаписывает и не двигает процесс дважды;
  * отмена атомарно закрывает queued-задания запуска и запрашивает остановку
    running; неподтверждённые остановки видны отдельно в cancel_info;
  * run-wide бюджеты (шаги, вызовы, секунды) резервируются policy и режут
    выдачу/приём с явной причиной.

Состояние живёт в базе, а не в памяти: контроллер переживает перезапуск хаба.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import db, flow_gates, packets, templates

RUN_STATES = ("active", "paused", "cancelled", "done", "failed")
TASK_STATES = ("pending", "dispatched", "running", "done", "failed", "skipped",
               "cancelled", "blocked", "waiting_context")
ACTIVE_TASK_STATES = ("pending", "dispatched", "running", "waiting_context")

BUDGET_KEYS = ("max_dispatches", "max_job_calls", "max_seconds")


class RunError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def validate_template(template: dict) -> list[dict]:
    """Проверяет шаблон до создания запуска: ключи, роли, зависимости, циклы."""
    if not isinstance(template, dict):
        raise RunError("bad_template", "шаблон должен быть объектом")
    steps = template.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RunError("bad_template", "в шаблоне нет шагов")
    keys = []
    for step in steps:
        if not isinstance(step, dict):
            raise RunError("bad_template", "шаг должен быть объектом")
        key = str(step.get("key") or "").strip()
        if not key:
            raise RunError("bad_template", "у шага нет ключа")
        if key in keys:
            raise RunError("duplicate_step", f"ключ шага повторяется: {key}")
        keys.append(key)
    known = set(keys)
    for step in steps:
        deps = step.get("depends_on") or []
        if not isinstance(deps, list):
            raise RunError("bad_template", f"depends_on должен быть списком: {step['key']}")
        for dep in deps:
            if dep not in known:
                raise RunError("unknown_dependency", f"{step['key']} зависит от неизвестного {dep}")
            if dep == step["key"]:
                raise RunError("self_dependency", f"{step['key']} зависит от себя")
    # цикл: топологическая проверка
    graph = {s["key"]: set(s.get("depends_on") or []) for s in steps}
    seen, stack = set(), set()

    def visit(node: str) -> None:
        if node in stack:
            raise RunError("dependency_cycle", f"цикл зависимостей вокруг {node}")
        if node in seen:
            return
        stack.add(node)
        for dep in graph[node]:
            visit(dep)
        stack.discard(node)
        seen.add(node)

    for node in graph:
        visit(node)
    return steps


def _as_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def validate_policy(policy: dict | None) -> dict:
    """Политика запуска: max_cycles и run-wide бюджеты с явными границами."""
    policy = dict(policy or {})
    budgets = policy.get("budgets") or {}
    if not isinstance(budgets, dict):
        raise RunError("bad_policy", "budgets должен быть объектом")
    clean_budgets = {}
    for key in BUDGET_KEYS:
        value = budgets.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RunError("bad_policy", f"budgets.{key}: ожидалось положительное целое")
        clean_budgets[key] = value
    policy["budgets"] = clean_budgets
    cycles = policy.get("max_cycles")
    if cycles is not None and (isinstance(cycles, bool) or not isinstance(cycles, int)
                               or cycles < 0):
        raise RunError("bad_policy", "max_cycles: ожидалось неотрицательное целое")
    return policy


def snapshot_assignments(project: dict, steps: list[dict]) -> dict:
    """Снимок назначений: карточки ролей проекта на момент создания запуска.

    Дальнейшие правки карточек роли этот запуск не меняют — dispatch читает
    только этот снимок.
    """
    cards = {r.get("role_id"): r for r in (project.get("roles") or [])}
    assignments = {}
    for step in steps:
        role = step.get("role") or ""
        card = cards.get(role) or cards.get(db.ROLE_ALIASES.get(role, role)) or {}
        assignments[step["key"]] = {
            "role": role,
            "agent_id": (card.get("agent_id") or "").strip() or (step.get("agent") or ""),
            "profile": card.get("profile") or None,
            "model": card.get("model") or None,
        }
    return assignments


def _idempotency_payload(project_id: str, template_id: str, goal: str,
                         criteria: list, policy: dict, assignments: dict) -> str:
    body = {
        "project_id": project_id, "template_id": template_id, "goal": goal,
        "criteria": criteria, "policy": policy, "assignments": assignments,
        "format": "flow-run-v1",
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def create_run(project_id: str, template: dict, policy: dict | None = None,
               actor: str = "hub-token-holder", goal: str = "",
               criteria: list | None = None, request_key: str | None = None) -> dict:
    """Создаёт запуск: цель, критерии, policy и снимки фиксируются транзакцией.

    Повтор с тем же request_key и тем же содержимым возвращает прежний запуск;
    тот же ключ с другим содержимым — RunError("idempotency_conflict").
    """
    steps = validate_template(template)
    policy = validate_policy(policy)
    goal = str(goal or "").strip()
    criteria = [str(c) for c in (criteria or []) if str(c).strip()]
    project = db.get_project(project_id)
    if not project:
        raise RunError("not_found", "проект не найден")
    assignments = snapshot_assignments(project, steps)
    template_id = str(template.get("id") or "default")
    version = int(template.get("version") or 1)
    project_snapshot = {
        "project_id": project_id,
        "project_host_id": project.get("host_id") or "",
        "project_workdir": project.get("workdir") or "",
    }
    payload_sha = _idempotency_payload(project_id, template_id, goal, criteria,
                                       policy, assignments)
    if request_key:
        existing = _run_by_key(request_key)
        if existing:
            if existing.get("idempotency_sha") == payload_sha:
                return {"run_id": existing["id"], "state": existing.get("state") or "active",
                        "template_id": template_id, "template_version": version,
                        "steps": len(steps), "existing": True}
            raise RunError("idempotency_conflict",
                           "request_key уже занят другим содержимым запуска",
                           {"existing_run_id": existing["id"]})
    run_id = uuid.uuid4().hex
    with db._lock:
        conn = db.connect()
        with db.transaction(conn):
            db._one(conn, db._qualify(
                "insert into aihub.runs(id, project_id, template_id, template_version, policy,"
                " state, goal, criteria, assignments, project_snapshot, budgets, request_key,"
                " idempotency_sha, created_at, updated_at)"
                " values (:i, :p, :t, :v, :pol::jsonb, 'active', :g, :cr::jsonb, :asn::jsonb,"
                "  :ps::jsonb, :b::jsonb, :rk, :sha, now(), now()) returning id"),
                i=run_id, p=project_id, t=template_id, v=version,
                pol=json.dumps(policy, ensure_ascii=False), g=goal,
                cr=json.dumps(criteria, ensure_ascii=False),
                asn=json.dumps(assignments, ensure_ascii=False),
                ps=json.dumps(project_snapshot, ensure_ascii=False),
                b=json.dumps(policy.get("budgets") or {}, ensure_ascii=False),
                rk=request_key, sha=payload_sha)
            for index, step in enumerate(steps):
                step_context = step.get("context") or {}
                db._one(conn, db._qualify(
                    "insert into aihub.run_tasks(id, run_id, key, role, agent, prompt, depends_on,"
                    " position, state, attempt, context, assignment, created_at, updated_at)"
                    " values (:i, :r, :k, :role, :ag, :pr, :d, :pos, 'pending', 0,"
                    "  :ctx::jsonb, :asn::jsonb, now(), now()) returning id"),
                    i=uuid.uuid4().hex, r=run_id, k=step["key"], role=step.get("role") or "",
                    ag=step.get("agent") or "", pr=step.get("prompt") or "",
                    d=json.dumps(list(step.get("depends_on") or []), ensure_ascii=False),
                    pos=index,
                    ctx=json.dumps(step_context, ensure_ascii=False),
                    asn=json.dumps(assignments.get(step["key"]) or {}, ensure_ascii=False))
            event(conn, run_id, None, "run_created",
                  {"template": template_id, "version": version, "steps": len(steps),
                   "actor": actor, "goal": goal, "criteria": criteria,
                   "request_key": request_key})
    return {"run_id": run_id, "state": "active", "template_id": template_id,
            "template_version": version, "steps": len(steps), "existing": False}


def _run_by_key(request_key: str) -> dict | None:
    with db._lock:
        conn = db.connect()
        return db._one(conn, db._qualify(
            "select id, state, idempotency_sha from aihub.runs where request_key = :k"),
            k=request_key)


def event(conn, run_id: str, task_id: str | None, name: str, payload: dict | None = None) -> None:
    conn.run(db._qualify(
        "insert into aihub.run_events(run_id, task_id, event, payload)"
        " values (:r, :t, :e, :p::jsonb)"),
        r=run_id, t=task_id, e=name,
        p=json.dumps(payload or {}, ensure_ascii=False))


def list_runs(project_id: str | None = None, limit: int = 30) -> list[dict]:
    """Список запусков с прогрессом: интерфейс восстанавливает состояние из базы."""
    with db._lock:
        conn = db.connect()
        if project_id:
            rows = db._rows(conn, db._qualify(
                "select r.*, (select count(*) from aihub.run_tasks t where t.run_id = r.id) as total,"
                " (select count(*) from aihub.run_tasks t where t.run_id = r.id and t.state = 'done') as done,"
                " (select t.key from aihub.run_tasks t where t.run_id = r.id and t.state in ('dispatched','running') limit 1) as current"
                " from aihub.runs r where r.project_id = :p order by r.created_at desc limit :l"),
                p=project_id, l=int(limit))
        else:
            rows = db._rows(conn, db._qualify(
                "select r.*, (select count(*) from aihub.run_tasks t where t.run_id = r.id) as total,"
                " (select count(*) from aihub.run_tasks t where t.run_id = r.id and t.state = 'done') as done,"
                " (select t.key from aihub.run_tasks t where t.run_id = r.id and t.state in ('dispatched','running') limit 1) as current"
                " from aihub.runs r order by r.created_at desc limit :l"), l=int(limit))
    out = []
    for row in rows:
        out.append(_public_run(dict(row)))
    return out


def _public_run(item: dict) -> dict:
    for key in ("created_at", "updated_at", "finished_at"):
        if item.get(key) is not None:
            item[key] = item[key].isoformat()
    for key in ("policy", "criteria", "assignments", "project_snapshot", "budgets",
                "budget_used", "check_receipt", "pipeline", "cancel_info"):
        if key in item:
            item[key] = _as_json(item.get(key), {} if key != "criteria" else [])
    return item


def get_run(run_id: str) -> dict | None:
    with db._lock:
        conn = db.connect()
        run = db._one(conn, db._qualify("select * from aihub.runs where id = :i"), i=run_id)
        if not run:
            return None
        tasks = db._rows(conn, db._qualify(
            "select * from aihub.run_tasks where run_id = :r order by position"), r=run_id)
        events = db._rows(conn, db._qualify(
            "select id, task_id, event, payload, created_at from aihub.run_events"
            " where run_id = :r order by id"), r=run_id)
    out = _public_run(dict(run))
    out["tasks"] = []
    for row in tasks:
        item = dict(row)
        for key in ("created_at", "updated_at", "finished_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat()
        for key in ("depends_on", "context", "assignment", "result", "receipt",
                    "retry_feedback"):
            if key in item:
                item[key] = _as_json(item.get(key), [] if key == "depends_on" else {})
        out["tasks"].append(item)
    out["events"] = []
    for row in events:
        item = dict(row)
        if item.get("created_at") is not None:
            item["created_at"] = item["created_at"].isoformat()
        item["payload"] = _as_json(item.get("payload"), {})
        out["events"].append(item)
    return out


def _ready(conn, run_id: str) -> list[dict]:
    """Задачи, готовые к выдаче: не выполняются, зависимости закрыты успешно."""
    tasks = db._rows(conn, db._qualify(
        "select * from aihub.run_tasks where run_id = :r order by position"), r=run_id)
    by_key = {t["key"]: t for t in tasks}
    ready = []
    for task in tasks:
        if task["state"] != "pending":
            continue
        deps = _as_json(task.get("depends_on"), [])
        if all((by_key.get(dep) or {}).get("state") == "done" for dep in deps):
            ready.append(task)
    return ready


def _check_budgets(conn, run: dict) -> str | None:
    """Превышение run-wide бюджета — явная причина остановки выдачи."""
    budgets = _as_json(run.get("budgets"), {}) or {}
    used = _as_json(run.get("budget_used"), {}) or {}
    if budgets.get("max_seconds"):
        row = db._one(conn, db._qualify(
            "select extract(epoch from (now() - created_at))::int as elapsed"
            " from aihub.runs where id = :i"), i=run["id"])
        elapsed = int((row or {}).get("elapsed") or 0)
        if elapsed >= int(budgets["max_seconds"]):
            return f"бюджет времени исчерпан: {elapsed}s из {budgets['max_seconds']}s"
    if budgets.get("max_dispatches") and int(used.get("dispatches") or 0) >= int(budgets["max_dispatches"]):
        return f"бюджет шагов исчерпан: {used.get('dispatches')} из {budgets['max_dispatches']}"
    if budgets.get("max_job_calls") and int(used.get("job_calls") or 0) >= int(budgets["max_job_calls"]):
        return f"бюджет вызовов исчерпан: {used.get('job_calls')} из {budgets['max_job_calls']}"
    return None


def _bump_budget(conn, run_id: str, counter: str, amount: int = 1) -> None:
    db._one(conn, db._qualify(
        "update aihub.runs set budget_used = coalesce(budget_used, '{}'::jsonb)"
        " || jsonb_build_object(:k::text, coalesce((budget_used->>:k)::int, 0) + :a),"
        " updated_at = now() where id = :i returning id"),
        k=counter, a=int(amount), i=run_id)


def _packet_manifest_lines(packet: dict, limit: int = 40) -> list[str]:
    lines = []
    for item in (packet.get("items") or [])[:limit]:
        if not item.get("selected"):
            continue
        lines.append(f"- {item['path']} ({int(item.get('size') or 0)} B, sha256 {str(item.get('sha256') or '-')[:16]}…)")
    return lines


def _task_inputs(conn, run: dict, task: dict) -> str:
    """Фактический вход шага: выходы предыдущих шагов + цель и критерии запуска."""
    parts = []
    if run.get("goal"):
        parts.append("Цель запуска: " + run["goal"])
    criteria = _as_json(run.get("criteria"), []) or []
    if criteria:
        parts.append("Критерии приёмки: " + "; ".join(criteria))
    tasks = db._rows(conn, db._qualify(
        "select key, role, result, state from aihub.run_tasks where run_id = :r"), r=run["id"])
    by_key = {t["key"]: t for t in tasks}
    for dep in (_as_json(task.get("depends_on"), []) or []):
        source = by_key.get(dep)
        if not source:
            continue
        result = _as_json(source.get("result"), {})
        if result:
            body = json.dumps(result, ensure_ascii=False)[:4000]
            parts.append(f"Вход от шага «{dep}» ({source.get('role') or 'роль'}):\n{body}")
        else:
            parts.append(f"Вход от шага «{dep}»: состояние {source.get('state')}, результата нет.")
    return "\n\n".join(parts)


def _gate_context(conn, run: dict, task: dict) -> tuple[str | None, dict | None]:
    """Обязательный контекст шага. Возвращает (причина_блокировки, пакет).

    Без готового пакета шаг не выдаётся: задача уходит в waiting_context, при
    ошибке сборки — в failed с зависимыми, заблокированными явно.
    """
    context_spec = _as_json(task.get("context"), {}) or {}
    if not context_spec:
        return None, None
    packet_id = task.get("context_id")
    if packet_id:
        packet = packets.get(packet_id)
        if not packet:
            return "пакет контекста не найден", None
        if packet["state"] == "sealed":
            selected = [item for item in (packet.get("items") or []) if item.get("selected")]
            saved = packets.verified_contents(packet["id"], int(packet.get("revision") or 1))
            if selected and not saved:
                try:
                    packets.start_materialize(packet["id"])
                except Exception as exc:  # noqa: BLE001
                    return f"контекст не материализован: {exc}"[:300], None
                conn.run(db._qualify(
                    "update aihub.run_tasks set state = 'waiting_context', updated_at = now()"
                    " where id = :i and state = 'pending'"), i=task["id"])
                event(conn, run["id"], task["id"], "task_waiting_context",
                      {"packet_id": packet["id"], "reason": "materializing"})
                return "контекст ещё собирается", None
            return None, packet
        if packet["state"] == "materialized":
            return None, packet
        if packet["state"] in ("failed", "conflict"):
            reason = (packet.get("manifest") or {}).get("error") or f"пакет в состоянии {packet['state']}"
            return f"контекст не собран: {reason}", None
        return "контекст ещё собирается", None
    # Первый заход: ставим сборку пакета и ждём готовности следующими тиками.
    project = db.get_project(run["project_id"]) or {}
    try:
        entry_path = context_spec.get("entry") or ""
        contracts = [c for c in (context_spec.get("contracts") or []) if c]
        if entry_path:
            ctx = packets.create_context(
                project, context_spec.get("goal") or run.get("goal") or "",
                entry_path, contracts, int(context_spec.get("budget_tokens") or 2000))
        elif context_spec.get("paths"):
            ctx = packets.start(project, list(context_spec["paths"]),
                                int(context_spec.get("budget_tokens") or 2000) * 4)
        else:
            return "в контексте шага нет ни entry, ни paths", None
    except Exception as exc:  # noqa: BLE001
        return f"контекст не собран: {exc}"[:300], None
    conn.run(db._qualify(
        "update aihub.run_tasks set context_id = :c, state = 'waiting_context',"
        " updated_at = now() where id = :i"), c=ctx["packet_id"], i=task["id"])
    event(conn, run["id"], task["id"], "task_waiting_context", {"packet_id": ctx["packet_id"]})
    return "контекст ещё собирается", None


def _role_gates(conn, run: dict, task: dict) -> str | None:
    """Ролевые ворота: test/review не выдаются без кандидата и receipt проверок."""
    role = task.get("role") or ""
    if role not in ("tester", "reviewer"):
        return None
    if not run.get("candidate_digest"):
        return "кандидат ещё не зафиксирован снимком"
    receipt = _as_json(run.get("check_receipt"), {}) or {}
    if not receipt:
        return "проверки кандидата ещё не выполнены"
    try:
        flow_gates.validate_check_receipt(receipt, snapshot_id=run.get("candidate_snapshot_id") or "")
    except flow_gates.GateError as exc:
        return f"receipt проверок не принят: {exc.message}"
    return None


def dispatch(run_id: str, resolve_agent, max_queue: int = 50) -> dict:
    """Выдаёт готовые задачи в очередь. Идемпотентно по operation key.

    Назначение берётся из снимка запуска, а не из живых карточек ролей; модель
    и профиль передаются в задание; снимок project/workspace/packet/candidate
    кладётся в run_snapshot задания. Шаг без готового обязательного контекста
    или без кандидата/receipt не выдаётся.
    """
    created = []
    with db._lock:
        conn = db.connect()
        with db.transaction(conn):
            run = db._one(conn, db._qualify(
                "select * from aihub.runs where id = :i for update"), i=run_id)
            if not run:
                raise RunError("not_found", "запуск не найден")
            if run["state"] != "active":
                return {"run_id": run_id, "dispatched": [], "state": run["state"],
                        "reason": "запуск не активен"}
            over = _check_budgets(conn, run)
            if over:
                _finish_run(conn, run_id, "failed", "budget_exceeded", over)
                return {"run_id": run_id, "dispatched": [], "state": "failed",
                        "reason": over}
            assignments = _as_json(run.get("assignments"), {}) or {}
            waiting = db._rows(conn, db._qualify(
                "select * from aihub.run_tasks where run_id = :r and state = 'waiting_context'"
                " order by position"), r=run_id)
            candidates = _ready(conn, run_id) + waiting
            for task in candidates:
                if task["state"] not in ("pending", "waiting_context"):
                    continue
                context_block, packet = _gate_context(conn, run, task)
                if task["state"] == "waiting_context":
                    # _gate_context уже перевёл задачу либо вернул готовый пакет.
                    task = db._one(conn, db._qualify(
                        "select * from aihub.run_tasks where id = :i for update"), i=task["id"])
                    if not task or task["state"] != "waiting_context":
                        continue
                if context_block:
                    if context_block != "контекст ещё собирается":
                        _fail_task(conn, run, task, context_block)
                    continue
                role_block = _role_gates(conn, run, task)
                if role_block:
                    event(conn, run_id, task["id"], "task_waiting_input",
                          {"reason": role_block})
                    continue
                assignment = dict(assignments.get(task["key"]) or {})
                agent_id = assignment.get("agent_id") or task.get("agent") or ""
                bridge_agent, error = resolve_agent(agent_id)
                if not bridge_agent:
                    event(conn, run_id, task["id"], "task_blocked",
                          {"reason": error or "агент не найден"})
                    continue
                prompt = task.get("prompt") or task["key"]
                sections = [prompt, _task_inputs(conn, run, task)]
                if packet:
                    context_files = packets.verified_contents(
                        packet["id"], int(packet.get("revision") or 1))
                    sections.append(
                        "Пакет контекста: " + packet["id"] + " (ревизия "
                        + str(packet.get("revision")) + ", digest " + str(packet.get("digest"))[:20]
                        + "…)\nВыбранные файлы:\n" + "\n".join(_packet_manifest_lines(packet)))
                    if context_files:
                        sections.append(
                            "Проверенное содержимое выбранных UTF-8 файлов (байты сверены с SHA-256;"
                            " бюджет пакета соблюдён):\n" + json.dumps(
                                [{"path": item["path"], "sha256": item["sha256"],
                                  "content": item["content"]} for item in context_files],
                                ensure_ascii=False))
                    conn.run(db._qualify(
                        "update aihub.run_tasks set packet_revision = :rev where id = :i"),
                        rev=int(packet.get("revision") or 1), i=task["id"])
                receipt = _as_json(run.get("check_receipt"), {}) or {}
                if receipt and (task.get("role") or "") in ("tester", "reviewer"):
                    body = json.dumps(receipt, ensure_ascii=False)[:6000]
                    sections.append("Receipt изолированного runner'а (программное доказательство):\n" + body)
                    sections.append("Кандидат: " + str(run.get("candidate_digest")))
                if (task.get("role") or "") == "reviewer" and run.get("candidate_digest"):
                    sections.append("Проверяемый кандидат: " + str(run.get("candidate_digest")))
                retry_feedback = _as_json(task.get("retry_feedback"), {}) or {}
                if (task.get("role") or "") == "coder" and retry_feedback:
                    sections.append("Обратная связь после неудачной проверки/ревью (учти при исправлении):\n"
                                    + json.dumps(retry_feedback, ensure_ascii=False)[:12000])
                prompt = "\n\n".join(s for s in sections if s)
                attempt = int(task["attempt"] or 0) + 1
                key = f"run:{run_id}:{task['key']}:{attempt}"
                sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                project_snapshot = _as_json(run.get("project_snapshot"), {}) or {}
                workspace_snapshot = {
                    "run_id": run_id,
                    "run_task_id": task["id"],
                    "role_id": task.get("role") or "",
                    "project_id": run["project_id"],
                    "project_host_id": project_snapshot.get("project_host_id") or "",
                    "project_workdir": project_snapshot.get("project_workdir") or "",
                    "workdir": project_snapshot.get("project_workdir") or "",
                    "packet_id": (packet or {}).get("id") or task.get("context_id"),
                    "packet_revision": int((packet or {}).get("revision") or 0) or None,
                    "candidate_digest": run.get("candidate_digest"),
                    "assignment": assignment,
                }
                job, used = db.create_job(
                    bridge_agent, prompt, sha, 900, key, max_queue,
                    assignment.get("model"), assignment.get("profile"),
                    workspace=workspace_snapshot)
                conn.run(db._qualify(
                    "update aihub.run_tasks set state = 'dispatched', attempt = :a, job_id = :j,"
                    " assignment = :asn::jsonb, retry_feedback = '{}'::jsonb,"
                    " updated_at = now() where id = :i"),
                    a=attempt, j=job["id"],
                    asn=json.dumps(assignment | {"bridge_agent": bridge_agent},
                                   ensure_ascii=False),
                    i=task["id"])
                _bump_budget(conn, run_id, "dispatches")
                event(conn, run_id, task["id"], "task_dispatched",
                      {"job_id": job["id"], "attempt": attempt, "agent": agent_id,
                       "bridge_agent": bridge_agent, "model": assignment.get("model"),
                       "profile": assignment.get("profile"),
                       "packet_id": workspace_snapshot["packet_id"],
                       "packet_revision": workspace_snapshot["packet_revision"]})
                created.append({"task": task["key"], "job_id": job["id"],
                                "attempt": attempt, "created": used})
            _maybe_finish_run(conn, run_id)
    return {"run_id": run_id, "dispatched": created, "state": "active"}


def _blocked_dependents(conn, run_id: str, failed_key: str, reason: str,
                        memo: set | None = None) -> list[str]:
    """Все ещё не закрытые зависимые задачи явно закрываются состоянием blocked."""
    memo = memo if memo is not None else set()
    tasks = db._rows(conn, db._qualify(
        "select id, key, depends_on, state from aihub.run_tasks where run_id = :r"),
        r=run_id)
    blocked = []
    for task in tasks:
        deps = _as_json(task.get("depends_on"), []) or []
        if failed_key not in deps or task["key"] in memo:
            continue
        memo.add(task["key"])
        if task["state"] in ACTIVE_TASK_STATES or task["state"] == "blocked":
            conn.run(db._qualify(
                "update aihub.run_tasks set state = 'blocked', error = :e, updated_at = now(),"
                " finished_at = coalesce(finished_at, now()) where id = :i"),
                e=reason[:400], i=task["id"])
            event(conn, run_id, task["id"], "task_blocked", {"reason": reason})
            blocked.append(task["key"])
            blocked.extend(_blocked_dependents(conn, run_id, task["key"], reason, memo))
    return blocked


def _fail_task(conn, run: dict, task: dict, reason: str | None) -> None:
    conn.run(db._qualify(
        "update aihub.run_tasks set state = 'failed', error = :e, updated_at = now(),"
        " finished_at = now() where id = :i"), e=(reason or None), i=task["id"])
    event(conn, run["id"], task["id"], "task_failed", {"error": reason})
    _blocked_dependents(conn, run["id"], task["key"],
                        f"заблокировано: шаг «{task['key']}» не выполнен — {reason or 'ошибка'}")


def _finish_run(conn, run_id: str, state: str, event_name: str, reason: str | None) -> None:
    conn.run(db._qualify(
        "update aihub.runs set state = :s, updated_at = now(),"
        " finished_at = now() where id = :i"), s=state, i=run_id)
    event(conn, run_id, None, event_name, {"reason": reason} if reason else {})


def _maybe_finish_run(conn, run_id: str) -> None:
    """Закрывает запуск, когда не осталось активных задач."""
    run = db._one(conn, db._qualify("select state from aihub.runs where id = :i"), i=run_id)
    if not run or run["state"] != "active":
        return
    states = {row["state"] for row in db._rows(conn, db._qualify(
        "select state from aihub.run_tasks where run_id = :r"), r=run_id)}
    if states & set(ACTIVE_TASK_STATES):
        return
    if states <= {"done", "skipped"}:
        _finish_run(conn, run_id, "done", "run_done", None)
    else:
        _finish_run(conn, run_id, "failed", "run_failed",
                    "есть шаги в состоянии failed/blocked/cancelled")


def set_state(run_id: str, action: str, actor: str = "hub-token-holder") -> dict:
    target = {"pause": "paused", "resume": "active", "cancel": "cancelled"}.get(action)
    if not target:
        raise RunError("bad_action", f"неизвестное действие: {action}")
    with db._lock:
        conn = db.connect()
        with db.transaction(conn):
            run = db._one(conn, db._qualify(
                "select * from aihub.runs where id = :i for update"), i=run_id)
            if not run:
                raise RunError("not_found", "запуск не найден")
            if run["state"] == "cancelled" and action != "cancel":
                raise RunError("cancelled", "запуск отменён")
            if action == "cancel" and run["state"] not in ("active", "paused"):
                raise RunError("bad_state", f"запуск в состоянии {run['state']}, отмена невозможна")
            conn.run(db._qualify(
                "update aihub.runs set state = :s, updated_at = now(),"
                " finished_at = case when :s = 'cancelled' then now() else finished_at end"
                " where id = :i"), s=target, i=run_id)
            if action == "cancel":
                # Атомарная отмена: не начатые задания закрываем сразу, работающим
                # шлём запрос остановки; неподтверждённые остановки видны отдельно.
                tasks = db._rows(conn, db._qualify(
                    "select id, key, job_id, state from aihub.run_tasks"
                    " where run_id = :r and state in ('pending', 'dispatched',"
                    " 'waiting_context', 'running')"), r=run_id)
                cancelled, stop_requested = [], []
                for task in tasks:
                    conn.run(db._qualify(
                        "update aihub.run_tasks set state = 'cancelled', updated_at = now(),"
                        " finished_at = now() where id = :i"), i=task["id"])
                    event(conn, run_id, task["id"], "task_cancelled", {"actor": actor})
                    if task.get("job_id"):
                        status = db.cancel_job(task["job_id"])
                        if status == "cancelled":
                            cancelled.append(task["job_id"])
                        elif status == "cancel_requested":
                            stop_requested.append(task["job_id"])
                        else:
                            stop_requested.append(task["job_id"])
                conn.run(db._qualify(
                    "update aihub.runs set cancel_info = :c::jsonb where id = :i"),
                    c=json.dumps({"jobs_cancelled": cancelled,
                                  "stop_requested": stop_requested,
                                  "actor": actor}, ensure_ascii=False), i=run_id)
                event(conn, run_id, None, "run_cancel",
                      {"actor": actor, "jobs_cancelled": len(cancelled),
                       "stop_requested": len(stop_requested)})
            else:
                event(conn, run_id, None, f"run_{action}", {"actor": actor})
    return {"run_id": run_id, "state": target}


def _cancel_pipeline_jobs(conn, run: dict) -> None:
    """При сбросе конвейера не оставляем висящих заданий снимка/проверок."""
    pipeline = _as_json(run.get("pipeline"), {}) or {}
    for field in ("candidate_job_id", "checks_job_id"):
        job_id = pipeline.pop(field, None)
        if not job_id:
            continue
        db.cancel_job(job_id)
        event(conn, run["id"], None, "pipeline_job_cancelled", {"job_id": job_id})
    conn.run(db._qualify(
        "update aihub.runs set pipeline = :p::jsonb, candidate_digest = null,"
        " candidate_snapshot_id = null, check_receipt = null, updated_at = now()"
        " where id = :i"),
        p=json.dumps(pipeline, ensure_ascii=False), i=run["id"])


def on_task_result(job_id: str, ok: bool, error: str | None = None) -> dict | None:
    """Закрывает задачу по результату её job и двигает запуск дальше.

    CAS по состоянию задачи: повтор результата фиксируется в журнале, но
    состояние и процесс не двигаются второй раз.
    """
    with db._lock:
        conn = db.connect()
        with db.transaction(conn):
            task = db._one(conn, db._qualify(
                "select * from aihub.run_tasks where job_id = :j for update"), j=job_id)
            if not task:
                return None
            if task["state"] not in ("dispatched", "running"):
                run_row = db._one(conn, db._qualify(
                    "select state from aihub.runs where id = :i"), i=task["run_id"])
                if run_row and run_row["state"] == "cancelled":
                    # Результат после отмены: сохраняем с историей, не продвигая.
                    job = db.get_job(job_id) or {}
                    conn.run(db._qualify(
                        "update aihub.run_tasks set result = :r::jsonb, updated_at = now()"
                        " where id = :i"),
                        r=json.dumps({"output": (job.get("result_output") or "")[:8000]},
                                     ensure_ascii=False), i=task["id"])
                    event(conn, task["run_id"], task["id"], "result_after_cancel",
                          {"job_status": job.get("status")})
                    return {"run_id": task["run_id"], "task": task["key"],
                            "state": "cancelled", "kept": "history"}
                event(conn, task["run_id"], task["id"], "duplicate_result",
                      {"state": task["state"], "ignored": True})
                return {"run_id": task["run_id"], "task": task["key"],
                        "state": task["state"], "duplicate": True}
            run = db._one(conn, db._qualify(
                "select * from aihub.runs where id = :i for update"), i=task["run_id"])
            job = db.get_job(job_id) or {}
            output = job.get("result_output") or ""
            _bump_budget(conn, task["run_id"], "job_calls")
            parsed = None
            role = task.get("role") or ""
            if ok and role in templates.ROLES:
                valid, parsed, reason = templates.validate_output(role, output)
                if valid:
                    try:
                        if role == "tester":
                            flow_gates.gate_tester_output(
                                parsed, candidate_digest=run.get("candidate_digest") or "")
                        elif role == "reviewer":
                            flow_gates.gate_reviewer_output(
                                parsed, candidate_digest=run.get("candidate_digest") or "")
                    except flow_gates.GateError as exc:
                        valid, parsed, reason = False, parsed, exc.message
                if not valid:
                    decision = retry_decision(role, int(run.get("cycles") or 0),
                                              _as_json(run.get("policy"), {}))
                    if decision["action"] == "retry_coder":
                        _restart_from_coder(conn, task, decision, {
                            "role": role, "reason": reason or decision["reason"],
                            "output": output[:6000],
                        })
                        return {"run_id": task["run_id"], "task": task["key"],
                                "state": "retry", "reason": decision["reason"]}
                    state, error = "failed", (reason or decision["reason"])
                else:
                    state, error = "done", None
            else:
                state = "done" if ok else "failed"
            if run["state"] == "cancelled":
                # Результат после отмены: сохраняем с историей, процесс не двигаем.
                conn.run(db._qualify(
                    "update aihub.run_tasks set result = :r::jsonb, updated_at = now()"
                    " where id = :i"),
                    r=json.dumps({"output": output[:8000]}, ensure_ascii=False), i=task["id"])
                event(conn, task["run_id"], task["id"], "result_after_cancel",
                      {"job_status": job.get("status")})
                return {"run_id": task["run_id"], "task": task["key"],
                        "state": "cancelled", "kept": "history"}
            if state == "done":
                conn.run(db._qualify(
                    "update aihub.run_tasks set state = 'done', error = null, result = :r::jsonb,"
                    " updated_at = now(), finished_at = now() where id = :i"),
                    r=json.dumps(parsed if parsed is not None else {"output": output[:8000]},
                                 ensure_ascii=False),
                    i=task["id"])
                event(conn, task["run_id"], task["id"], "task_done", {})
            else:
                _fail_task(conn, run, task, error)
            _maybe_finish_run(conn, task["run_id"])
    return {"run_id": task["run_id"], "task": task["key"], "state": state}


def _restart_from_coder(conn, task: dict, decision: dict,
                        feedback: dict | None = None) -> None:
    """Возврат к кодеру: конвейер кандидата сбрасывается, последующие шаги в очередь.

    Старые тесты и ревью относятся к прежнему кандидату: digest кандидата,
    receipt проверок и висящие задания снимка/проверок инвалидируются, поэтому
    повторно приняты быть не могут.
    """
    run = db._one(conn, db._qualify(
        "select * from aihub.runs where id = :i for update"), i=task["run_id"])
    if run:
        _cancel_pipeline_jobs(conn, run)
    tasks = db._rows(conn, db._qualify(
        "select id, key, state from aihub.run_tasks where run_id = :r"), r=task["run_id"])
    coder = next((t for t in tasks if t["key"] == "code"), None)
    if coder:
        conn.run(db._qualify(
            "update aihub.run_tasks set state = 'pending', error = null, context_id = null,"
            " retry_feedback = :f::jsonb, updated_at = now() where id = :i"),
            i=coder["id"], f=json.dumps(feedback or {
                "role": task.get("role") or "pipeline", "reason": decision["reason"]
            }, ensure_ascii=False))
        event(conn, task["run_id"], coder["id"], "task_requeued",
              {"reason": decision["reason"], "cycle": decision.get("next_cycle")})
    for row in tasks:
        if row["key"] in ("test", "review", "archive") and row["state"] in (
                "done", "failed", "blocked"):
            conn.run(db._qualify(
                "update aihub.run_tasks set state = 'pending', error = null, result = null,"
                " receipt = null, updated_at = now(), finished_at = null where id = :i"),
                i=row["id"])
    conn.run(db._qualify(
        "update aihub.runs set cycles = cycles + 1, updated_at = now() where id = :i"),
        i=task["run_id"])
    conn.run(db._qualify(
        "update aihub.run_tasks set state = 'pending', error = null, updated_at = now(),"
        " finished_at = null where id = :i"), i=task["id"])
    event(conn, task["run_id"], task["id"], "task_retry",
          {"reason": decision["reason"], "cycle": decision.get("next_cycle")})


# ---------- программный конвейер: снимок кандидата и проверки без слов модели ----------

def _project_checks(project_id: str) -> list[str]:
    project = db.get_project(project_id) or {}
    settings = _as_json(project.get("settings"), {}) or {}
    plan = settings.get("checks")
    if not isinstance(plan, dict):
        return []
    return [name for name, item in plan.items() if isinstance(item, dict)]


def _advance_pipeline(conn, run: dict) -> None:
    """Кандидат фиксируется снимком дерева, проверки запускает хаб, не модель.

    Состояние конвейера — в run.pipeline; каждый переход журналируется. Ошибки
    снимка/проверок — инфраструктурные: задача кодера возвращается в очередь
    в рамках лимита циклов, иначе запуск падает с явной причиной.
    """
    if run["state"] != "active":
        return
    pipeline = _as_json(run.get("pipeline"), {}) or {}
    changed = False

    def save(extra: dict | None = None) -> None:
        nonlocal changed
        changed = True
        if extra:
            pipeline.update(extra)
        conn.run(db._qualify(
            "update aihub.runs set pipeline = :p::jsonb, updated_at = now() where id = :i"),
            p=json.dumps(pipeline, ensure_ascii=False), i=run["id"])

    candidate_job = pipeline.get("candidate_job_id")
    if candidate_job:
        try:
            job = workspace_job(candidate_job)
        except Exception:  # noqa: BLE001
            return
        if not job or job.get("status") not in ("succeeded", "failed", "cancelled", "timeout", "unknown"):
            return
        pipeline.pop("candidate_job_id")
        if job.get("status") == "succeeded":
            result = job.get("result") or {}
            snapshot_id = result.get("snapshot_id") or ""
            digest_value = result.get("tree_digest") or ""
            if not snapshot_id or not digest_value:
                _infra_fail(conn, run, pipeline, "снимок кандидата без snapshot_id/tree_digest", save)
                return
            conn.run(db._qualify(
                "update aihub.runs set candidate_snapshot_id = :s, candidate_digest = :d,"
                " updated_at = now() where id = :i"),
                s=snapshot_id, d=digest_value, i=run["id"])
            event(conn, run["id"], None, "candidate_ready",
                  {"snapshot_id": snapshot_id, "candidate_digest": digest_value})
            run["candidate_snapshot_id"] = snapshot_id
            run["candidate_digest"] = digest_value
            save()
            # Continue in this poll tick so the checks job is created as soon
            # as the candidate snapshot is accepted.
        else:
            _infra_fail(conn, run, pipeline,
                        f"снимок кандидата завершился статусом {job.get('status')}", save)
            return

    if run.get("candidate_digest") and not run.get("check_receipt") \
            and not pipeline.get("checks_job_id"):
        project = db.get_project(run["project_id"]) or {}
        checks = _project_checks(run["project_id"])
        if not checks:
            event(conn, run["id"], None, "checks_unavailable",
                  {"reason": "в настройках проекта нет утверждённых проверок"})
            save()
            return
        try:
            from . import workspace
            job = workspace.create_op_job(
                "run_checks",
                {"root": project.get("workdir") or "",
                 "snapshot_id": run.get("candidate_snapshot_id") or "",
                 "checks": checks},
                project.get("host_id") or "", run["project_id"],
                idempotency_key=f"run:{run['id']}:checks:{run.get('cycles') or 0}")
        except Exception as exc:  # noqa: BLE001
            event(conn, run["id"], None, "checks_unavailable", {"reason": str(exc)[:300]})
            save()
            return
        save({"checks_job_id": job["job"]["id"]})
        event(conn, run["id"], None, "checks_started",
              {"job_id": job["job"]["id"], "checks": checks})
        return

    checks_job = pipeline.get("checks_job_id")
    if checks_job:
        try:
            job = workspace_job(checks_job)
        except Exception:  # noqa: BLE001
            return
        if not job or job.get("status") not in ("succeeded", "failed", "cancelled", "timeout", "unknown"):
            return
        pipeline.pop("checks_job_id")
        save()
        if job.get("status") != "succeeded":
            _infra_fail(conn, run, pipeline,
                        f"проверки кандидата завершились статусом {job.get('status')}", save)
            return
        receipt = job.get("result") or {}
        try:
            flow_gates.validate_check_receipt(
                receipt, snapshot_id=run.get("candidate_snapshot_id") or "")
        except flow_gates.GateError as exc:
            _infra_fail(conn, run, pipeline, f"receipt проверок отклонён: {exc.message}", save,
                        feedback={"role": "run_checks", "reason": exc.message,
                                  "receipt": receipt})
            return
        conn.run(db._qualify(
            "update aihub.runs set check_receipt = :r::jsonb, updated_at = now() where id = :i"),
            r=json.dumps(receipt, ensure_ascii=False), i=run["id"])
        event(conn, run["id"], None, "checks_done",
              {"ok": receipt.get("ok"), "checks": len(receipt.get("checks") or [])})
        if receipt.get("ok") is not True:
            # Проверки реально упали: возврат кодеру в рамках политики циклов.
            decision = retry_decision("tester", int(run.get("cycles") or 0),
                                      _as_json(run.get("policy"), {}))
            code_task = db._one(conn, db._qualify(
                "select * from aihub.run_tasks where run_id = :r and key = 'code'"), r=run["id"])
            if code_task and decision["action"] == "retry_coder":
                _restart_from_coder(conn, code_task, decision, {
                    "role": "run_checks", "reason": "проверки кандидата не пройдены",
                    "receipt": receipt,
                })
            else:
                _finish_run(conn, run["id"], "failed", "run_failed",
                            decision["reason"] if decision["action"] == "fail"
                            else "проверки кандидата не пройдены")


def _infra_fail(conn, run: dict, pipeline: dict, reason: str, save,
                feedback: dict | None = None) -> None:
    """Инфраструктурный сбой конвейера: возврат кодеру или падение запуска."""
    save()
    event(conn, run["id"], None, "pipeline_failed", {"reason": reason})
    decision = retry_decision("tester", int(run.get("cycles") or 0),
                              _as_json(run.get("policy"), {}))
    conn.run(db._qualify(
        "update aihub.runs set candidate_digest = null, candidate_snapshot_id = null,"
        " check_receipt = null where id = :i"), i=run["id"])
    code_task = db._one(conn, db._qualify(
        "select * from aihub.run_tasks where run_id = :r and key = 'code'"), r=run["id"])
    if code_task and decision["action"] == "retry_coder":
        _restart_from_coder(conn, code_task, decision, feedback or {
            "role": "pipeline", "reason": reason,
        })
    else:
        _finish_run(conn, run["id"], "failed", "run_failed", reason)


def workspace_job(job_id: str) -> dict | None:
    from . import workspace
    return workspace.job_result(job_id)


def _start_candidate_snapshot(conn, run: dict) -> None:
    """После кодера хаб сам снимает digest дерева — кодер его не декларирует."""
    project_snapshot = _as_json(run.get("project_snapshot"), {}) or {}
    try:
        from . import workspace
        job = workspace.create_op_job(
            "snapshot",
            {"root": project_snapshot.get("project_workdir") or "",
             "label": f"candidate-{run['id'][:8]}"},
            project_snapshot.get("project_host_id") or "", run["project_id"],
            idempotency_key=f"run:{run['id']}:candidate:{run.get('cycles') or 0}")
    except Exception as exc:  # noqa: BLE001
        event(conn, run["id"], None, "candidate_snapshot_unavailable",
              {"reason": str(exc)[:300]})
        return
    pipeline = _as_json(run.get("pipeline"), {}) or {}
    pipeline["candidate_job_id"] = job["job"]["id"]
    conn.run(db._qualify(
        "update aihub.runs set pipeline = :p::jsonb, updated_at = now() where id = :i"),
        p=json.dumps(pipeline, ensure_ascii=False), i=run["id"])
    event(conn, run["id"], None, "candidate_snapshot_started",
          {"job_id": job["job"]["id"]})


def poll_active(resolve_agent, limit: int = 20) -> int:
    """Двигает активные запуски: закрывает завершившиеся задачи и выдаёт готовые."""
    moved = 0
    with db._lock:
        conn = db.connect()
        tasks = db._rows(conn, db._qualify(
            "select id, run_id, job_id from aihub.run_tasks"
            " where state = 'dispatched' and job_id is not null order by updated_at limit :l"),
            l=int(limit))
    for task in tasks:
        job = db.get_job(task["job_id"]) or {}
        status = job.get("status")
        if status not in ("succeeded", "failed", "cancelled", "timeout", "unknown"):
            continue
        on_task_result(task["job_id"], status == "succeeded",
                       (job.get("result_error") or "")[:400] or None)
        moved += 1
    with db._lock:
        conn = db.connect()
        runs = db._rows(conn, db._qualify(
            "select * from aihub.runs where state = 'active' order by updated_at limit :l"),
            l=int(limit))
    for run in runs:
        try:
            with db._lock:
                conn = db.connect()
                with db.transaction(conn):
                    fresh = db._one(conn, db._qualify(
                        "select * from aihub.runs where id = :i for update"), i=run["id"])
                    if not fresh or fresh["state"] != "active":
                        continue
                    over = _check_budgets(conn, fresh)
                    if over:
                        _finish_run(conn, fresh["id"], "failed", "budget_exceeded", over)
                        continue
                    _advance_pipeline(conn, fresh)
                    # Кодер закончил — запускаем программный снимок кандидата.
                    fresh = db._one(conn, db._qualify(
                        "select * from aihub.runs where id = :i for update"), i=run["id"])
                    if fresh and fresh["state"] == "active" \
                            and not fresh.get("candidate_digest") \
                            and not (_as_json(fresh.get("pipeline"), {}) or {}).get("candidate_job_id"):
                        code = db._one(conn, db._qualify(
                            "select state from aihub.run_tasks where run_id = :r and key = 'code'"),
                            r=run["id"])
                        if code and code["state"] == "done":
                            _start_candidate_snapshot(conn, fresh)
            result = dispatch(run["id"], resolve_agent)
            moved += len(result.get("dispatched") or [])
        except RunError:
            continue
    return moved


def run_state(run_id: str) -> dict | None:
    """Политика и счётчик циклов запуска — для правил перехода."""
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify(
            "select id, state, cycles, policy from aihub.runs where id = :i"), i=run_id)
    if not row:
        return None
    out = dict(row)
    out["policy"] = _as_json(out.get("policy"), {})
    return out


def retry_decision(role: str, cycles: int, policy: dict | None) -> dict:
    """FLOW-03: вернуть кодеру, пока не исчерпан лимит циклов. Решение без LLM."""
    policy = policy or {}
    limit = int(policy.get("max_cycles") or 0)
    if role not in ("tester", "reviewer"):
        return {"action": "fail", "reason": f"роль {role or 'неизвестная'} не возвращает задачу кодеру"}
    if limit <= 0:
        return {"action": "fail", "reason": "политика запуска не разрешает циклы исправлений"}
    if cycles >= limit:
        return {"action": "fail", "reason": f"лимит циклов исчерпан: {cycles} из {limit}; нужна эскалация"}
    return {"action": "retry_coder", "reason": f"цикл {cycles + 1} из {limit}",
            "next_cycle": cycles + 1}
