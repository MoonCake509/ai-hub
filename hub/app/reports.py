"""P-03: проекция событий AI Hub в живые отчёты проекта.

`ops/TASKFLOW.md` и `ops/pr/<id задачи>.md` собираются из фактических данных
прогона (run) и его задач: статус шага, роль, ветка, базовый и текущий SHA,
результат проверок, решение ревью. Один и тот же прогон всегда даёт один и тот
же текст, поэтому повтор обновляет запись, а не добавляет новую.

Отчёты пишет host worker: хаб не предполагает, что видит диск проекта.
"""
from __future__ import annotations

import json
import time

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


def _task_receipt(run: dict, task: dict) -> dict:
    """Receipt проверок: у шага test он общий на прогон (receipt runner'а)."""
    receipt = run.get("check_receipt") if str(task.get("key")) == "test" else None
    if not isinstance(receipt, dict):
        receipt = task.get("receipt") if isinstance(task.get("receipt"), dict) else {}
    return receipt


def _check_rows(receipt: dict) -> list[str]:
    rows: list[str] = []
    checks = receipt.get("checks") if isinstance(receipt, dict) else None
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        ok = check.get("passed", check.get("ok"))
        cmd = check.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        rows.append(f"| {check.get('name') or '—'} | {'ok' if ok else 'fail'} | "
                    f"{check.get('exit_code', '—')} | `{str(cmd)[:90]}` |")
    return rows


def build_taskflow(project: dict, run: dict) -> str:
    title = str(project.get("name") or "Project")
    tasks = run.get("tasks") or []
    receipt = run.get("check_receipt") if isinstance(run.get("check_receipt"), dict) else {}
    lines = [
        f"# TASKFLOW — {title}",
        "",
        f"Прогон: `{run.get('id')}` · шаблон `{run.get('template_id')}` · состояние **{_label(run.get('state'))}**",
        "",
        f"Ветка: `{run.get('branch') or project.get('branch') or '—'}` · "
        f"база `{_short(project.get('head_sha'))}` · "
        f"кандидат `{_short(run.get('candidate_digest'))}`",
        "",
        "## Шаги",
        "",
        "| Шаг | Роль | Статус | Задание | Проверки |",
        "| --- | --- | --- | --- | --- |",
    ]
    for task in tasks:
        task_receipt = _task_receipt(run, task)
        rows = _check_rows(task_receipt)
        if rows:
            ok = sum(1 for line in rows if "| ok |" in line)
            checks_text = f"{ok}/{len(rows)}"
        else:
            checks_text = "—"
        lines.append(
            f"| {task.get('key')} | {task.get('role')} | {_label(task.get('state'))} | "
            f"`{_short(task.get('job_id'))}` | {checks_text} |")
    if receipt:
        lines += ["", "## Проверки изолированного runner'а", ""]
        lines += ["| Проверка | Итог | Код | Команда |", "| --- | --- | --- | --- |"]
        lines += _check_rows(receipt)
        isolation = receipt.get("isolation") if isinstance(receipt.get("isolation"), dict) else {}
        if isolation:
            flags = ", ".join(f"{k}={v}" for k, v in sorted(isolation.items()))
            lines += ["", f"Изоляция: {flags}."]
        if receipt.get("snapshot_id"):
            lines += ["", f"Снимок: `{receipt.get('snapshot_id')}`."]
    review = next((t for t in tasks if str(t.get("key")) == "review"), None)
    if review and isinstance(review.get("result"), dict):
        verdict = review["result"].get("verdict") or "—"
        findings = review["result"].get("findings") or []
        lines += ["", "## Решение ревьюера", "", f"Вердикт: **{verdict}** ({len(findings)} замечаний)."]
        for item in findings[:10] if isinstance(findings, list) else []:
            if isinstance(item, dict):
                lines.append(f"- [{item.get('severity') or 'info'}] {str(item.get('issue'))[:200]}")
    lines += ["", "## Сейчас", ""]
    current = next((t for t in tasks if t.get("state") in {"running", "dispatched", "waiting_context"}), None)
    if current:
        lines.append(f"Шаг «{current.get('key')}», роль {current.get('role')}, "
                     f"статус {_label(current.get('state'))}.")
    elif run.get("state") in {"done", "succeeded", "accepted"}:
        lines.append("Работа завершена и принята.")
    else:
        lines.append(f"Состояние прогона: {_label(run.get('state'))}.")
    lines.append("")
    return "\n".join(lines)


