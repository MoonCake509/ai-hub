"""Versioned, deterministic quality checks for explicitly selected AI Hub channels."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SUITE_ID = "aihub-channel-smoke"
SUITE_VERSION = "1.0.0"
GRADER_VERSION = "deterministic-v1"

# Prompts and checks are deliberately small, synthetic and non-executable.
_CASES: list[dict[str, Any]] = [
    {
        "id": "exact-instruction",
        "title": "Точное следование инструкции",
        "category": "instruction",
        "description": "Проверяет короткий точный ответ без добавочного текста.",
        "prompt": "Reply with exactly this token and nothing else: ROUTE-OK",
        "check": {"type": "exact", "value": "ROUTE-OK"},
    },
    {
        "id": "json-shape",
        "title": "Структура JSON",
        "category": "format",
        "description": "Проверяет валидность JSON, набор ключей и значения полей.",
        "prompt": "Return only a JSON object with exactly these keys: status, count. Set status to ready and count to 3.",
        "check": {"type": "json-object", "keys": ["status", "count"], "equals": {"status": "ready", "count": 3}},
    },
    {
        "id": "basic-math",
        "title": "Базовая арифметика",
        "category": "reasoning",
        "description": "Проверяет один небольшой расчёт с ответом только числом.",
        "prompt": "A box has 12 tokens. Remove 5, then add 7. Reply with only the final number.",
        "check": {"type": "integer", "value": 14},
    },
    {
        "id": "synthetic-context",
        "title": "Ответ по контексту",
        "category": "retrieval",
        "description": "Проверяет извлечение метки из выданного синтетического контекста.",
        "prompt": "Reference note: The inspection marker is MINT-48A. What is the inspection marker? Reply with the marker only.",
        "check": {"type": "exact", "value": "MINT-48A"},
    },
]


def channel_id(agent_id: str, profile: str | None, model: str | None) -> str:
    canonical = json.dumps(
        {"agent_id": agent_id, "profile": profile or None, "model": model or None},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def get_suite(suite_id: str = SUITE_ID, version: str = SUITE_VERSION) -> dict[str, Any] | None:
    if suite_id != SUITE_ID or version != SUITE_VERSION:
        return None
    return {
        "id": SUITE_ID,
        "version": SUITE_VERSION,
        "grader_version": GRADER_VERSION,
        "title": "Проверка качества канала",
        "description": "Четыре базовые детерминированные проверки на синтетических данных.",
        "cases": [dict(case) for case in _CASES],
    }


def list_suites() -> list[dict[str, Any]]:
    suite = get_suite()
    assert suite is not None
    return [{k: v for k, v in suite.items() if k != "cases"} | {
        "cases": [{k: case[k] for k in ("id", "title", "category", "description")} for case in _CASES]
    }]


def select_cases(case_count: int, case_ids: list[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(case_count, int) or isinstance(case_count, bool) or not 1 <= case_count <= len(_CASES):
        raise ValueError(f"case_count must be between 1 and {len(_CASES)}")
    if case_ids is None:
        selected = _CASES[:case_count]
    else:
        if not isinstance(case_ids, list) or len(case_ids) != case_count or len(set(case_ids)) != len(case_ids):
            raise ValueError("case_ids must contain exactly case_count unique case ids")
        known = {case["id"]: case for case in _CASES}
        if any(case_id not in known for case_id in case_ids):
            raise ValueError("case_ids contains an unknown case")
        # Preserve the explicit caller order.
        selected = [known[case_id] for case_id in case_ids]
    return [json.loads(json.dumps(case, ensure_ascii=False)) for case in selected]


def _normalize_ocr(value: str) -> str:
    """Compare OCR output by the characters it actually produced."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    return match.group(1).strip() if match else value


