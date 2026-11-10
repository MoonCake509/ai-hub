"""FLOW-01: durable контроллер запусков.

Запуск — это дерево задач с зависимостями и журналом событий. Диспетчер
выдаёт только готовые задачи, никогда не создаёт второе исполнение по тому же
ключу и переживает перезапуск хаба: состояние живёт в базе, а не в памяти.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import db, templates

RUN_STATES = ("active", "paused", "cancelled", "done", "failed")
TASK_STATES = ("pending", "dispatched", "running", "done", "failed", "skipped", "cancelled")


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


def create_run(project_id: str, template: dict, policy: dict | None = None,
               actor: str = "hub-token-holder") -> dict:
    steps = validate_template(template)
    template_id = str(template.get("id") or "default")
    version = int(template.get("version") or 1)
    run_id = uuid.uuid4().hex
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "insert into aihub.runs(id, project_id, template_id, template_version, policy, state,"
            " created_at, updated_at) values (:i, :p, :t, :v, :pol::jsonb, 'active', now(), now())"
            " returning id"),
            i=run_id, p=project_id, t=template_id, v=version,
            pol=json.dumps(policy or {}, ensure_ascii=False))
        for index, step in enumerate(steps):
            db._one(conn, db._qualify(
                "insert into aihub.run_tasks(id, run_id, key, role, agent, prompt, depends_on,"
                " position, state, attempt, created_at, updated_at)"
                " values (:i, :r, :k, :role, :ag, :pr, :d, :pos, 'pending', 0, now(), now())"
                " returning id"),
                i=uuid.uuid4().hex, r=run_id, k=step["key"], role=step.get("role") or "",
                ag=step.get("agent") or "", pr=step.get("prompt") or "",
                d=json.dumps(list(step.get("depends_on") or []), ensure_ascii=False), pos=index)
        event(conn, run_id, None, "run_created", {"template": template_id, "version": version,
                                                  "steps": len(steps), "actor": actor})
    return {"run_id": run_id, "state": "active", "template_id": template_id,
            "template_version": version, "steps": len(steps)}


def event(conn, run_id: str, task_id: str | None, name: str, payload: dict | None = None) -> None:
    conn.run(db._qualify(
        "insert into aihub.run_events(run_id, task_id, event, payload)"
        " values (:r, :t, :e, :p::jsonb)"),
        r=run_id, t=task_id, e=name,
        p=json.dumps(payload or {}, ensure_ascii=False))

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
    out = dict(run)
    for key in ("created_at", "updated_at", "finished_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    out["tasks"] = []
    for row in tasks:
        item = dict(row)
        for key in ("created_at", "updated_at", "finished_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat()
        out["tasks"].append(item)
    out["events"] = []
    for row in events:
        item = dict(row)
        if item.get("created_at") is not None:
            item["created_at"] = item["created_at"].isoformat()
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
        deps = task.get("depends_on") or []
        if isinstance(deps, str):
            deps = json.loads(deps or "[]")
        if all((by_key.get(dep) or {}).get("state") == "done" for dep in deps):
            ready.append(task)
    return ready


def dispatch(run_id: str, resolve_agent, max_queue: int = 50) -> dict:
    """Выдаёт готовые задачи в очередь. Идемпотентно по operation key."""
    created = []
    with db._lock:
        conn = db.connect()
        run = db._one(conn, db._qualify("select * from aihub.runs where id = :i for update"),
                      i=run_id)
        if not run:
            raise RunError("not_found", "запуск не найден")
        if run["state"] != "active":
            return {"run_id": run_id, "dispatched": [], "state": run["state"],
                    "reason": "запуск не активен"}
        for task in _ready(conn, run_id):
            agent_id = task.get("agent") or ""
            bridge_agent, error = resolve_agent(agent_id)
            if not bridge_agent:
                event(conn, run_id, task["id"], "task_blocked", {"reason": error or "агент не найден"})
                continue
            attempt = int(task["attempt"] or 0) + 1
            key = f"run:{run_id}:{task['key']}:{attempt}"
            prompt = task.get("prompt") or task["key"]
            context_spec = task.get("context") or {}
            if isinstance(context_spec, str):
                context_spec = json.loads(context_spec or "{}")
            if context_spec:
                project = db.get_project(run["project_id"]) or {}
                ctx_id = None
                try:
                    entry_path = context_spec.get("entry") or ""
                    contracts = [c for c in (context_spec.get("contracts") or []) if c]
                    if entry_path:
                        ctx = packets.create_context(
                            project, context_spec.get("goal") or "", entry_path, contracts,
                            int(context_spec.get("budget_tokens") or 2000))
                        ctx_id = ctx["packet_id"]
                    elif context_spec.get("paths"):
                        ctx = packets.start(project, list(context_spec["paths"]),
                                            int(context_spec.get("budget_tokens") or 2000) * 4)
                        ctx_id = ctx["packet_id"]
                except Exception as exc:  # noqa: BLE001
                    event(conn, run_id, task["id"], "context_failed",
                          {"reason": str(exc)[:200]})
                if ctx_id:
                    conn.run(db._qualify(
                        "update aihub.run_tasks set context_id = :c where id = :i"),
                        c=ctx_id, i=task["id"])
                    prompt = prompt + chr(10) + chr(10) + "Вход роли: пакет " + ctx_id + "."

            sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            job, used = db.create_job(bridge_agent, prompt, sha, 900, key, max_queue,
                                      None, None)
            conn.run(db._qualify(
                "update aihub.run_tasks set state = 'dispatched', attempt = :a, job_id = :j,"
                " updated_at = now() where id = :i"),
                a=attempt, j=job["id"], i=task["id"])
            event(conn, run_id, task["id"], "task_dispatched",
                  {"job_id": job["id"], "attempt": attempt, "agent": agent_id})
            created.append({"task": task["key"], "job_id": job["id"], "attempt": attempt,
                            "created": used})
    return {"run_id": run_id, "dispatched": created, "state": "active"}


def set_state(run_id: str, action: str, actor: str = "hub-token-holder") -> dict:
    target = {"pause": "paused", "resume": "active", "cancel": "cancelled"}.get(action)
    if not target:
        raise RunError("bad_action", f"неизвестное действие: {action}")
    with db._lock:
        conn = db.connect()
        run = db._one(conn, db._qualify("select state from aihub.runs where id = :i for update"),
                      i=run_id)
        if not run:
            raise RunError("not_found", "запуск не найден")
        if run["state"] == "cancelled" and action != "cancel":
            raise RunError("cancelled", "запуск отменён")
        conn.run(db._qualify(
            "update aihub.runs set state = :s, updated_at = now(),"
            " finished_at = case when :s = 'cancelled' then now() else finished_at end"
            " where id = :i"), s=target, i=run_id)
        if action == "cancel":
            conn.run(db._qualify(
                "update aihub.run_tasks set state = 'cancelled', updated_at = now()"
                " where run_id = :r and state in ('pending', 'dispatched')"), r=run_id)
        event(conn, run_id, None, f"run_{action}", {"actor": actor})
    return {"run_id": run_id, "state": target}


def on_task_result(job_id: str, ok: bool, error: str | None = None) -> dict | None:
    """Закрывает задачу по результату её job и двигает запуск дальше."""
    with db._lock:
        conn = db.connect()
        task = db._one(conn, db._qualify(
            "select * from aihub.run_tasks where job_id = :j for update"), j=job_id)
        if not task:
            return None
        state = "done" if ok else "failed"
        error = error
        job = db.get_job(job_id) or {}
        output = job.get("result_output") or ""
        role = task.get("role") or ""
        if ok and role in templates.ROLES:
            valid, _parsed, reason = templates.validate_output(role, output)
            if not valid:
                state, error = "failed", reason
        conn.run(db._qualify(
            "update aihub.run_tasks set state = :s, error = :e, updated_at = now(),"
            " finished_at = now() where id = :i"), s=state, e=error, i=task["id"])
        event(conn, task["run_id"], task["id"], f"task_{state}", {"error": error})
        remaining = db._rows(conn, db._qualify(
            "select state from aihub.run_tasks where run_id = :r"), r=task["run_id"])
        states = {row["state"] for row in remaining}
        if states <= {"done", "skipped"}:
            conn.run(db._qualify(
                "update aihub.runs set state = 'done', updated_at = now(), finished_at = now()"
                " where id = :i"), i=task["run_id"])
            event(conn, task["run_id"], None, "run_done", {})
        elif "failed" in states and states <= {"done", "failed", "skipped"}:
            conn.run(db._qualify(
                "update aihub.runs set state = 'failed', updated_at = now(), finished_at = now()"
                " where id = :i"), i=task["run_id"])
            event(conn, task["run_id"], None, "run_failed", {})
    return {"run_id": task["run_id"], "task": task["key"], "state": state}


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
            "select id from aihub.runs where state = 'active' order by updated_at limit :l"),
            l=int(limit))
    for run in runs:
        try:
            result = dispatch(run["id"], resolve_agent)
            moved += len(result.get("dispatched") or [])
        except RunError:
            continue
    return moved
