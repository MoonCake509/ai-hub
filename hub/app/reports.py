"""P-03: проекция событий AI Hub в живые отчёты проекта.

`ops/TASKFLOW.md` и `ops/pr/<id задачи>.md` собираются из фактических данных
прогона (run) и его задач: статус шага, роль, ветка, базовый и текущий SHA,
результат проверок, решение ревью. Один и тот же прогон всегда даёт один и тот
же текст, поэтому повтор обновляет запись, а не добавляет новую.

Отчёты пишет host worker: хаб не предполагает, что видит диск проекта.
"""
from __future__ import annotations

import json

from . import db, runs as runs_ctrl, workspace

STATUS_LABELS = {
    "active": "идёт",
    "paused": "пауза",
    "done": "завершён",
    "dispatched": "отправлена исполнителю",
    "waiting_context": "ждёт контекста",
    "queued": "в очереди",
    "running": "выполняется",
    "succeeded": "успех",
    "failed": "провал",
    "cancelled": "отменена",
    "blocked": "заблокирована",
    "skipped": "пропущена",
    "unknown": "неизвестно",
}


def _label(value: str | None) -> str:
    text = str(value or "")
    return STATUS_LABELS.get(text, text or "—")


def _short(sha: str | None) -> str:
    text = str(sha or "")
    return text[:12] if text else "—"


def build_taskflow(project: dict, run: dict) -> str:
    title = str(project.get("name") or "Project")
    tasks = run.get("tasks") or []
    lines = [
        f"# TASKFLOW — {title}",
        "",
        f"Прогон: `{run.get('id')}` · шаблон `{run.get('template_id')}` · состояние **{_label(run.get('state'))}**",
        "",
        f"Ветка: `{run.get('branch') or project.get('branch') or '—'}` · "
        f"база `{_short(run.get('base_sha') or project.get('head_sha'))}` · "
        f"кандидат `{_short(run.get('candidate_sha') or run.get('candidate_digest'))}`",
        "",
        "## Шаги",
        "",
        "| Шаг | Роль | Статус | SHA | Проверки |",
        "| --- | --- | --- | --- | --- |",
    ]
    for task in tasks:
        receipt = task.get("receipt") if isinstance(task.get("receipt"), dict) else {}
        checks = receipt.get("checks") or receipt.get("ok")
        if isinstance(checks, (list, tuple)):
            checks_text = f"{sum(1 for c in checks if (c or {}).get('ok'))}/{len(checks)}"
        elif checks is None:
            checks_text = "—"
        else:
            checks_text = "успех" if checks else "провал"
        lines.append(
            f"| {task.get('title') or task.get('key')} | {task.get('role')} | "
            f"{_label(task.get('status'))} | {_short(task.get('candidate_sha') or task.get('job_sha'))} | {checks_text} |")
    lines += ["", "## Сейчас", ""]
    current = next((t for t in tasks if t.get("status") in {"running", "queued"}), None)
    if current:
        lines.append(f"Шаг «{current.get('title') or current.get('key')}», роль {current.get('role')}, "
                     f"статус {_label(current.get('status'))}.")
    elif run.get("state") in {"done", "succeeded", "accepted"}:
        lines.append("Работа завершена и принята.")
    else:
        lines.append(f"Состояние прогона: {_label(run.get('state'))}.")
    lines.append("")
    return "\n".join(lines)


def build_pr_drafts(project: dict, run: dict) -> dict[str, str]:
    """Один черновик на задачу: база, кандидат, проверки, ревью."""
    drafts: dict[str, str] = {}
    for task in run.get("tasks") or []:
        key = str(task.get("key") or "")
        if not key:
            continue
        receipt = task.get("receipt") if isinstance(task.get("receipt"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        checks = receipt.get("checks") or []
        rows = []
        for check in checks if isinstance(checks, list) else []:
            if isinstance(check, dict):
                rows.append(f"| {check.get('name') or check.get('command') or '—'} | "
                            f"{'ok' if check.get('ok') else 'fail'} | {str(check.get('exit_code', ''))[:8]} |")
        body = [
            f"# {task.get('title') or key}",
            "",
            f"Прогон: `{run.get('id')}` · роль: {task.get('role')} · статус: {_label(task.get('status'))}",
            "",
            f"База: `{_short(run.get('base_sha') or project.get('head_sha'))}`  ",
            f"Кандидат: `{_short(task.get('candidate_sha') or result.get('sha') or run.get('candidate_sha'))}`  ",
            f"Ветка: `{run.get('branch') or project.get('branch') or '—'}`",
            "",
            "## Проверки",
            "",
        ]
        if rows:
            body += ["| Проверка | Итог | Код |", "| --- | --- | --- |"] + rows
        else:
            body.append("Проверки не зафиксированы.")
        body += ["", "## Изменения", ""]
        changed = result.get("changed_files") or receipt.get("write_set") or []
        if isinstance(changed, list) and changed:
            body += [f"- `{item}`" for item in changed[:50]]
        else:
            body.append("Список файлов не зафиксирован.")
        body += ["", "## Решение ревьюера", "",
                 str(receipt.get("review") or result.get("review") or "Не зафиксировано."), ""]
        drafts[f"ops/pr/{key}.md"] = "\n".join(body)
    return drafts


def sync_project(project_id: str, run_id: str | None = None, timeout_s: int = 120) -> dict:
    """Собрать отчёты по последнему прогону проекта и записать их на хост."""
    project = db.get_project(project_id)
    if not project:
        return {"ok": False, "error": "project not found"}
    prompts = runs_ctrl.list_runs(project_id, 1) if run_id is None else [{"id": run_id}]
    if not prompts:
        return {"ok": False, "error": "no runs"}
    run = runs_ctrl.get_run(str(prompts[0].get("id")))
    if not run:
        return {"ok": False, "error": "run not found"}
    files = {"ops/TASKFLOW.md": build_taskflow(project, run)}
    files.update(build_pr_drafts(project, run))
    reports = [{"path": path, "content": content} for path, content in sorted(files.items())]
    root = str(project.get("workdir") or "").strip()
    if not root:
        return {"ok": False, "error": "project has no workdir"}
    job = workspace.create_op_job(
        "write_reports", {"root": root, "reports": reports},
        str(project.get("host_id") or "laptop"), project_id,
        f"reports:{run.get('id')}:{hashlib_digest(reports)}", timeout_s)
    return {"ok": True, "job": job["job"]["id"], "run": run.get("id"), "files": sorted(files)}


def hashlib_digest(reports: list[dict]) -> str:
    import hashlib
    payload = json.dumps(reports, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def sync_quiet(project_id: str) -> None:
    """Обновление после события прогона: ошибка отчёта не должна ломать сам прогон."""
    try:
        sync_project(project_id)
    except Exception:  # noqa: BLE001
        pass