def build_end(project: dict, run: dict) -> str:
    """Итог последней принятой работы: версия, ветка, слияние, шаги."""
    tasks = run.get("tasks") or []
    merged = str(run.get("merged_sha") or "").strip()
    candidate = str(run.get("candidate_sha") or "").strip()
    version = merged or candidate or _short(project.get("head_sha"))
    lines = [
        f"# Итог последней работы — {project.get('name') or project.get('id')}",
        "",
        f"Версия: `{version}`  ",
        f"Ветка: `{run.get('task_branch') or run.get('branch') or project.get('branch') or 'main'}` · "
        f"состояние: {_label(run.get('state'))} · слияние: {run.get('merge_state') or '—'}",
        "",
        "## Что принято",
        "",
        str(run.get("goal") or ""),
        "",
        "## Шаги",
        "",
        "| Шаг | Роль | Статус |",
        "| --- | --- | --- |",
    ]
    for task in tasks:
        lines.append(f"| `{task.get('key')}` | {task.get('role')} | {_label(task.get('state'))} |")
    receipt = run.get("check_receipt") if isinstance(run.get("check_receipt"), dict) else {}
    rows = _check_rows(receipt)
    lines += ["", "## Проверки", ""]
    if rows:
        lines += ["| Проверка | Итог | Код | Команда |", "| --- | --- | --- | --- |"] + rows
    else:
        lines.append("Проверки не зафиксированы.")
    lines += ["", "## Слияние", ""]
    if merged:
        lines.append(f"Проверенный коммит `{merged}` слит в основную ветку fast-forward.")
    elif run.get("merge_state"):
        lines.append(f"Слияние не выполнено: {run.get('merge_state')}.")
    else:
        lines.append("Слияние ещё не выполнялось.")
    lines += ["", f"Артефакты запуска: `ops/ARTIFACTS.md` (прогон `{run.get('id')}`).", ""]
    return "\n".join(lines)


def build_pr_drafts(project: dict, run: dict) -> dict[str, str]:
    """Один черновик на шаг: роль, задание, проверки, изменения, ревью."""
    drafts: dict[str, str] = {}
    tasks = run.get("tasks") or []
    review = next((t for t in tasks if str(t.get("key")) == "review"), None)
    for task in tasks:
        key = str(task.get("key") or "")
        if not key:
            continue
        receipt = _task_receipt(run, task)
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        body = [
            f"# {key}",
            "",
            f"Прогон: `{run.get('id')}` · роль: {task.get('role')} · статус: {_label(task.get('state'))}",
            "",
            f"База: `{_short(project.get('head_sha'))}`  ",
            f"Кандидат: `{_short(run.get('candidate_digest'))}`  ",
            f"Ветка: `{run.get('branch') or project.get('branch') or '—'}`",
            "",
            "## Проверки",
            "",
        ]
        rows = _check_rows(receipt) or _check_rows(result)
        if rows:
            body += ["| Проверка | Итог | Код | Команда |", "| --- | --- | --- | --- |"] + rows
        else:
            body.append("Проверки не зафиксированы.")
        body += ["", "## Изменения", ""]
        changed = result.get("files") or result.get("changed_files") or receipt.get("write_set") or []
        if isinstance(changed, list) and changed:
            body += [f"- `{item}`" for item in changed[:50]]
        else:
            body.append("Список файлов не зафиксирован.")
        body += ["", "## Решение ревьюера", ""]
        if key == "review" and result.get("verdict"):
            body.append(f"Вердикт: **{result['verdict']}**")
            for item in (result.get("findings") or [])[:10]:
                if isinstance(item, dict):
                    body.append(f"- [{item.get('severity') or 'info'}] {str(item.get('issue'))[:300]}")
        elif isinstance(review, dict) and isinstance(review.get("result"), dict):
            body.append(f"Вердикт ревьюера по тому же кандидату: **{review['result'].get('verdict') or '—'}**")
        else:
            body.append("Ревью ещё не проводилось.")
        body.append("")
        drafts[f"ops/pr/{key}.md"] = "\n".join(body)
    return drafts


def build_environments(project: dict, run: dict) -> str:
    """Среды и рабочие копии — только подтверждённые факты прогона."""
    receipt = run.get("check_receipt") if isinstance(run.get("check_receipt"), dict) else {}
    isolation = receipt.get("isolation") if isinstance(receipt.get("isolation"), dict) else {}
    rows = [
        ("рабочая копия", str(project.get("host_id") or "—"),
         f"`{project.get('workdir') or '—'}`", "карточка проекта"),
        ("git-репозиторий", str(project.get("host_id") or "—"),
         f"`{project.get('git_root') or project.get('workdir') or '—'}`", "подготовка проекта"),
        ("ветка / коммит", str(project.get("host_id") or "—"),
         f"`{project.get('branch') or '—'}` / `{_short(project.get('index_sha') or project.get('head_sha'))}`",
         "карта проекта"),
    ]
    if isolation:
        rows.append(("проверки", str(project.get("host_id") or "—"),
                     f"контейнер `{str(isolation.get('image') or '—')[:19]}…`, сеть `{isolation.get('network')}`",
                     "receipt runner'а"))
    if receipt.get("snapshot_id"):
        rows.append(("снимок прогона", str(project.get("host_id") or "—"),
                     f"`{receipt.get('snapshot_id')}`", "receipt runner'а"))
    lines = [f"# Среды — {project.get('name') or 'Project'}", "",
             "| Среда | Хост | Путь / версия | Источник сведений | Проверено |",
             "| --- | --- | --- | --- | --- |"]
    for name, host, where, source in rows:
        lines.append(f"| {name} | {host} | {where} | {source} | {time.strftime('%Y-%m-%d')} |")
    lines += ["", "Сведения подтверждены записями AI Hub; неизвестное не додумывается.", ""]
    return "\n".join(lines)


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
    files = {"ops/TASKFLOW.md": build_taskflow(project, run),
             "ops/ENVIRONMENTS.md": build_environments(project, run),
             "ops/END.md": build_end(project, run)}
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