def grade(case_snapshot: dict[str, Any], response: str | None) -> dict[str, Any]:
    """Grade only fixed data checks; never evaluates response text as code."""
    text = response if isinstance(response, str) else ""
    check = case_snapshot.get("check") or {}
    kind = check.get("type")
    if kind == "exact":
        expected = str(check.get("value", ""))
        passed = text.strip() == expected
        return {"passed": passed, "reason": "Точное совпадение." if passed else "Ответ отличается от ожидаемой метки или содержит лишний текст."}
    if kind == "integer":
        expected = check.get("value")
        match = re.fullmatch(r"\s*([+-]?\d+)\s*", text)
        actual = int(match.group(1)) if match else None
        passed = actual == expected
        return {"passed": passed, "reason": "Числовой ответ совпал." if passed else "Нужен только целый ответ; получено другое значение."}
    if kind == "ocr-normalized":
        expected = _normalize_ocr(str(check.get("value", "")))
        actual = _normalize_ocr(text)
        passed = bool(expected) and actual == expected
        return {"passed": passed,
                "reason": ("Распознанный текст совпал после нормализации символов." if passed
                           else "Распознанный текст не совпал с ожидаемым.")}
    if kind == "json-object":
        try:
            value = json.loads(_strip_json_fence(text))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"passed": False, "reason": "Ответ не является корректным JSON."}
        if not isinstance(value, dict):
            return {"passed": False, "reason": "Верхний уровень JSON должен быть объектом."}
        expected_keys = set(check.get("keys") or [])
        if set(value) != expected_keys:
            return {"passed": False, "reason": "Набор ключей JSON не совпал с ожидаемым."}
        expected = check.get("equals") or {}
        if any(type(value.get(key)) is not type(wanted) or value.get(key) != wanted
               for key, wanted in expected.items()):
            return {"passed": False, "reason": "Значения JSON не совпали с ожидаемыми."}
        return {"passed": True, "reason": "JSON валиден; ключи и значения совпали."}
    return {"passed": False, "reason": "Для этого case_id нет поддерживаемого детерминированного grader."}

TERMINAL_JOB_STATES = {"succeeded", "failed", "timeout", "cancelled", "unknown"}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
IDENTITY_NOTE = "Reported model metadata is an adapter/provider observation, not proof of model identity."


def channel_state(agent_id: str, profile: str | None, model: str | None) -> dict[str, Any]:
    """Return the manual state for a normalized channel; absent rows mean enabled."""
    from . import quality_db
    key = channel_id(agent_id, profile, model)
    state = quality_db.get_channel(key)
    return {"channel_id": key, "enabled": True, "reason": "", **(state or {})}


def ensure_channel_enabled(agent_id: str, profile: str | None, model: str | None) -> None:
    state = channel_state(agent_id, profile, model)
    if not state.get("enabled", True):
        reason = state.get("reason") or "оператор отключил этот канал"
        raise ValueError(f"канал отключён для новых заданий: {reason}")


