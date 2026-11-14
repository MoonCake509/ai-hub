"""Postgres-слой AI-Hub: задания, попытки, события.

Схема в отдельной схеме `aihub` той же базы, что использует LiteLLM.
Клиент — pg8000 (чистый Python, без сборки бинарников).
Гарантии, заложенные осознанно:
  * задание фиксируется до ответа клиенту;
  * аренда попытки вместо автоматического перезапуска: истёкшая аренда
    помечает попытку `unknown`, а не запускает агента заново;
  * приём результата идемпотентен по attempt_id, устаревшая попытка не перезаписывает актуальную.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import threading
import time
import uuid
from urllib.parse import urlparse

import pg8000.native

_lock = threading.RLock()
# Сколько кандидатов очереди просматривать в поисках свободного checkout.
CANDIDATES = max(1, min(50, int(os.environ.get("LEASE_CANDIDATES", "10"))))
_conn: pg8000.native.Connection | None = None

# Глубина явных транзакций на единственном соединении. Все вызовы идут под
# db._lock (RLock), поэтому счётчик корректен: вложенный db.transaction не
# шлёт второй "begin"/"commit", а сливается с внешней транзакцией — так
# create_job внутри dispatch атомарен с выдачей шага, а не коммитит её раньше.
_txn_depth = 0


@contextlib.contextmanager
def transaction(conn):
    """Явная транзакция: begin/commit на внешнем уровне, вложенные — no-op."""
    global _txn_depth
    if _txn_depth == 0:
        conn.run("begin")
    _txn_depth += 1
    try:
        yield
    except Exception:
        _txn_depth -= 1
        if _txn_depth == 0:
            conn.run("rollback")
        raise
    else:
        _txn_depth -= 1
        if _txn_depth == 0:
            conn.run("commit")


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}

# Тестовый стенд работает в отдельной схеме той же БД: рабочие таблицы не затрагиваются.
# Схема подставляется через search_path при подключении, а не правкой SQL-текстов.
SCHEMA = os.environ.get("SCHEMA", "aihub")
if not SCHEMA.replace("_", "").isalnum():
    raise ValueError(f"недопустимое имя схемы: {SCHEMA!r}")


def set_run_task(run_id: str, task_root: str, branch: str, base_sha: str,
                 head_sha: str = "") -> None:
    """Фиксирует рабочую копию задачи и переводит снимок запуска на неё.

    Контекст-пакеты по-прежнему читаются из основной копии на зафиксированном
    SHA; задания кодера/тестера/ревьюера получают рабочую папку задачи.
    """
    with _lock:
        conn = connect()
        with transaction(conn):
            row = _one(conn, _qualify(
                "select project_snapshot from aihub.runs where id = :i for update"), i=run_id)
            raw = (row or {}).get("project_snapshot")
            if isinstance(raw, dict):
                snapshot = dict(raw)
            elif isinstance(raw, str) and raw.strip():
                snapshot = json.loads(raw)
            else:
                snapshot = {}
            snapshot["main_workdir"] = snapshot.get("main_workdir") or snapshot.get("project_workdir")
            snapshot["project_workdir"] = task_root
            _one(conn, _qualify(
                "update aihub.runs set task_root = :t, task_branch = :b, base_sha = :s,"
                " project_snapshot = :ps::jsonb,"
                " updated_at = now() where id = :i returning id"),
                t=task_root, b=branch, s=base_sha,
                ps=json.dumps(snapshot, ensure_ascii=False), i=run_id)


def set_run_candidate_sha(run_id: str, sha: str) -> None:
    with _lock:
        conn = connect()
        _one(conn, _qualify(
            "update aihub.runs set candidate_sha = :s, updated_at = now()"
            " where id = :i returning id"), s=sha, i=run_id)


def set_run_merge(run_id: str, state: str, merged_sha: str = "") -> None:
    with _lock:
        conn = connect()
        _one(conn, _qualify(
            "update aihub.runs set merge_state = :m, merged_sha = nullif(:s, ''),"
            " updated_at = now() where id = :i returning id"), m=state, s=merged_sha, i=run_id)


def _qualify(sql: str) -> str:
    """Переводит литералы схемы `aihub.` на активную схему.

    Для прода это тождественная замена. Для стенда все обращения уходят в aihub_test.
    """
    if SCHEMA == "aihub":
        return sql
    return sql.replace("aihub.", SCHEMA + ".")

DDL = [
    "create schema if not exists aihub",
    """create table if not exists aihub.jobs (
        id text primary key,
        agent_id text not null,
        prompt text not null,
        prompt_sha text not null,
        timeout_s int not null,
        status text not null default 'queued',
        idempotency_key text,
        attempts int not null default 0,
        needs_reconcile boolean not null default false,
        created_at timestamptz not null default now(),
        started_at timestamptz,
        finished_at timestamptz,
        result_output text,
        result_error text,
        result_usage jsonb,
        requested_model text,
        requested_profile text,
        effective_model text,
        lease_until timestamptz,
        worker text
    )""",
    """create unique index if not exists jobs_idem
        on aihub.jobs(idempotency_key) where idempotency_key is not null""",
    "create index if not exists jobs_status on aihub.jobs(status, created_at desc)",
    """create table if not exists aihub.job_attempts (
        id text primary key,
        job_id text not null references aihub.jobs(id) on delete cascade,
        attempt_no int not null,
        worker text,
        status text not null default 'running',
        lease_until timestamptz,
        started_at timestamptz not null default now(),
        finished_at timestamptz,
        output text,
        error text,
        usage jsonb
    )""",
    "create index if not exists attempts_job on aihub.job_attempts(job_id, attempt_no desc)",
    """create table if not exists aihub.usage_ledger (
        attempt_id text primary key,
        job_id text,
        project_id text,
        role_id text,
        agent_id text,
        profile text,
        model text,
        tokens_in bigint not null default 0,
        tokens_out bigint not null default 0,
        tokens_reasoning bigint not null default 0,
        source text not null default 'absent',
        recorded_at timestamptz not null default now()
    )""",
    "create index if not exists usage_ledger_project on aihub.usage_ledger(project_id, recorded_at desc)",
    """create table if not exists aihub.project_jobs (
        id text primary key,
        project_id text not null references aihub.projects(id) on delete cascade,
        name text not null,
        kind text not null default 'archivist',
        agent_id text not null,
        profile text,
        model text,
        prompt text not null,
        schedule text not null default '0 5 * * *',
        timezone text not null default 'Europe/Moscow',
        enabled boolean not null default true,
        last_run_at timestamptz,
        next_run_at timestamptz,
        last_status text,
        last_job_id text,
        fallback_agent_id text,
        fallback_profile text,
        fallback_model text,
        fallback_used boolean not null default false,
        created_at timestamptz not null default now()
    )""",
    "create index if not exists project_jobs_due on aihub.project_jobs(enabled, next_run_at)",
    """create table if not exists aihub.project_job_runs (
        id bigserial primary key,
        job_id text not null references aihub.project_jobs(id) on delete cascade,
        slot text not null,
        status text not null default 'queued',
        agent_job_id text,
        started_at timestamptz not null default now(),
        finished_at timestamptz,
        summary text,
        unique (job_id, slot)
    )""",
    """create table if not exists aihub.late_results (
        id bigserial primary key,
        job_id text,
        attempt_id text not null,
        attempt_status text,
        payload jsonb not null,
        received_at timestamptz not null default now(),
        reviewed_at timestamptz,
        note text
    )""",
    "create unique index if not exists late_attempt on aihub.late_results(attempt_id)",
    """create table if not exists aihub.job_events (
        id bigserial primary key,
        job_id text not null references aihub.jobs(id) on delete cascade,
        attempt_id text,
        event text not null,
        at timestamptz not null default now(),
        payload jsonb
    )""",
    # --- 9.2: рабочее пространство проекта ---
    """create table if not exists aihub.projects (
        id text primary key,
        name text not null,
        host_id text not null default 'laptop',
        workdir text not null default '',
        settings jsonb not null default '{}',
        created_at timestamptz not null default now()
    )""",
    """create table if not exists aihub.roles (
        project_id text not null references aihub.projects(id) on delete cascade,
        role_id text not null,
        agent_id text not null default '',
        profile text,
        model text,
        instructions text not null default '',
        timeout_s int,
        primary key (project_id, role_id)
    )""",
    """create table if not exists aihub.conversations (
        id text primary key,
        project_id text not null references aihub.projects(id) on delete cascade,
        role_id text not null default '',
        title text not null default '',
        created_at timestamptz not null default now()
    )""",
    "create index if not exists conversations_project_page on aihub.conversations"
    "(project_id, created_at desc, id desc)",
    """create table if not exists aihub.messages (
        id bigserial primary key,
        conversation_id text not null references aihub.conversations(id) on delete cascade,
        author text not null,
        body text not null,
        job_id text references aihub.jobs(id) on delete set null,
        state text not null default 'done',
        idempotency_key text,
        created_at timestamptz not null default now()
    )""",
    "create index if not exists messages_conv on aihub.messages(conversation_id, id)",
    "alter table aihub.messages add column if not exists idempotency_key text",
    "create unique index if not exists messages_idem on aihub.messages(idempotency_key) where idempotency_key is not null",
    # Вложения: загрузка с устройства пользователя или выбор файла на устройстве агента.
    """create table if not exists aihub.attachments (
        id text primary key,
        project_id text not null references aihub.projects(id) on delete cascade,
        origin text not null,
        filename text not null,
        mime text not null default '',
        size_bytes int not null default 0,
        storage text not null,
        host_id text,
        host_path text,
        created_at timestamptz not null default now()
    )""",
    "alter table aihub.attachments add column if not exists metadata jsonb",
    """create table if not exists aihub.message_attachments (
        message_id bigint not null references aihub.messages(id) on delete cascade,
        attachment_id text not null references aihub.attachments(id) on delete cascade,
        primary key (message_id, attachment_id)
    )""",
    # Запуск помнит своё назначение снимком — правки карточки роли его не меняют.
    "alter table aihub.jobs add column if not exists project_id text",
    "alter table aihub.jobs add column if not exists conversation_id text",
    "alter table aihub.jobs add column if not exists role_id text",
    "alter table aihub.jobs add column if not exists run_snapshot jsonb",
    # CORE-01: протокол, на котором задание поставлено и на котором его арендовали.
    "alter table aihub.jobs add column if not exists protocol int not null default 2",
    "alter table aihub.project_jobs add column if not exists fallback_agent_id text",
    "alter table aihub.project_jobs add column if not exists fallback_profile text",
    "alter table aihub.project_jobs add column if not exists fallback_model text",
    "alter table aihub.project_jobs add column if not exists fallback_used boolean not null default false",
    "alter table aihub.job_attempts add column if not exists protocol int not null default 2",
    # CORE-02b: kind отделяет агентский prompt от управляемой операции host worker.
    "alter table aihub.jobs add column if not exists kind text not null default 'agent'",
    "alter table aihub.jobs add column if not exists reasoning_effort text",
    "create index if not exists jobs_kind_status on aihub.jobs(kind, status, created_at)",
    """create table if not exists aihub.workspace_receipts (
        idempotency_key text primary key,
        op text not null,
        host_id text not null,
        job_id text,
        args_digest text not null,
        status text not null default 'pending',
        result_digest text,
        result jsonb,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    )""",
    "alter table aihub.workspace_receipts add column if not exists attempt_id text",
    "create unique index if not exists workspace_receipts_attempt on aihub.workspace_receipts(attempt_id) where attempt_id is not null",
    """create table if not exists aihub.workspace_reconciliation_audit (
        case_id text not null,
        revision int not null,
        action text not null,
        actor text not null,
        reason text not null,
        evidence jsonb not null default '{}',
        created_at timestamptz not null default now(),
        primary key (case_id, revision)
    )""",
    "create index if not exists workspace_reconciliation_audit_created on aihub.workspace_reconciliation_audit(created_at desc, case_id)",
    # CTX-01: неизменяемые пакеты входных артефактов.
    """create table if not exists aihub.context_packets (
        id text primary key,
        project_id text not null,
        conversation_id text,
        state text not null default 'probing',
        revision int not null default 1,
        request jsonb not null default '{}',
        digest text,
        budget_bytes bigint not null default 0,
        used_bytes bigint not null default 0,
        manifest jsonb not null default '{}',
        provider_job_id text,
        provider_op text,
        materialized_path text,
        materialized_digest text,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        sealed_at timestamptz,
        materialized_at timestamptz
    )""",
    "create index if not exists context_packets_project on aihub.context_packets(project_id, created_at desc)",
    # FLOW-01: durable запуски, задачи и журнал.
    """create table if not exists aihub.runs (
        id text primary key,
        project_id text not null,
        template_id text not null default 'default',
        template_version int not null default 1,
        policy jsonb not null default '{}',
        cycles int not null default 0,
        state text not null default 'active',
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        finished_at timestamptz
    )""",
    "alter table aihub.runs add column if not exists cycles int not null default 0",
    "create index if not exists runs_state on aihub.runs(state, updated_at)",
    """create table if not exists aihub.run_tasks (
        id text primary key,
        run_id text not null references aihub.runs(id) on delete cascade,
        key text not null,
        role text,
        agent text,
        prompt text,
        depends_on jsonb not null default '[]',
        position int not null default 0,
        state text not null default 'pending',
        attempt int not null default 0,
        job_id text,
        error text,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        finished_at timestamptz
    )""",
    "alter table aihub.run_tasks add column if not exists context jsonb not null default '{}'",
    "alter table aihub.run_tasks add column if not exists context_id text",
    "create unique index if not exists run_tasks_key on aihub.run_tasks(run_id, key)",
    "create index if not exists run_tasks_job on aihub.run_tasks(job_id)",
    """create table if not exists aihub.run_events (
        id bigserial primary key,
        run_id text not null references aihub.runs(id) on delete cascade,
        task_id text,
        event text not null,
        payload jsonb not null default '{}',
        created_at timestamptz not null default now()
    )""",
    "create index if not exists run_events_run on aihub.run_events(run_id, id)",
    """create table if not exists aihub.context_packet_items (
        packet_id text not null references aihub.context_packets(id) on delete cascade,
        path text not null,
        size bigint not null default 0,
        sha256 text,
        selected boolean not null default false,
        reason text not null default '',
        primary key (packet_id, path)
    )""",
    """create table if not exists aihub.context_packet_files (
        packet_id text not null references aihub.context_packets(id) on delete cascade,
        revision int not null,
        path text not null,
        sha256 text not null,
        size bigint not null,
        content text not null,
        primary key (packet_id, revision, path)
    )""",
    # P-09: семантический контекст — отдельный тип evidence, не файлы пакета.
    """create table if not exists aihub.context_packet_evidence (
        packet_id text not null references aihub.context_packets(id) on delete cascade,
        revision int not null,
        position int not null,
        sha text not null,
        path text not null,
        start_line int not null,
        end_line int not null,
        kind text not null,
        text text not null,
        blob_oid text not null,
        text_sha256 text not null,
        score double precision not null default 0,
        model text not null default '',
        primary key (packet_id, revision, position)
    )""",
    "create index if not exists context_packet_evidence_sha on aihub.context_packet_evidence(packet_id, revision)",
    # P-09: черновик пакета на время асинхронной сборки семантического контекста.
    # Файлы и digest фиксируются только после проверки evidence, поэтому до
    # финализации пакет не имеет ревизии и не может быть выдан роли.
    """create table if not exists aihub.context_packet_drafts (
        packet_id text primary key references aihub.context_packets(id) on delete cascade,
        draft jsonb not null default '{}',
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    )""",
    # FLOW-P1: история ревизий пакетов — ревизии неизменяемы, текущие items лишь указатель.
    """create table if not exists aihub.context_packet_revisions (
        packet_id text not null references aihub.context_packets(id) on delete cascade,
        revision int not null,
        digest text,
        items jsonb not null default '[]',
        sealed_at timestamptz not null default now(),
        primary key (packet_id, revision)
    )""",
    # FLOW-P1: запуск связан с целью, критериями, снимком назначений и проекта.
    "alter table aihub.runs add column if not exists request_key text",
    "alter table aihub.runs add column if not exists idempotency_sha text",
    "create unique index if not exists runs_request_key on aihub.runs(request_key)"
    " where request_key is not null",
    "alter table aihub.runs add column if not exists goal text not null default ''",
    "alter table aihub.runs add column if not exists criteria jsonb not null default '[]'",
    "alter table aihub.runs add column if not exists assignments jsonb not null default '{}'",
    "alter table aihub.runs add column if not exists project_snapshot jsonb not null default '{}'",
    "alter table aihub.runs add column if not exists budgets jsonb not null default '{}'",
    "alter table aihub.runs add column if not exists budget_used jsonb not null default '{}'",
    # FLOW-P1: проверяемый кандидат и receipt изолированного runner'а.
    "alter table aihub.runs add column if not exists candidate_digest text",
    "alter table aihub.runs add column if not exists candidate_snapshot_id text",
    "alter table aihub.runs add column if not exists check_receipt jsonb",
    "alter table aihub.runs add column if not exists pipeline jsonb not null default '{}'",
    # FLOW-P8: отдельная рабочая копия задачи и слияние проверенного коммита.
    "alter table aihub.runs add column if not exists task_root text",
    "alter table aihub.runs add column if not exists task_branch text",
    "alter table aihub.runs add column if not exists base_sha text",
    "alter table aihub.runs add column if not exists candidate_sha text",
    "alter table aihub.runs add column if not exists merge_state text",
    "alter table aihub.runs add column if not exists merged_sha text",
    "alter table aihub.runs add column if not exists cancel_info jsonb",
    # FLOW-P1: контекст, результат, назначение и receipt фиксируются на задаче.
    "alter table aihub.run_tasks add column if not exists assignment jsonb not null default '{}'",
    "alter table aihub.run_tasks add column if not exists result jsonb",
    "alter table aihub.run_tasks add column if not exists receipt jsonb",
    "alter table aihub.run_tasks add column if not exists retry_feedback jsonb not null default '{}'",
    "alter table aihub.run_tasks add column if not exists packet_revision int",
        # P-01: создание рабочей папки проекта — статус подготовки и привязка Git.
        "alter table aihub.projects add column if not exists status text not null default 'ready'",
        "alter table aihub.projects add column if not exists repo_root text",
        "alter table aihub.projects add column if not exists git_root text",
        "alter table aihub.projects add column if not exists branch text",
        "alter table aihub.projects add column if not exists head_sha text",
        "alter table aihub.projects add column if not exists template_version text",
        "alter table aihub.projects add column if not exists prepare_error jsonb",
        "alter table aihub.projects add column if not exists prepared_at timestamptz",
        "alter table aihub.projects add column if not exists request_key text",
        "alter table aihub.projects add column if not exists index_sha text",
        "alter table aihub.projects add column if not exists index_files int",
        "alter table aihub.projects add column if not exists indexed_at timestamptz",
        "create unique index if not exists projects_request_key on aihub.projects(request_key)"
        " where request_key is not null",
        # P-09: векторы фрагментов кода и документации, привязанные к коммиту.
        "create table if not exists aihub.semantic_chunks (id bigserial primary key,"
        " project_id text not null, sha text not null, path text not null, start_line int not null,"
        " end_line int not null, kind text not null, text text not null, vector jsonb not null,"
        " model text not null default '', created_at timestamptz not null default now())",
        "create index if not exists semantic_chunks_scope on aihub.semantic_chunks(project_id, sha)",
        "create unique index if not exists semantic_chunks_unique on aihub.semantic_chunks"
        "(project_id, sha, path, start_line)",
    ]


def _dsn() -> dict:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    p = urlparse(url)
    return {
        "user": p.username or "llmproxy",
        "password": p.password or "",
        "host": p.hostname or "postgres",
        "port": int(p.port or 5432),
        "database": (p.path or "/postgres").lstrip("/"),
    }


_tls = threading.local()


def connect() -> pg8000.native.Connection:
    """Соединение на поток.

    Одно общее соединение pg8000 не выдерживает параллельных вызовов из потоков
    sweeper'а: смешиваются prepared statements, отсюда `portal "" does not exist`
    и `bind message supplies N parameters`. Поток получает своё соединение.
    """
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = pg8000.native.Connection(**_dsn(), timeout=30)
        _tls.conn = conn
    return conn


def init() -> None:
    with _lock:
        conn = connect()
        # Сначала создаём саму схему — её имя подменять нельзя.
        conn.run(f"create schema if not exists {SCHEMA}")
        for stmt in DDL:
            conn.run(_qualify(stmt))
        # Миграция на месте: новые колонки для выбора модели (9.1).
        for col, decl in (("requested_model", "text"), ("effective_model", "text"),
                      ("requested_profile", "text")):
            try:
                conn.run(_qualify(
                    f"alter table aihub.jobs add column if not exists {col} {decl}"))
            except Exception:
                pass


def _rows(conn, sql: str, **params) -> list[dict]:
    result = conn.run(_qualify(sql), **params)
    cols = [c["name"] for c in conn.columns] if conn.columns else []
    return [dict(zip(cols, row)) for row in result]


def _one(conn, sql: str, **params) -> dict | None:
    rows = _rows(conn, sql, **params)
    return rows[0] if rows else None


def event(job_id: str, name: str, attempt_id: str | None = None, payload: dict | None = None) -> None:
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "insert into aihub.job_events(job_id, attempt_id, event, payload)"
            " values (:j, :a, :e, :p::jsonb)"),
            j=job_id, a=attempt_id, e=name, p=json.dumps(payload or {}, ensure_ascii=False),
        )


class QueueFull(Exception):
    """Очередь достигла предела: новое задание не принимаем."""


def create_job(agent_id: str, prompt: str, prompt_sha: str, timeout_s: int,
               idempotency_key: str | None, max_queue: int | None = None,
               requested_model: str | None = None,
               requested_profile: str | None = None,
               workspace: dict | None = None,
               kind: str = "agent",
               reasoning_effort: str | None = None) -> tuple[dict, bool]:
    """Возвращает (job, created). При совпадении ключа с другим телом — ValueError."""
    job_id = uuid.uuid4().hex
    with _lock:
        conn = connect()
        with transaction(conn):
            # Проверка места в очереди и вставка должны быть одной операцией:
            # иначе два процесса увидят одинаково свободное место и превысят лимит.
            conn.run("select pg_advisory_xact_lock(7401)")
            if idempotency_key:
                existing = _one(conn, "select * from aihub.jobs where idempotency_key = :k", k=idempotency_key)
                if existing:
                    # Модель и профиль — часть назначения: смена модели под тем же
                    # ключом не должна молча вернуть задание со старым назначением.
                    if ((existing.get("kind") or "agent") != kind
                            or existing["prompt_sha"] != prompt_sha
                            or existing["agent_id"] != agent_id
                            or int(existing["timeout_s"]) != int(timeout_s)
                            or (existing.get("requested_model") or None) != (requested_model or None)
                            or (existing.get("requested_profile") or None) != (requested_profile or None)
                            or (existing.get("reasoning_effort") or None) != (reasoning_effort or None)):
                        raise ValueError("idempotency_key_conflict")
                    return existing, False
            if max_queue:
                depth = _one(conn, "select count(*) as n from aihub.jobs where status = 'queued'")["n"]
                if depth >= max_queue:
                    raise QueueFull(depth)
            workspace = workspace or {}
            conn.run(_qualify(
                "insert into aihub.jobs(id, agent_id, prompt, prompt_sha, timeout_s, idempotency_key,"
                " requested_model, requested_profile, project_id, conversation_id, role_id, run_snapshot,"
                " kind, reasoning_effort)"
                " values (:i, :a, :p, :s, :t, :k, :rm, :rp, :pr, :cv, :ri, :sn::jsonb, :kd, :re)"),
                i=job_id, a=agent_id, p=prompt, s=prompt_sha, t=timeout_s, k=idempotency_key,
                rm=requested_model, rp=requested_profile, pr=workspace.get("project_id"),
                cv=workspace.get("conversation_id"), ri=workspace.get("role_id"),
                sn=json.dumps(workspace, ensure_ascii=False), kd=kind, re=reasoning_effort,
            )
            conn.run(_qualify("insert into aihub.job_events(job_id, event) values (:j, 'queued')"), j=job_id)
        return get_job(job_id), True


def get_job(job_id: str) -> dict | None:
    with _lock:
        conn = connect()
        job = _one(conn, "select * from aihub.jobs where id = :i", i=job_id)
        if not job:
            return None
        if isinstance(job.get("run_snapshot"), str):
            try:
                job["run_snapshot"] = json.loads(job["run_snapshot"])
            except (TypeError, ValueError):
                job["run_snapshot"] = {}
        job["attempts_detail"] = _rows(
            conn,
            "select id, attempt_no, worker, status, started_at, finished_at, error, usage"
            " from aihub.job_attempts where job_id = :i order by attempt_no desc",
            i=job_id,
        )
        # Миграция на месте: старые таблицы без новых колонок.
        for col in ("requested_model", "effective_model", "requested_profile"):
            if col not in job:
                try:
                    conn.run(_qualify(
                        f"alter table aihub.jobs add column if not exists {col} text"))
                    job[col] = None
                except Exception:
                    job[col] = None
        return job


def get_job_by_idempotency_key(key: str) -> dict | None:
    with _lock:
        conn = connect()
        job = _one(conn, "select * from aihub.jobs where idempotency_key = :k", k=key)
        if job and isinstance(job.get("run_snapshot"), str):
            try:
                job["run_snapshot"] = json.loads(job["run_snapshot"])
            except (TypeError, ValueError):
                job["run_snapshot"] = {}
        return job


def list_jobs(status: str | None, limit: int = 50, offset: int = 0) -> list[dict]:
    with _lock:
        conn = connect()
        if status:
            return _rows(conn, "select id, agent_id, status, created_at, finished_at, attempts,"
                               " requested_model, requested_profile, effective_model,"
                               " left(prompt, 160) as prompt_preview"
                               " from aihub.jobs where status = :s order by created_at desc"
                               " limit :l offset :o",
                         s=status, l=limit, o=offset)
        return _rows(conn, "select id, agent_id, status, created_at, finished_at, attempts,"
                          " requested_model, requested_profile, effective_model,"
                          " left(prompt, 160) as prompt_preview"
                          " from aihub.jobs order by created_at desc limit :l offset :o",
                    l=limit, o=offset)


def list_late(limit: int = 50) -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(conn, "select id, job_id, attempt_id, attempt_status, received_at,"
                           " reviewed_at, note, payload"
                           " from aihub.late_results order by received_at desc limit :l", l=limit)


def count_jobs(status: str | None = None) -> int:
    with _lock:
        conn = connect()
        if status:
            return _one(conn, "select count(*) as n from aihub.jobs where status = :s", s=status)["n"]
        return _one(conn, "select count(*) as n from aihub.jobs")["n"]


def events(job_id: str) -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(
            conn,
            "select event, attempt_id, at, payload from aihub.job_events"
            " where job_id = :j order by id",
            j=job_id,
        )


def _checkout_key(conn, row: dict) -> str | None:
    """Ключ писательского checkout для задания (CORE-02).

    Задание без проекта и без рабочего каталога не занимает checkout: у него нет
    места, куда писать файлы. Ключ строится из фактического host+path, а не из
    имени агента, поэтому два проекта с одним каталогом — один ресурс.
    """
    if (row.get("kind") or "agent") == "workspace":
        # Чтение/снимок/упаковка не меняют рабочее дерево пользователя: держать
        # на них checkout — значит останавливать кодеров без причины.
        op = _json_object(row.get("run_snapshot")).get("op")
        if op != "apply_patch":
            return None
    from . import resources

    snap = row.get("run_snapshot") or {}
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except Exception:
            snap = {}
    workdir = snap.get("project_workdir") or snap.get("workdir")
    host_id = snap.get("project_host_id") or snap.get("host_id")
    project_id = row.get("project_id")
    if project_id and (not workdir or not host_id):
        proj = _one(conn, _qualify("select workdir, host_id from aihub.projects where id = :p"),
                    p=project_id)
        if proj:
            workdir = workdir or proj.get("workdir")
            host_id = host_id or proj.get("host_id")
    if not workdir or not host_id:
        return None
    try:
        return resources.key_for("checkout", host_id, workdir)
    except ValueError as exc:
        print("checkout key rejected:", exc, flush=True)
        return None


def lease_job(worker: str, lease_seconds: int = 300, agents: str = "",
              client_protocol: int = 0, kinds: str = "agent") -> dict | None:
    """Атомарно берёт задание из очереди, создаёт попытку. None — очередь пуста.

    Аренда возможна только клиентом, который говорит на протоколе не ниже
    протокола задания: старый poller (v1) не видит заданий v2 и не может
    украсть исполнение. Версия клиента записывается в попытку, чтобы поздний
    результат можно было отнести к протоколу исполнения.
    """
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            # Голова очереди может ждать занятый checkout. Перебираем кандидатов
            # по порядку и берём первое задание, которое можно выполнить сейчас:
            # воркер не простаивает из-за одного занятого каталога, а порядок
            # остаётся FIFO для всех остальных.
            candidates = _rows(
                conn,
                """select id from aihub.jobs
                    where status = 'queued'
                      and protocol <= :cp
                      and kind = any(string_to_array(:kinds, ','))
                      and (:agents = '' or agent_id = any(string_to_array(:agents, ',')))
                    order by (kind = 'workspace') desc, created_at
                    limit :n""",
                cp=int(client_protocol), kinds=kinds or "agent", agents=agents,
                n=CANDIDATES,
            )
            row = None
            resource = None
            for cand in candidates:
                conn.run("savepoint cand")
                row = _one(
                    conn,
                    """update aihub.jobs j
                          set status = 'running',
                              started_at = now(),
                              lease_until = now() + (:lease || ' seconds')::interval,
                              worker = :w,
                              attempts = j.attempts + 1
                        where j.id = :i and j.status = 'queued'
                      returning j.id, j.agent_id, j.prompt, j.timeout_s, j.attempts,
                                j.requested_model, j.requested_profile, j.run_snapshot,
                                j.project_id, j.protocol, j.kind, j.reasoning_effort""",
                    lease=str(int(lease_seconds)), w=worker, i=cand["id"],
                )
                if not row:
                    conn.run("rollback to savepoint cand")
                    conn.run("release savepoint cand")
                    continue
                resource_key = _checkout_key(conn, row)
                if resource_key:
                    from . import resources
                    resource = resources.acquire(conn, resource_key, worker or "unknown",
                                                 row["id"], lease_seconds)
                    if resource is None:
                        conn.run("rollback to savepoint cand")
                        conn.run("release savepoint cand")
                        row, resource = None, None
                        continue
                conn.run("release savepoint cand")
                break
            if not row:
                conn.run("commit")
                return None
            # CORE-02: писательская аренда checkout берётся в этой же транзакции.
            # Если каталог занят другим живым заданием, транзакция откатывается и
            # задание остаётся queued: воркер не получает двух писателей в один
            # checkout даже при одновременном опросе.
            attempt_id = uuid.uuid4().hex
            conn.run(_qualify(
                "insert into aihub.job_attempts(id, job_id, attempt_no, worker, lease_until, protocol)"
                " values (:a, :j, :n, :w, now() + (:lease || ' seconds')::interval, :p)"),
                a=attempt_id, j=row["id"], n=row["attempts"], w=worker, lease=str(int(lease_seconds)),
                p=int(client_protocol),
            )
            conn.run(_qualify(
                "insert into aihub.job_events(job_id, attempt_id, event) values (:j, :a, 'leased')"),
                j=row["id"], a=attempt_id,
            )
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        snap = row.get("run_snapshot") or {}
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception:
                snap = {}
        # Рабочая папка — из проекта, а не из промпта. Воркер запускает агента с этим cwd.
        workdir = snap.get("workdir") if "workdir" in snap else None
        host_id = snap.get("host_id")
        project_id = row.get("project_id") or snap.get("project_id")
        if project_id:
            proj = _one(conn, _qualify("select workdir, host_id from aihub.projects where id = :p"),
                        p=project_id)
            if proj:
                if "workdir" not in snap:
                    workdir = proj.get("workdir") or None
                host_id = host_id or proj.get("host_id")
        # Вложения задания: содержимое передаётся воркеру, а не ссылка.
        attachments = []
        refs = snap.get("attachment_refs") or []
        if not refs:
            versions = snap.get("attachment_versions") or {}
            refs = [{"id": aid, "version": versions.get(aid) or aid}
                    for aid in (snap.get("attachment_ids") or [])]
        for ref in refs[:5]:
            aid = ref.get("id") if isinstance(ref, dict) else ref
            if not aid:
                continue
            att = _one(conn, _qualify(
                "select id, project_id, filename, mime, storage, origin from aihub.attachments"
                " where id = :a and project_id = :p"),
                a=aid, p=project_id)
            if att and (not isinstance(ref, dict) or
                        str(ref.get("version") or aid) == str(aid)):
                attachments.append({"id": att["id"], "filename": att["filename"],
                                    "mime": att["mime"], "content_b64": att["storage"] or "",
                                    "origin": att["origin"], "version": str(aid),
                                    "source": ref.get("source") if isinstance(ref, dict) else None,
                                    "message_id": ref.get("message_id") if isinstance(ref, dict) else None})
        return {**row, "attempt_id": attempt_id, "lease_seconds": lease_seconds,
                "protocol": int(row.get("protocol") or client_protocol),
                "resource": resource,
                "workdir": workdir or None, "host_id": host_id, "attachments": attachments}


def heartbeat(attempt_id: str, lease_seconds: int = 300) -> bool:
    return heartbeat_detail(attempt_id, lease_seconds) is not None


def heartbeat_detail(attempt_id: str, lease_seconds: int = 300,
                     resource_generation: int | None = None) -> dict | None:
    """Продлить аренду и вернуть версии протокола попытки и задания.

    None означает, что продлевать нечего (попытка или задание уже не running):
    хаб отвечает воркеру ok=false, и воркер знает, что аренда потеряна.
    """
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            row = _one(
                conn,
                """update aihub.job_attempts a
                      set lease_until = now() + (:lease || ' seconds')::interval
                    from aihub.jobs j
                   where a.id = :a and a.status = 'running'
                     and j.id = a.job_id and j.status = 'running'
                   returning a.id, a.protocol, j.protocol as job_protocol, j.id as job_id, j.kind,
                             j.run_snapshot->>'op' as workspace_op""",
                a=attempt_id, lease=str(int(lease_seconds)),
            )
            if not row:
                conn.run("rollback")
                return None
            from . import resources
            resource = resources.renew_for_job(
                conn, row["job_id"], resource_generation, lease_seconds)
            if ((row.get("kind") == "workspace" and row.get("workspace_op") == "apply_patch"
                     and not resource) or (resource_generation is not None and not resource)):
                conn.run("rollback")
                return None
            conn.run("commit")
            return {"id": row["id"], "job_id": row["job_id"],
                    "attempt_protocol": int(row["protocol"] or 0),
                    "job_protocol": int(row["job_protocol"] or 0),
                    "resource": ({"key": resource["key"],
                                  "generation": int(resource["generation"])} if resource else None)}
        except Exception:
            conn.run("rollback")
            raise


def _store_late(conn, job_id: str | None, attempt_id: str, attempt_status: str | None,
                ok: bool, output: str | None, error: str | None,
                usage: dict | None, elapsed: float | None) -> None:
    """Складывает неприменённый результат отдельно: он не теряется и не меняет статус задания."""
    payload = json.dumps({
        "ok": ok, "output": output, "error": error,
        "usage": usage or {}, "elapsed": elapsed,
    }, ensure_ascii=False)
    inserted = _one(conn, _qualify(
        "insert into aihub.late_results(job_id, attempt_id, attempt_status, payload)"
        " values (:j, :a, :s, :p::jsonb) on conflict (attempt_id) do nothing returning attempt_id"),
        j=job_id, a=attempt_id, s=attempt_status, p=payload)
    if inserted:
        return
    prior = _one(conn, _qualify(
        "select job_id, attempt_status, payload from aihub.late_results where attempt_id = :a"),
        a=attempt_id)
    prior_payload = _json_object(prior.get("payload")) if prior else None
    wanted_payload = _json_object(payload)
    if (not prior or prior.get("job_id") != job_id
            or prior.get("attempt_status") != attempt_status
            or prior_payload != wanted_payload):
        raise ValueError("late_result_attempt_conflict")


def _usage_numbers(usage: dict | None) -> tuple[int, int, int, str]:
    """Токены из отчёта канала. Ноль — «нет данных», а не «бесплатно»."""
    data = usage or {}
    def pick(*names):
        for name in names:
            value = data.get(name)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0
    t_in = pick("tokens_in", "input_tokens", "prompt_tokens")
    t_out = pick("tokens_out", "output_tokens", "completion_tokens")
    t_reason = pick("tokens_reasoning", "reasoning_tokens")
    return t_in, t_out, t_reason, ("reported" if (t_in or t_out or t_reason) else "absent")


def _record_usage(conn, job: dict, attempt_id: str, usage: dict | None) -> None:
    """append-only журнал: одна строка на попытку, повтор не удваивает счёт."""
    t_in, t_out, t_reason, source = _usage_numbers(usage)
    conn.run(_qualify(
        "insert into aihub.usage_ledger(attempt_id, job_id, project_id, role_id, agent_id,"
        " profile, model, tokens_in, tokens_out, tokens_reasoning, source)"
        " values (:a, :j, :p, :r, :ag, :pr, :m, :ti, :to, :tr, :s)"
        " on conflict (attempt_id) do nothing"),
        a=attempt_id, j=job.get("id"), p=job.get("project_id"), r=job.get("role_id"),
        ag=job.get("agent_id"), pr=job.get("requested_profile"),
        m=(usage or {}).get("reported_model") or (usage or {}).get("model") or job.get("effective_model"),
        ti=t_in, to=t_out, tr=t_reason, s=source)


def usage_summary(project_id: str, days: int = 30) -> dict:
    """Расход в токенах по проекту: всего и разрез провайдер → модель."""
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select coalesce(agent_id, '') as agent_id, coalesce(profile, '') as profile,"
            " coalesce(model, '') as model, sum(tokens_in) as tokens_in,"
            " sum(tokens_out) as tokens_out, sum(tokens_reasoning) as tokens_reasoning,"
            " count(*) as attempts, max(recorded_at) as last_at"
            " from aihub.usage_ledger where project_id = :p"
            " and recorded_at >= now() - (:d || ' days')::interval"
            " group by 1, 2, 3 order by (sum(tokens_in) + sum(tokens_out)) desc"),
            p=project_id, d=str(max(1, min(int(days or 30), 365))))
    items = [{
        "agent_id": row["agent_id"], "profile": row["profile"], "model": row["model"],
        "tokens_in": int(row["tokens_in"] or 0), "tokens_out": int(row["tokens_out"] or 0),
        "tokens_reasoning": int(row["tokens_reasoning"] or 0),
        "tokens_total": int((row["tokens_in"] or 0) + (row["tokens_out"] or 0) + (row["tokens_reasoning"] or 0)),
        "attempts": int(row["attempts"] or 0),
        "last_at": row["last_at"].isoformat() if row.get("last_at") else None,
        "source": "reported" if (row["tokens_in"] or row["tokens_out"] or row["tokens_reasoning"]) else "absent",
    } for row in rows]
    totals = {k: sum(item[k] for item in items) for k in
              ("tokens_in", "tokens_out", "tokens_reasoning", "tokens_total", "attempts")}
    return {"project_id": project_id, "days": days, "totals": totals, "items": items,
            "has_usage": totals["tokens_total"] > 0}

def delete_project(project_id: str) -> bool:
    """Удаляет карточку проекта; роли, чаты, вложения и задания уходят каскадом."""
    with _lock:
        conn = connect()
        conn.run("delete from aihub.projects where id = :id", id=project_id)
        row = _one(conn, "select count(*) as n from aihub.projects where id = :id", id=project_id)
    return not int((row or {}).get("n") or 0)


def usage_analytics(days: int = 1, profile: str = "", model: str = "") -> dict:
    """Расход по времени: часы для суток, дни для 7/30; срезы по модели и провайдеру."""
    days = max(1, min(int(days or 1), 90))
    bucket = "hour" if days <= 1 else "day"
    where = ["recorded_at >= now() - (:d || ' days')::interval"]
    params = {"d": str(days)}
    if profile:
        where.append("coalesce(profile, '') = :prof")
        params["prof"] = profile
    if model:
        where.append("coalesce(model, '') = :mod")
        params["mod"] = model
    clause = " and ".join(where)
    with _lock:
        conn = connect()
        series = _rows(conn, _qualify(
            "select to_char(date_trunc(:b, recorded_at), 'YYYY-MM-DD HH24:00') as bucket,"
            " sum(tokens_in) as tokens_in, sum(tokens_out) as tokens_out,"
            " sum(tokens_reasoning) as tokens_reasoning, count(*) as attempts"
            " from aihub.usage_ledger where " + clause +
            " group by 1 order by 1"), b=bucket, **params)
        by_model = _rows(conn, _qualify(
            "select coalesce(profile, '') as profile, coalesce(model, '') as model,"
            " sum(tokens_in) as tokens_in, sum(tokens_out) as tokens_out,"
            " sum(tokens_reasoning) as tokens_reasoning, count(*) as attempts"
            " from aihub.usage_ledger where " + clause +
            " group by 1, 2 order by (sum(tokens_in) + sum(tokens_out) + sum(tokens_reasoning)) desc"),
            **params)
    def pack(row: dict) -> dict:
        tin = int(row["tokens_in"] or 0); tout = int(row["tokens_out"] or 0); tr = int(row["tokens_reasoning"] or 0)
        return {"tokens_in": tin, "tokens_out": tout, "tokens_reasoning": tr,
                "tokens_total": tin + tout + tr, "attempts": int(row["attempts"] or 0)}
    points = [{"bucket": row["bucket"], **pack(row)} for row in series]
    models = [{"profile": row["profile"], "model": row["model"], **pack(row)} for row in by_model]
    providers: dict[str, dict] = {}
    for item in models:
        acc = providers.setdefault(item["profile"] or "—", {"tokens_in": 0, "tokens_out": 0,
                                                        "tokens_reasoning": 0, "tokens_total": 0, "attempts": 0})
        for key in ("tokens_in", "tokens_out", "tokens_reasoning", "tokens_total", "attempts"):
            acc[key] += item[key]
    totals = {key: sum(point[key] for point in points) for key in
              ("tokens_in", "tokens_out", "tokens_reasoning", "tokens_total", "attempts")}
    return {"days": days, "bucket": bucket, "series": points, "models": models,
            "providers": [{"profile": name, **value} for name, value in providers.items()],
            "totals": totals, "has_usage": totals["tokens_total"] > 0,
            "options": {"providers": sorted({item["profile"] for item in models if item["profile"]}),
                        "models": sorted({item["model"] for item in models if item["model"]})}}


def finish_attempt(attempt_id: str, ok: bool, output: str | None, error: str | None,
                   usage: dict | None, elapsed: float | None,
                   job_id_hint: str | None = None, uncertain: bool = False) -> str:
    """Идемпотентно закрывает попытку.

    applied   — результат сохранён в задание;
    duplicate — эта попытка уже закрыта тем же результатом, повтор ничего не меняет;
    late      — аренда истекла (или задание уже закрыто иначе): результат сохранён в late_results;
    stale     — попытка не последняя, задание закрыто другой попыткой: результат в late_results;
    not_found — такой попытки нет.
    """
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            attempt = _one(conn, "select * from aihub.job_attempts where id = :a", a=attempt_id)
            if not attempt:
                conn.run("rollback")
                return "not_found"
            if job_id_hint and job_id_hint != attempt["job_id"]:
                conn.run("rollback")
                return "attempt_mismatch"
            if attempt["status"] in ("succeeded", "failed"):
                conn.run("rollback")
                return "duplicate"
            if (attempt["status"] == "unknown"
                    and attempt.get("output") == output
                    and attempt.get("error") == error
                    and _json_object(attempt.get("usage")) == (usage or {})):
                conn.run("rollback")
                return "duplicate"
            if attempt["status"] != "running":
                _store_late(conn, attempt["job_id"], attempt_id, attempt["status"],
                            ok, output, error, usage, elapsed)
                conn.run("commit")
                return "late"
            job = _one(conn, "select * from aihub.jobs where id = :j for update", j=attempt["job_id"])
            if not job or job["status"] != "running":
                _store_late(conn, attempt["job_id"], attempt_id,
                            attempt["status"], ok, output, error, usage, elapsed)
                conn.run("commit")
                return "stale"
            latest = _one(
                conn,
                "select max(attempt_no) as n from aihub.job_attempts where job_id = :j", j=job["id"],
            )
            if latest and latest["n"] != attempt["attempt_no"]:
                _store_late(conn, job["id"], attempt_id, attempt["status"],
                            ok, output, error, usage, elapsed)
                conn.run("commit")
                return "stale"
            if uncertain:
                conn.run(_qualify(
                    "update aihub.job_attempts set status = 'unknown', finished_at = now(),"
                    " output = :o, error = :e, usage = :u::jsonb where id = :a and status = 'running'"),
                    o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False), a=attempt_id)
                conn.run(_qualify(
                    "update aihub.jobs set status = 'unknown', finished_at = now(),"
                    " result_output = :o, result_error = :e, result_usage = :u::jsonb,"
                    " lease_until = null, needs_reconcile = true where id = :j and status = 'running'"),
                    o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False), j=job["id"])
                conn.run(_qualify(
                    "insert into aihub.job_events(job_id, attempt_id, event, payload)"
                    " values (:j, :a, 'execution_uncertain', :p::jsonb)"),
                    j=job["id"], a=attempt_id,
                    p=json.dumps({"elapsed": elapsed}, ensure_ascii=False))
                # An unknown outcome may still have a native writer running
                # after its worker lost heartbeats. Keep the checkout blocked
                # until explicit recovery; TTL alone must not invite a second
                # writer into the same tree.
                conn.run(_qualify(
                    "update aihub.resource_leases set lease_until = 'infinity'::timestamptz"
                    " where job_id = :j"), j=job["id"])
                conn.run("commit")
                return "unknown"
            status = "succeeded" if ok else "failed"
            conn.run(_qualify(
                "update aihub.job_attempts set status = :s, finished_at = now(), output = :o,"
                " error = :e, usage = :u::jsonb where id = :a"),
                s=status, o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False), a=attempt_id,
            )
            conn.run(_qualify(
                "update aihub.jobs set status = :s, finished_at = now(), result_output = :o,"
                " result_error = :e, result_usage = :u::jsonb, lease_until = null,"
                " needs_reconcile = false, effective_model = :em where id = :j"),
                s=status, o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False),
                em=next((value.strip() for value in ((usage or {}).get("reported_model"),
                                                     (usage or {}).get("model"))
                         if isinstance(value, str) and value.strip()), None), j=job["id"],
            )
            conn.run(_qualify(
                "insert into aihub.job_events(job_id, attempt_id, event, payload)"
                " values (:j, :a, :e, :p::jsonb)"),
                j=job["id"], a=attempt_id, e=status,
                p=json.dumps({"elapsed": elapsed}, ensure_ascii=False),
            )
            # Расход в токенах фиксируется там же, где закрывается попытка:
            # повторный результат не создаёт вторую строку (primary key attempt_id).
            _record_usage(conn, job, attempt_id, usage)
            # CORE-02: задание закрыто — его checkout свободен для следующего.
            from . import resources
            resources.release_for_job(conn, job["id"])
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return "applied"


# --- Автоматизация проекта: задачи по расписанию (решение владельца 2026-11-14) ---
# Архивариус живёт не в списке ролей, а здесь: расписание, модель, промпт и
# последний запуск видны в одном месте. Расписание поддерживает ежедневный вид
# "M H * * *"; часовой пояс МСК фиксирован (UTC+3, без перехода на летнее время).
MSK_OFFSET = 3 * 3600


def _parse_daily(schedule: str) -> tuple[int, int]:
    parts = (schedule or "").split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        raise ValueError("schedule must be daily cron: 'M H * * *'")
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError("schedule time is out of range")
    return minute, hour


def _next_daily_run(schedule: str, now: float | None = None) -> float:
    minute, hour = _parse_daily(schedule)
    moment = now if now is not None else time.time()
    local = moment + MSK_OFFSET
    day_start = local - (local % 86400)
    target = day_start + hour * 3600 + minute * 60
    if target <= local:
        target += 86400
    return target - MSK_OFFSET


def create_project_job(project_id: str, payload: dict) -> dict:
    name = (payload.get("name") or "Архивариус").strip()[:120]
    agent_id = (payload.get("agent_id") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    schedule = (payload.get("schedule") or "0 5 * * *").strip()
    if not agent_id:
        raise ValueError("agent_id is required")
    if not prompt:
        raise ValueError("prompt is required")
    _parse_daily(schedule)
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    with _lock:
        conn = connect()
        with transaction(conn):
            conn.run(_qualify(
                "insert into aihub.project_jobs(id, project_id, name, kind, agent_id, profile,"
                " model, prompt, schedule, timezone, enabled, next_run_at,"
                " fallback_agent_id, fallback_profile, fallback_model)"
                " values (:i, :p, :n, :k, :a, :pr, :m, :t, :s, 'Europe/Moscow', :e, to_timestamp(:nx),"
                " :fa, :fp, :fm)"),
                i=job_id, p=project_id, n=name, k=(payload.get("kind") or "archivist"),
                a=agent_id, pr=payload.get("profile"), m=payload.get("model"), t=prompt,
                s=schedule, e=bool(payload.get("enabled", True)),
                nx=_next_daily_run(schedule, now),
                fa=payload.get("fallback_agent_id") or None,
                fp=payload.get("fallback_profile") or None,
                fm=payload.get("fallback_model") or None)
    return get_project_job(job_id)


def get_project_job(job_id: str) -> dict | None:
    with _lock:
        conn = connect()
        return _one(conn, _qualify("select * from aihub.project_jobs where id = :i"), i=job_id)


def list_project_jobs(project_id: str) -> list[dict]:
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select * from aihub.project_jobs where project_id = :p order by created_at"), p=project_id)
    out = []
    for row in rows:
        item = dict(row)
        last = None
        with _lock:
            conn = connect()
            last = _one(conn, _qualify(
                "select status, started_at, finished_at, summary from aihub.project_job_runs"
                " where job_id = :i order by started_at desc limit 1"), i=row["id"])
        item["last_run"] = ({**last,
                             "started_at": last["started_at"].isoformat() if last.get("started_at") else None,
                             "finished_at": last["finished_at"].isoformat() if last.get("finished_at") else None}
                            if last else None)
        item["next_run_at"] = row["next_run_at"].isoformat() if row.get("next_run_at") else None
        item["last_run_at"] = row["last_run_at"].isoformat() if row.get("last_run_at") else None
        out.append(item)
    return out


def due_project_jobs(now: float | None = None) -> list[dict]:
    """Забирает задачи, которым пора запускаться, и двигает их расписание.

    Слот фиксируется строкой (!YYYY-MM-DD HH:MM по МСК): повтор тика не создаёт
    второй запуск — уникальный ключ (job_id, slot) отбивает дубль.
    """
    moment = now if now is not None else time.time()
    claimed: list[dict] = []
    with _lock:
        conn = connect()
        with transaction(conn):
            rows = _rows(conn, _qualify(
                "select * from aihub.project_jobs where enabled and next_run_at is not null"
                " and next_run_at <= to_timestamp(:n) order by next_run_at limit 20"), n=moment)
            for row in rows:
                minute, hour = _parse_daily(row["schedule"])
                local = moment + MSK_OFFSET
                slot_day = time.strftime("%Y-%m-%d", time.gmtime(local))
                slot = f"{slot_day} {hour:02d}:{minute:02d}"
                inserted = _one(conn, _qualify(
                    "insert into aihub.project_job_runs(job_id, slot, status) values (:j, :s, 'queued')"
                    " on conflict (job_id, slot) do nothing returning id"), j=row["id"], s=slot)
                if not inserted:
                    conn.run(_qualify("update aihub.project_jobs set next_run_at = to_timestamp(:n) where id = :i"),
                             n=_next_daily_run(row["schedule"], moment), i=row["id"])
                    continue
                conn.run(_qualify(
                    "update aihub.project_jobs set last_run_at = to_timestamp(:n), last_status = 'queued',"
                    " next_run_at = to_timestamp(:nx) where id = :i"),
                    n=moment, nx=_next_daily_run(row["schedule"], moment), i=row["id"])
                claimed.append({**row, "run_id": inserted["id"], "slot": slot})
    return claimed


def record_manual_run(job_id: str) -> int:
    """Пробный запуск тоже попадает в историю задачи, а не только в очередь."""
    slot = "manual:" + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    with _lock:
        conn = connect()
        with transaction(conn):
            row = _one(conn, _qualify(
                "insert into aihub.project_job_runs(job_id, slot, status) values (:j, :s, 'queued')"
                " returning id"), j=job_id, s=slot)
            conn.run(_qualify(
                "update aihub.project_jobs set last_run_at = now(), last_status = 'queued' where id = :i"),
                i=job_id)
    return int(row["id"])


STUCK_RUN_S = 600


def fallback_candidates(stuck_s: int = STUCK_RUN_S) -> list[dict]:
    """Запуски, где основной канал не взялся за работу или провалился.

    Резерв включается ровно один раз на запуск: колонка fallback_used снимает
    риск бесконечного перекладывания между каналами.
    """
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select r.id as run_id, r.job_id, r.agent_job_id, r.started_at,"
            " j.fallback_agent_id, j.fallback_profile, j.fallback_model, j.project_id,"
            " j.prompt, j.agent_id, j.fallback_used"
            " from aihub.project_job_runs r"
            " join aihub.project_jobs j on j.id = r.job_id"
            " where r.status in ('queued', 'running') and j.fallback_agent_id is not null"
            " and not j.fallback_used limit 20"))
    out = []
    for row in rows:
        job = get_job(row["agent_job_id"]) if row.get("agent_job_id") else None
        status = (job or {}).get("status")
        age = time.time() - row["started_at"].timestamp()
        failed = status in ("failed", "cancelled", "expired")
        stuck = status in (None, "queued") and age > stuck_s
        if failed or stuck:
            out.append({**row, "primary_status": status, "age_s": int(age)})
    return out


def mark_fallback(job_id: str, run_id: int) -> None:
    with _lock:
        conn = connect()
        with transaction(conn):
            conn.run(_qualify("update aihub.project_jobs set fallback_used = true,"
                              " last_status = 'fallback' where id = :i"), i=job_id)
            conn.run(_qualify("update aihub.project_job_runs set status = 'fallback' where id = :i"),
                     i=run_id)


def attach_project_run(run_id: int, agent_job_id: str) -> None:
    with _lock:
        conn = connect()
        conn.run(_qualify("update aihub.project_job_runs set agent_job_id = :a where id = :i"),
                 a=agent_job_id, i=run_id)


def finish_project_run(run_id: int, status: str, summary: str | None = None) -> None:
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "update aihub.project_job_runs set status = :s, finished_at = now(), summary = :m where id = :i"),
            s=status, m=summary, i=run_id)

def cancel_job(job_id: str) -> str:
    """queued → cancelled сразу; running → cancel_requested (подтверждает исполнитель)."""
    with _lock:
        conn = connect()
        with transaction(conn):
            job = _one(conn, "select * from aihub.jobs where id = :j for update", j=job_id)
            if not job:
                return "not_found"
            if job["status"] in ("succeeded", "failed", "cancelled", "timeout"):
                return "already_finished"
            if job["status"] == "queued":
                conn.run(_qualify("update aihub.jobs set status = 'cancelled', finished_at = now() where id = :j"), j=job_id)
                status = "cancelled"
            else:
                conn.run(_qualify("update aihub.jobs set needs_reconcile = true where id = :j"), j=job_id)
                status = "cancel_requested"
            conn.run(_qualify(
                "insert into aihub.job_events(job_id, event) values (:j, :e)"), j=job_id, e=status,
            )
            if status == "cancelled":
                from . import resources
                resources.release_for_job(conn, job_id)
        return status


def expire_leases() -> int:
    """Истёкшая аренда: попытка → unknown, задание → unknown (без автоповтора)."""
    with _lock:
        conn = connect()
        # Один оператор: попытка, задание и событие меняются вместе. Иначе результат,
        # принятый между UPDATE-ами, мог быть перетёрт статусом unknown.
        row = _one(
            conn,
            """with expired as (
                   update aihub.job_attempts a
                      set status = 'unknown', finished_at = now()
                     from aihub.jobs j
                    where a.status = 'running' and a.lease_until < now()
                      and j.id = a.job_id and j.status = 'running'
                   returning a.id, a.job_id
               ),
               jobs_upd as (
                   update aihub.jobs j
                      set status = 'unknown', needs_reconcile = true, finished_at = now()
                     from expired e
                    where j.id = e.job_id and j.status = 'running'
                   returning j.id
               ),
               res as (
                   update aihub.resource_leases
                      set lease_until = 'infinity'::timestamptz
                    where job_id in (select job_id from expired)
                   returning 1
               ),
               ev as (
                   insert into aihub.job_events(job_id, attempt_id, event)
                   select e.job_id, e.id, 'lease_expired' from expired e
                   returning 1
               )
               select (select count(*) from expired) as n""",
        )
        return int(row["n"])


# ============================================================
# 9.2: проекты, роли, чаты, вложения
# ============================================================

ROLE_IDS = ("orchestrator", "planner", "coder", "tester", "reviewer", "archivist")

# Идентификатор роли в данных — planner, как в шаблонах и назначениях; в интерфейсе
# и документах подпись «Архитектор». architect принимается только как старый алиас.
ROLE_LABELS = {
    "orchestrator": "Оркестратор",
    "architect": "Архитектор",
    "planner": "Архитектор",
    "coder": "Кодер",
    "tester": "Тестер",
    "reviewer": "Ревьюер",
    "archivist": "Архивариус",
}
ROLE_ALIASES = {"architect": "planner"}

ROLE_RU = {
    "orchestrator": "Оркестратор",
    "architect": "Архитектор",
    "coder": "Кодер",
    "tester": "Тестер",
    "reviewer": "Ревьюер",
    "archivist": "Архивариус",
}


def seed_workspace() -> dict:
    """Проект «AI Hub» и пять карточек ролей. Идемпотентно: повтор — без изменений."""
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            proj = _one(conn, _qualify("select * from aihub.projects where id = 'ai-hub'"))
            if not proj:
                conn.run(_qualify(
                    "insert into aihub.projects(id, name, host_id, workdir, settings)"
                    " values ('ai-hub', 'AI Hub', 'laptop', '', '{}'::jsonb)"))
            # Раньше роль архитектора хранилась как architect; данные переводим на
            # planner, чтобы шаблоны и назначения ссылались на один идентификатор.
            conn.run(_qualify(
                "update aihub.roles r set role_id = 'planner' where r.role_id = 'architect'"
                " and not exists (select 1 from aihub.roles p"
                " where p.project_id = r.project_id and p.role_id = 'planner')"))
            for rid in ROLE_IDS:
                ex = _one(conn, _qualify(
                    "select 1 as one from aihub.roles where project_id = 'ai-hub' and role_id = :r"), r=rid)
                if not ex:
                    conn.run(_qualify(
                        "insert into aihub.roles(project_id, role_id, agent_id, profile, model, instructions)"
                        " values ('ai-hub', :r, 'codex', null, null, '')"), r=rid)
            # Новые роли добавляются в уже существующие проекты отдельным идемпотентным проходом.
            for project_row in _rows(conn, _qualify("select id from aihub.projects")):
                for rid in ROLE_IDS:
                    conn.run(_qualify(
                        "insert into aihub.roles(project_id, role_id, agent_id, profile, model, instructions)"
                        " values (:p, :r, 'codex', null, null, '')"
                        " on conflict (project_id, role_id) do nothing"),
                        p=project_row["id"], r=rid)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_project("ai-hub")


def list_projects() -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(conn, _qualify("select * from aihub.projects order by created_at"))


def get_project(project_id: str) -> dict | None:
    with _lock:
        conn = connect()
        proj = _one(conn, _qualify("select * from aihub.projects where id = :p"), p=project_id)
        if not proj:
            return None
        proj["roles"] = _rows(conn, _qualify(
            "select * from aihub.roles where project_id = :p order by role_id"), p=project_id)
        for role in proj["roles"]:
            role["role_label"] = ROLE_LABELS.get(str(role.get("role_id") or ""),
                                                 str(role.get("role_id") or ""))
        return proj


def create_project(name: str, host_id: str = "laptop", workdir: str = "",
                   settings: dict | None = None, *, status: str = "ready",
                   request_key: str | None = None,
                   project_id: str | None = None) -> dict:
    project_id = project_id or uuid.uuid4().hex
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            conn.run(_qualify(
                "insert into aihub.projects(id, name, host_id, workdir, settings, status, request_key)"
                " values (:i, :n, :h, :w, :s::jsonb, :st, :rk)"),
                i=project_id, n=name, h=host_id, w=workdir,
                s=json.dumps(settings or {}, ensure_ascii=False), st=status, rk=request_key)
            for rid in ROLE_IDS:
                conn.run(_qualify(
                    "insert into aihub.roles(project_id, role_id, agent_id, profile, model, instructions)"
                    " values (:p, :r, 'codex', null, null, '')"), p=project_id, r=rid)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
    project = get_project(project_id)
    if project is None:
        raise RuntimeError("project row disappeared after insert")
    return project


def project_index_updated(project_id: str, head_sha: str, files: int) -> None:
    """Карточка проекта: карта обновлена по этому коммиту."""
    with _lock:
        conn = connect()
        with transaction(conn):
            conn.run(_qualify(
                "update aihub.projects set index_sha = :s, index_files = :f, indexed_at = now()"
                " where id = :i"), s=head_sha, f=files, i=project_id)


def project_by_workdir(workdir: str) -> dict | None:
    """Проект, который уже занимает эту папку (для проверки совпадения имён)."""
    if not workdir:
        return None
    with _lock:
        conn = connect()
        row = _one(conn, _qualify(
            "select id from aihub.projects where lower(workdir) = lower(:w) limit 1"),
            w=workdir)
    return get_project(row["id"]) if row else None


def project_by_request_key(key: str) -> dict | None:
    if not key:
        return None
    with _lock:
        conn = connect()
        row = _one(conn, _qualify(
            "select id from aihub.projects where request_key = :k"), k=key)
    return get_project(row["id"]) if row else None


def project_prepared(project_id: str, *, repo_root: str, git_root: str, branch: str,
                     head_sha: str, template_version: str) -> dict | None:
    """Проект готов: привязываем фактические пути, ветку и первый коммит."""
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "update aihub.projects set status = 'ready', repo_root = :rr, git_root = :gr,"
            " branch = :b, head_sha = :sha, template_version = :tv,"
            " prepared_at = now(), prepare_error = null, workdir = :w where id = :p"),
            rr=repo_root, gr=git_root, b=branch, sha=head_sha, tv=template_version,
            w=repo_root, p=project_id)
    return get_project(project_id)


def project_prepare_failed(project_id: str, error: dict) -> dict | None:
    """Подготовка не удалась: статус и причина остаются в карточке, папка не удаляется."""
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "update aihub.projects set status = 'failed', prepare_error = :e::jsonb"
            " where id = :p"),
            e=json.dumps(error, ensure_ascii=False), p=project_id)
    return get_project(project_id)


def update_project(project_id: str, name=None, host_id=None, workdir=None,
                   settings=None) -> dict | None:
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            if name is not None:
                conn.run(_qualify("update aihub.projects set name = :v where id = :p"), v=name, p=project_id)
            if host_id is not None:
                conn.run(_qualify("update aihub.projects set host_id = :v where id = :p"), v=host_id, p=project_id)
            if workdir is not None:
                conn.run(_qualify("update aihub.projects set workdir = :v where id = :p"), v=workdir, p=project_id)
            if settings is not None:
                conn.run(_qualify("update aihub.projects set settings = :v::jsonb where id = :p"),
                         v=json.dumps(settings, ensure_ascii=False), p=project_id)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_project(project_id)


def update_role(project_id: str, role_id: str, agent_id=None, profile=None,
                model=None, instructions=None, timeout_s=None) -> dict | None:
    """Назначение роли. model=None означает «сброс к значению по умолчанию профиля»."""
    role_id = ROLE_ALIASES.get(role_id, role_id)
    if role_id not in ROLE_IDS:
        raise ValueError("unknown_role")
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            if agent_id is not None:
                conn.run(_qualify(
                    "update aihub.roles set agent_id = :v where project_id = :p and role_id = :r"),
                    v=agent_id, p=project_id, r=role_id)
            if profile is not None:
                conn.run(_qualify(
                    "update aihub.roles set profile = :v where project_id = :p and role_id = :r"),
                    v=profile, p=project_id, r=role_id)
            if model is not None:
                conn.run(_qualify(
                    "update aihub.roles set model = :v where project_id = :p and role_id = :r"),
                    v=model, p=project_id, r=role_id)
            if instructions is not None:
                conn.run(_qualify(
                    "update aihub.roles set instructions = :v where project_id = :p and role_id = :r"),
                    v=instructions, p=project_id, r=role_id)
            if timeout_s is not None:
                conn.run(_qualify(
                    "update aihub.roles set timeout_s = :v where project_id = :p and role_id = :r"),
                    v=timeout_s, p=project_id, r=role_id)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_project(project_id)


def clear_role_model(project_id: str, role_id: str) -> dict | None:
    if role_id not in ROLE_IDS:
        raise ValueError("unknown_role")
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "update aihub.roles set model = null where project_id = :p and role_id = :r"),
            p=project_id, r=role_id)
        return get_project(project_id)


def clear_role_selection(project_id: str, role_id: str) -> None:
    if role_id not in ROLE_IDS:
        raise ValueError("unknown_role")
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "update aihub.roles set profile = null, model = null"
            " where project_id = :p and role_id = :r"), p=project_id, r=role_id)


def get_role(project_id: str, role_id: str) -> dict | None:
    with _lock:
        conn = connect()
        return _one(conn, _qualify(
            "select * from aihub.roles where project_id = :p and role_id = :r"), p=project_id, r=role_id)


def list_conversations(project_id: str) -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(conn, _qualify(
            "select * from aihub.conversations where project_id = :p order by created_at desc"),
            p=project_id)


def encode_cursor(row: dict) -> str:
    """Курсор страницы: время создания и id последней строки, без смещений."""
    stamp = row.get("created_at")
    iso = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp or "")
    payload = json.dumps({"t": iso, "i": row.get("id") or ""}, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    if not data.get("t") or not data.get("i"):
        return None
    return {"created_at": data["t"], "id": data["i"]}


def list_conversations_page(project_id: str, limit: int = 0, cursor: str = "",
                            query: str = "") -> dict:
    """Постраничный список разговоров: keyset по (created_at, id) плюс поиск по названию.

    Ключ на (created_at, id) не даёт пропусков и дублей, когда в проект одновременно
    добавляют новые разговоры. Пустой limit — прежнее поведение: весь список целиком.
    """
    text_query = (query or "").strip()
    like = "%" + text_query + "%"
    page_size = int(limit or 0)
    with _lock:
        conn = connect()
        total_row = _one(conn, "select count(*) as n from aihub.conversations"
                               " where project_id = :p and (:q = '' or title ilike :like)",
                         p=project_id, q=text_query, like=like)
        sql = ("select * from aihub.conversations where project_id = :p"
               " and (:q = '' or title ilike :like)")
        params = {"p": project_id, "q": text_query, "like": like}
        anchor = decode_cursor(cursor)
        if anchor:
            sql += " and (created_at, id) < (:t::timestamptz, :i::text)"
            params["t"] = anchor["created_at"]
            params["i"] = anchor["id"]
        sql += " order by created_at desc, id desc"
        if page_size > 0:
            sql += " limit :lim"
            params["lim"] = page_size + 1
        rows = _rows(conn, sql, **params)
    has_more = page_size > 0 and len(rows) > page_size
    if has_more:
        rows = rows[:page_size]
    return {
        "conversations": rows,
        "total": int((total_row or {}).get("n") or 0),
        "has_more": has_more,
        "next_cursor": encode_cursor(rows[-1]) if has_more and rows else "",
    }


def get_conversation(conversation_id: str) -> dict | None:
    with _lock:
        conn = connect()
        conv = _one(conn, _qualify(
            "select * from aihub.conversations where id = :c"), c=conversation_id)
        if conv:
            conv["project"] = get_project(conv["project_id"])
        return conv


def create_conversation(project_id: str, role_id: str = "", title: str = "") -> dict:
    cid = uuid.uuid4().hex
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "insert into aihub.conversations(id, project_id, role_id, title)"
            " values (:c, :p, :r, :t)"), c=cid, p=project_id, r=role_id, t=title)
        return {"id": cid, "project_id": project_id, "role_id": role_id, "title": title}


def pipeline_conversation(project_id: str, role_id: str) -> dict:
    """Один служебный чат роли на проект, куда падают задачи конвейера и ответы."""
    title = "Конвейер · " + ROLE_LABELS.get(role_id, role_id)
    with _lock:
        conn = connect()
        row = _one(conn, _qualify(
            "select id, project_id, role_id, title from aihub.conversations"
            " where project_id = :p and role_id = :r and title = :t"
            " order by created_at limit 1"), p=project_id, r=role_id, t=title)
        if row:
            return row
        cid = uuid.uuid4().hex
        conn.run(_qualify(
            "insert into aihub.conversations(id, project_id, role_id, title)"
            " values (:c, :p, :r, :t)"), c=cid, p=project_id, r=role_id, t=title)
        return {"id": cid, "project_id": project_id, "role_id": role_id, "title": title}


def mirror_run_message(run_id: str, role_id: str, author: str, body: str, suffix: str,
                       job_id: str | None = None, state: str = "done") -> dict | None:
    """Копия шага конвейера в чат роли. Идемпотентно по run_id+suffix."""
    if role_id not in ROLE_IDS:
        return None
    with _lock:
        run = _one(connect(), _qualify(
            "select project_id from aihub.runs where id = :i"), i=run_id)
    if not run or not run.get("project_id"):
        return None
    conv = pipeline_conversation(run["project_id"], role_id)
    return add_message(conv["id"], author, (body or "")[:8000], job_id=job_id, state=state,
                       idempotency_key=f"run:{run_id}:{suffix}")


def get_messages(conversation_id: str, limit: int = 200) -> list[dict]:
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select m.id, m.conversation_id, m.author, m.body, m.job_id, m.state, m.created_at,"
            " j.status as job_status, coalesce(j.run_snapshot->>'agent_id', j.agent_id) as job_agent_id,"
            " coalesce(j.role_id, j.run_snapshot->>'role_id') as job_role_id,"
            " j.requested_profile, j.requested_model, j.effective_model"
            " from aihub.messages m left join aihub.jobs j on j.id = m.job_id"
            " where m.conversation_id = :c order by m.id desc limit :l"),
            c=conversation_id, l=limit)
        rows.reverse()
        for m in rows:
            m["attachments"] = _rows(conn, _qualify(
                "select a.id, a.filename, a.mime, a.size_bytes, a.origin, a.metadata"
                " from aihub.attachments a"
                " join aihub.message_attachments ma on ma.attachment_id = a.id"
                " where ma.message_id = :m"), m=m["id"])
            for attachment in m["attachments"]:
                attachment["metadata"] = _json_object(attachment.get("metadata"))
        return rows


def add_message(conversation_id: str, author: str, body: str, job_id: str | None = None,
                state: str = "done", idempotency_key: str | None = None) -> dict:
    """Повтор с тем же ключом возвращает прежнее сообщение (без дубля)."""
    with _lock:
        conn = connect()
        if idempotency_key:
            ex = _one(conn, _qualify(
                "select * from aihub.messages where idempotency_key = :k"), k=idempotency_key)
            if ex:
                if ex["conversation_id"] != conversation_id or ex["author"] != author or ex["body"] != body:
                    raise ValueError("message_idempotency_conflict")
                return ex
        conn.run("begin")
        try:
            row = _one(conn, _qualify(
                "insert into aihub.messages(conversation_id, author, body, job_id, state, idempotency_key)"
                " values (:c, :a, :b, :j, :s, :k)"
                " on conflict (idempotency_key) where idempotency_key is not null do nothing returning *"),
                c=conversation_id, a=author, b=body, j=job_id, s=state, k=idempotency_key)
            if not row and idempotency_key:
                row = _one(conn, _qualify(
                    "select * from aihub.messages where idempotency_key = :k"), k=idempotency_key)
                if not row or row["conversation_id"] != conversation_id or row["author"] != author or row["body"] != body:
                    raise ValueError("message_idempotency_conflict")
            conn.run("commit")
            return row
        except Exception:
            conn.run("rollback")
            raise


def get_message_by_idempotency_key(key: str) -> dict | None:
    with _lock:
        conn = connect()
        return _one(conn, _qualify(
            "select * from aihub.messages where idempotency_key = :k"), k=key)


def set_message_job(message_id: int, job_id: str, state: str = "running") -> None:
    with _lock:
        conn = connect()
        conn.run(_qualify("update aihub.messages set job_id = :j, state = :s where id = :m"),
                 j=job_id, s=state, m=message_id)


def finish_message_by_job(job_id: str, body: str, state: str) -> dict | None:
    """Ответ агента дописывается в чат тем же сообщением-«ожиданием». Идемпотентно."""
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            msg = _one(conn, _qualify(
                "select * from aihub.messages where job_id = :j order by id desc limit 1"), j=job_id)
            if not msg:
                conn.run("rollback")
                return None
            if msg["state"] not in ("running", "queued"):
                conn.run("rollback")
                return msg
            conn.run(_qualify("update aihub.messages set body = :b, state = :s where id = :m"),
                     b=body, s=state, m=msg["id"])
            conn.run("commit")
            msg["body"] = body
            msg["state"] = state
            return msg
        except Exception:
            conn.run("rollback")
            raise


def create_attachment(project_id: str, origin: str, filename: str, mime: str,
                      data: bytes, host_id: str | None = None,
                      host_path: str | None = None, metadata: dict | None = None) -> dict:
    import base64 as _b64
    aid = uuid.uuid4().hex
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "insert into aihub.attachments(id, project_id, origin, filename, mime, size_bytes,"
            " storage, host_id, host_path, metadata)"
            " values (:i, :p, :o, :f, :m, :s, :st, :h, :hp, :md::jsonb)"),
            i=aid, p=project_id, o=origin, f=filename, m=mime, s=len(data),
            st=_b64.b64encode(data).decode(), h=host_id, hp=host_path,
            md=json.dumps(metadata or {}))
        return {"id": aid, "filename": filename, "mime": mime, "size_bytes": len(data),
                "origin": origin, "host_path": host_path, "metadata": metadata or {}}


def list_attachments(project_id: str) -> list[dict]:
    """Файлы проекта для выбора в контексте. Хранилище не отдаём — только метаданные."""
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select id, filename, mime, size_bytes, origin, metadata, created_at from aihub.attachments"
            " where project_id = :p order by created_at desc limit 200"), p=project_id)
        for attachment in rows:
            attachment["metadata"] = _json_object(attachment.get("metadata"))
        return rows


def get_attachment(attachment_id: str) -> dict | None:
    with _lock:
        conn = connect()
        row = _one(conn, _qualify("select * from aihub.attachments where id = :a"), a=attachment_id)
        if row:
            row["metadata"] = _json_object(row.get("metadata"))
        return row


def link_attachment(message_id: int, attachment_id: str) -> None:
    with _lock:
        conn = connect()
        conn.run(_qualify(
            "insert into aihub.message_attachments(message_id, attachment_id)"
            " values (:m, :a) on conflict do nothing"), m=message_id, a=attachment_id)


def get_conversation_context(conversation_id: str, tail: int = 20) -> list[dict]:
    """Recent messages and only their project-owned attachment links."""
    with _lock:
        conn = connect()
        rows = _rows(conn, _qualify(
            "select m.id, m.author, m.body, c.project_id from aihub.messages m"
            " join aihub.conversations c on c.id = m.conversation_id"
            " where m.conversation_id = :c order by m.id desc limit :t"),
            c=conversation_id, t=tail)
        rows.reverse()
        for message in rows:
            message["attachments"] = _rows(conn, _qualify(
                "select a.id, a.project_id, a.filename, a.mime, a.size_bytes, a.origin"
                " from aihub.message_attachments ma"
                " join aihub.attachments a on a.id = ma.attachment_id"
                " where ma.message_id = :m and a.project_id = :p"
                " order by a.created_at desc, a.id desc"),
                m=message["id"], p=message["project_id"])
        return rows


def refresh_default_conversation_title(conversation_id: str) -> str | None:
    """Name a default conversation from its first user message; preserve custom titles."""
    with _lock:
        conn = connect()
        updated = _one(conn, _qualify(
            "with first_user as ("
            " select regexp_replace(btrim(body), '\\s+', ' ', 'g') as body"
            " from aihub.messages where conversation_id = :c and author = 'user'"
            " order by id asc limit 1"
            "), renamed as ("
            " update aihub.conversations c set title = case"
            " when char_length(first_user.body) > 64 then left(first_user.body, 61) || '…'"
            " else first_user.body end"
            " from first_user where c.id = :c"
            " and lower(btrim(coalesce(c.title, ''))) in ('', 'новый чат', 'new chat')"
            " returning c.title"
            ") select title from renamed"), c=conversation_id)
        if updated:
            return updated["title"]
        current = _one(conn, _qualify(
            "select title from aihub.conversations where id = :c"), c=conversation_id)
        return current.get("title") if current else None


def create_chat_job(agent_id: str, prompt: str, prompt_sha: str, timeout_s: int,
                    idempotency_key: str | None, max_queue: int | None,
                    requested_model: str | None, requested_profile: str | None,
                    project_id: str, conversation_id: str, role_id: str,
                    run_snapshot: dict, reasoning_effort: str | None = None) -> tuple[dict, bool]:
    """Запуск из чата: job + связь с проектом/ролью/снимок назначения."""
    workspace = dict(run_snapshot)
    workspace.update(project_id=project_id, conversation_id=conversation_id, role_id=role_id)
    return create_job(agent_id, prompt, prompt_sha, timeout_s, idempotency_key,
                      max_queue, requested_model, requested_profile, workspace,
                      reasoning_effort=reasoning_effort)


def _iso(value):
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def workspace_receipt(key: str) -> dict | None:
    """Durable receipt операции по её idempotency key."""
    if not key:
        return None
    with _lock:
        conn = connect()
        row = _one(conn, "select * from aihub.workspace_receipts where idempotency_key = :k", k=key)
    if not row:
        return None
    result = row.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    return {
        "idempotency_key": row.get("idempotency_key"),
        "op": row.get("op"),
        "host_id": row.get("host_id"),
        "job_id": row.get("job_id"),
        "args_digest": row.get("args_digest"),
        "status": row.get("status"),
        "attempt_id": row.get("attempt_id"),
        "result_digest": row.get("result_digest"),
        "result": result,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def record_workspace_receipt(key: str, op: str, host_id: str, job_id: str | None,
                             args_digest: str, status: str, result: dict | None = None,
                             result_digest: str | None = None,
                             attempt_id: str | None = None) -> dict | None:
    """Write a receipt once, bound to one job/attempt; conflicting replays fail closed."""
    if not key:
        return None
    with _lock:
        conn = connect()
        encoded_result = json.dumps(result, ensure_ascii=False) if result is not None else None
        conn.run("begin")
        try:
            if status not in {"pending", "applied", "failed", "unknown", "late"}:
                raise ValueError("workspace_receipt_status_invalid")
            if attempt_id:
                linked_attempt = _one(conn,
                    "select job_id from aihub.job_attempts where id = :a", a=attempt_id)
                if not linked_attempt or linked_attempt.get("job_id") != job_id:
                    raise ValueError("workspace_receipt_attempt_link_mismatch")
            row = _one(conn, _qualify(
                "select * from aihub.workspace_receipts where idempotency_key = :k for update"), k=key)
            if not row:
                conn.run(_qualify(
                    "insert into aihub.workspace_receipts(idempotency_key, op, host_id, job_id,"
                    " args_digest, status, result, result_digest, attempt_id)"
                    " values (:k, :o, :h, :j, :d, :s, :r::jsonb, :rd, :a)"),
                    k=key, o=op, h=host_id, j=job_id, d=args_digest, s=status,
                    r=encoded_result, rd=result_digest, a=attempt_id)
            else:
                identity = (row.get("op"), row.get("host_id"), row.get("job_id"), row.get("args_digest"))
                if identity != (op, host_id, job_id, args_digest):
                    raise ValueError("workspace_receipt_identity_conflict")
                if attempt_id is None:
                    if row.get("status") != status or row.get("attempt_id") is not None:
                        raise ValueError("workspace_receipt_terminal_conflict")
                elif row.get("attempt_id") is None and row.get("status") == "pending":
                    conn.run(_qualify(
                        "update aihub.workspace_receipts set status = :s, result = :r::jsonb,"
                        " result_digest = :rd, attempt_id = :a, updated_at = now()"
                        " where idempotency_key = :k and attempt_id is null and status = 'pending'"),
                        s=status, r=encoded_result, rd=result_digest, a=attempt_id, k=key)
                else:
                    existing_result = row.get("result")
                    if isinstance(existing_result, str):
                        try:
                            existing_result = json.loads(existing_result)
                        except ValueError:
                            existing_result = None
                    if (row.get("attempt_id") != attempt_id or row.get("status") != status
                            or existing_result != result or row.get("result_digest") != result_digest):
                        raise ValueError("workspace_receipt_attempt_conflict")
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
    return workspace_receipt(key)