def channel_catalog(profile_map: dict[str, dict], agent_items: list[dict]) -> list[dict[str, Any]]:
    from . import quality_db
    overrides = {row["channel_id"]: row for row in quality_db.list_channels()}
    audit = quality_db.channel_audit(200)
    latest_audit: dict[str, dict[str, Any]] = {}
    for record in audit:
        latest_audit.setdefault(record["channel_id"], record)
    agent_names = {agent.get("id"): agent.get("name") or agent.get("id") for agent in agent_items}
    rows: list[dict[str, Any]] = []
    seen_channel_ids: set[str] = set()
    for agent_id, catalog in profile_map.items():
        agent = next((item for item in agent_items if item.get("id") == agent_id), {})
        if not agent.get("model_selection"):
            continue
        profiles = catalog.get("profiles") or []
        for provider in profiles:
            provider_id = provider.get("id")
            models = provider.get("models") or [None]
            for model in models:
                model_id = model if isinstance(model, str) or model is None else model.get("id")
                model_name = model if isinstance(model, str) or model is None else model.get("name") or model_id
                model_meta = (model if isinstance(model, dict)
                              else ((provider.get("model_capabilities") or {}).get(model_id)
                                    or (agent.get("model_capabilities") or {}).get(model_id)
                                    or {}))
                required_modalities = (model_meta.get("required_modalities") or provider.get("required_modalities")
                                       or agent.get("required_modalities") or [])
                requires_image = bool(model_meta.get("requires_image") or provider.get("requires_image")
                                      or agent.get("requires_image") or "image" in required_modalities)
                key = channel_id(agent_id, provider_id, model_id)
                if key in seen_channel_ids:
                    continue
                seen_channel_ids.add(key)
                state = overrides.get(key) or {}
                row = {
                    "channel_id": key,
                    "agent_id": agent_id,
                    "agent_name": agent_names.get(agent_id, agent_id),
                    "profile": provider_id,
                    "profile_name": provider.get("name") or provider_id,
                    "model": model_id,
                    "model_name": model_name,
                    "required_modalities": list(required_modalities),
                    "requires_image": requires_image,
                    "enabled": state.get("enabled", True),
                    "reason": state.get("reason") or "",
                    "updated_at": state.get("updated_at"),
                    "audit": latest_audit.get(key),
                }
                rows.append(row)
    return sorted(rows, key=lambda row: (row["agent_id"], row["profile"] or "", row["model"] or ""))


def _as_datetime(value):
    from datetime import datetime
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _elapsed(start, end):
    from datetime import datetime, timezone
    a = _as_datetime(start)
    b = _as_datetime(end) or datetime.now(timezone.utc)
    if a is None:
        return None
    try:
        return round(max(0.0, (b - a).total_seconds()), 3)
    except TypeError:
        return None


def _sum_attempt_usage(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    reported: list[dict[str, Any] | None] = []
    for attempt in attempts:
        usage = attempt.get("usage")
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except ValueError:
                usage = None
        reported.append(usage if isinstance(usage, dict) else None)
    if not reported:
        return None
    result = {}
    for key in USAGE_FIELDS:
        values = [entry.get(key) if entry else None for entry in reported]
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            result[key] = sum(values)
    return result or None


def _refresh_item(run_id: str, item: dict[str, Any]) -> dict[str, Any]:
    from . import db, quality_db
    from .quality_quick import SKIPPED_STATES
    if item.get("status") in SKIPPED_STATES:
        # A probe that never ran keeps its reason; grading it would turn a
        # partial result into a fake failure.
        return item
    job_id = item.get("job_id")
    if not job_id:
        return item
    job = db.get_job(job_id)
    if not job:
        return item
    attempts = job.get("attempts_detail") or []
    starts = [a.get("started_at") for a in attempts if a.get("started_at")]
    first_start = min(starts, key=lambda value: (_as_datetime(value).timestamp() if _as_datetime(value) else 0)) if starts else None
    queue_wait = _elapsed(job.get("created_at"), first_start) if first_start else None
    # A queued cancellation has a real queue duration but no executor duration.
    if not starts and job.get("status") == "cancelled":
        queue_wait = _elapsed(job.get("created_at"), job.get("finished_at"))
    execution_parts = []
    for attempt in attempts:
        duration = _elapsed(attempt.get("started_at"), attempt.get("finished_at"))
        if duration is not None:
            execution_parts.append(duration)
    execution = round(sum(execution_parts), 3) if execution_parts else None
    wall = _elapsed(job.get("created_at"), job.get("finished_at")) if job.get("finished_at") else None
    usage = _sum_attempt_usage(attempts)
    state = job.get("status") or "unknown"
    response = job.get("result_output")
    error = job.get("result_error")
    passed = None
    reason = None
    if state == "succeeded":
        result = grade(item.get("case_snapshot") or {}, response)
        passed, reason = result["passed"], result["reason"]
    elif state in TERMINAL_JOB_STATES:
        passed = False
        reason = ("Исполнение завершилось без grader: " + (error or state))
    reported = job.get("effective_model")
    updated = {
        "status": state,
        "job_status": state,
        "passed": passed,
        "grader_reason": reason,
        "raw_response": response,
        "reported_model": reported,
        "identity_evidence_source": "adapter_usage.model" if reported else "not_reported",
        "error": error,
        "first_started_at": first_start,
        "queue_wait_seconds": queue_wait,
        "execution_seconds": execution,
        "wall_seconds": wall,
        "usage": usage,
        # No applicable versioned pricing or transport TTFT data is configured yet.
        "cost": None,
        "first_token_latency_seconds": None,
        "finished_at": job.get("finished_at") if state in TERMINAL_JOB_STATES else None,
    }
    quality_db.update_item(item["id"], updated)
    item.update(updated)
    return item


def _aggregate(run: dict[str, Any]) -> None:
    from . import quality_db
    items = run.get("items") or []
    states = [item.get("status") for item in items]
    finished = bool(items) and all(state in TERMINAL_JOB_STATES or state == "failed" for state in states)
    if run.get("error"):
        status = "failed"
    elif finished:
        status = "completed"
    elif run.get("budget_exhausted"):
        # All work accepted before the cap has been observed. Probes that never
        # ran are explicit partial results, not permanently running jobs.
        status = "completed"
    elif any(state == "running" for state in states):
        status = "running"
    elif any(state in ("queued", "pending", "starting") for state in states):
        status = "queued"
    else:
        status = "starting"
    passed_count = sum(item.get("passed") is True for item in items)
    failed_count = sum(item.get("passed") is False for item in items)
    score = round(100.0 * passed_count / len(items), 2) if finished and items else None
    queue_values = [item["queue_wait_seconds"] for item in items if isinstance(item.get("queue_wait_seconds"), (int, float))]
    execution_values = [item["execution_seconds"] for item in items if isinstance(item.get("execution_seconds"), (int, float))]
    all_starts = []
    all_finishes = []
    item_usages: list[dict[str, Any] | None] = []
    for item in items:
        item_usages.append(item.get("usage") if isinstance(item.get("usage"), dict) else None)
        if item.get("first_started_at"):
            all_starts.append(item.get("first_started_at"))
        if item.get("status") in TERMINAL_JOB_STATES:
            all_finishes.append(item.get("finished_at"))
    # Queue wait, execution, and wall time are kept as separate measurements.
    created = _as_datetime(run.get("created_at"))
    latest_finish = max((_as_datetime(value) for value in all_finishes if _as_datetime(value)), default=None)
    from datetime import timezone
    now = __import__("datetime").datetime.now(timezone.utc)
    wall_end = latest_finish if finished else now
    wall = round(max(0, (wall_end - created).total_seconds()), 3) if created else None
    first_started = min((_as_datetime(value) for value in all_starts if _as_datetime(value)), default=None)
    total_usage: dict[str, float] = {}
    if items:
        for key in USAGE_FIELDS:
            values = [usage.get(key) if usage else None for usage in item_usages]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
                total_usage[key] = sum(values)
    quality_db.update_run_aggregate(run["id"], {
        "status": status,
        "started_at": first_started,
        "finished_at": latest_finish if finished else None,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "score": score,
        "queue_wait_seconds": round(sum(queue_values) / len(queue_values), 3) if queue_values else None,
        "execution_seconds": round(sum(execution_values), 3) if execution_values else None,
        "wall_seconds": wall,
        "total_usage": total_usage or None,
        "error": run.get("error"),
    })


def _apply_quick_budget(run: dict[str, Any]) -> dict[str, Any]:
    """Stop accepting/awaiting work once a quick run's wall cap has passed."""
    from . import db, quality_db, quality_quick
    if run.get("mode") != "quick" or not isinstance(run.get("budget_s"), int):
        return run
    created = _as_datetime(run.get("created_at"))
    if not created:
        return run
    from datetime import datetime, timezone
    elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    if not quality_quick.budget_exhausted(run, elapsed):
        return run
    reason = quality_quick.skip_reason(run)
    for item in run.get("items") or []:
        if item.get("status") not in quality_quick.PENDING_STATES:
            continue
        # A queued job is safe to withdraw before it gets a lease. A running
        # job is not silently called stopped; the existing lease/timeout path
        # remains its final authority, while this probe is reported partial.
        if item.get("job_id") and item.get("status") in ("queued", "pending", "starting"):
            db.cancel_job(item["job_id"])
        quality_db.mark_item_skipped(item["id"], "budget_exhausted", reason)
    quality_db.mark_budget_exhausted(run["id"], reason)
    return quality_db.get_run(run["id"]) or run


def refresh_run(run_id: str) -> dict[str, Any] | None:
    from . import quality_db
    run = quality_db.get_run(run_id)
    if not run:
        return None
    run = _apply_quick_budget(run)
    snapshot = quality_db.get_run(run_id)
    if snapshot:
        cases = (snapshot.get("suite_snapshot") or {}).get("cases") or []
        for item in snapshot.get("items") or []:
            case = next((row for row in cases if row.get("id") == item.get("case_id")), None)
            if case and "fixture" in case:
                item.setdefault("fixture", case["fixture"])
        run = snapshot
    for item in run.get("items") or []:
        _refresh_item(run_id, item)
    run = quality_db.get_run(run_id)
    if run:
        _aggregate(run)
        run = quality_db.get_run(run_id)
    return run


def refresh_job(job_id: str) -> None:
    from . import quality_db
    item = quality_db.item_by_job(job_id)
    if item:
        run_id = item["run_id"]
        run = quality_db.get_run(run_id)
        if run:
            _refresh_item(run_id, item)
            _aggregate(quality_db.get_run(run_id) or run)


def refresh_active_runs() -> None:
    from . import quality_db
    for run_id in quality_db.list_run_ids():
        refresh_run(run_id)


def public_run(run: dict[str, Any], include_items: bool = True) -> dict[str, Any]:
    result = {key: run.get(key) for key in (
        "id", "suite_id", "suite_version", "grader_version", "channel_id", "agent_id",
        "profile", "model", "case_count", "timeout_s", "status", "created_at", "started_at",
        "finished_at", "passed_count", "failed_count", "score", "queue_wait_seconds",
        "execution_seconds", "wall_seconds", "total_usage", "error",
        "mode", "budget_s", "max_calls", "budget_exhausted")}
    from . import quality_quick
    result["verdict"] = quality_quick.verdict(run)
    result["identity_note"] = IDENTITY_NOTE
    result["cost"] = None
    result["first_token_latency_seconds"] = None
    if include_items:
        from . import db
        items = []
        for item in run.get("items") or []:
            case = item.get("case_snapshot") or {}
            job = db.get_job(item["job_id"]) if item.get("job_id") else None
            attempts = []
            for attempt in (job or {}).get("attempts_detail") or []:
                attempts.append({
                    key: attempt.get(key) for key in (
                        "id", "attempt_no", "worker", "status", "started_at", "finished_at", "error", "usage")
                } | {"elapsed_seconds": _elapsed(attempt.get("started_at"), attempt.get("finished_at"))})
            items.append({key: item.get(key) for key in (
                "id", "ordinal", "case_id", "job_id", "status", "job_status", "passed",
                "grader_reason", "raw_response", "error", "queue_wait_seconds", "execution_seconds",
                "wall_seconds", "usage", "cost", "first_token_latency_seconds", "reported_model",
                "identity_evidence_source", "finished_at")}
                | {"case": {key: (case.get(key) if key != "fixture" else (case.get("fixture") or item.get("fixture")))
                            for key in ("id", "title", "category", "description", "fixture") if key in case or key == "fixture"},
                   "requested_model": run.get("model"), "requested_profile": run.get("profile"),
                   "identity_note": IDENTITY_NOTE, "attempts": attempts})
        result["items"] = items
    return result
