# IMPLEMENTATION-BASELINE — фактическое состояние перед поставками A–E

Снято: 10.11.2026, 02:15–02:30 MSK. Исполнитель: Hermes (по заданию из `AI-Hub-architecture-roadmap-2026-11-10.md`).
Документ фиксирует наблюдения, а не намерения. Всё ниже проверено командами на сервере `win-v76qc2g60q1`.

## 1. Размещение и окружение

| Что | Факт |
|---|---|
| Чекаут | `R:\AI-Hub` (в WSL — `/mnt/r/AI-Hub`) |
| Git | `master`, HEAD `bbb3f5a` «fix: resolve Codex updates and retire legacy poller» (09.11.2026 23:04) |
| Remote | отсутствует (локальный репозиторий, публикация невозможна) |
| ОС | Windows 10 Pro 19045, WSL2 дистрибутив `AIHub` (Ubuntu 24.04, python 3.12.3, bash 5.2.21) |
| Docker | Engine 29.x внутри WSL; `docker.exe` на хосте недоступен — только `"C:\Program Files\WSL\wsl.exe" -d AIHub -- bash -lc "..."` |
| RAM / диск | 15 ГиБ в WSL, `R:` 209 ГиБ свободно |
| Хаб | `aihub-hub` :8000 и :8443, health `{"status":"ok","version":"0.3.0","db":true}` |
| Стенд | `aihub-hub-test` :8100 + `aihub-postgres-test` (схема `aihub_test`) |
| Прочее | `aihub-litellm` :4000, `aihub-postgres` (роль `llmproxy`, БД `litellm`, схема `aihub`), `aihub-ui-fix-app` |

Доступ по SSH: `User@192.168.31.95` (LAN). Tailscale-адрес `100.104.134.6` сейчас не отвечает (banner exchange timeout), в работе использовать LAN. `scp` на этот хост не работает — передача файлов через stdin/`type`.

## 2. Рабочее дерево и чужие изменения

`git status --short`:

```
 M infra/docker-compose.test.yml
?? infra/stand92.bat
?? models/
?? tools/netwatchdog.ps1
```

Ничего из этого не сброшено, не закоммичено и не изменено.

Важное расхождение в `infra/docker-compose.test.yml` (незакоммиченная правка): комментарий утверждает, что bind-mount идёт в отдельную копию `R:\AI-Hub-stand`, но фактический volume — `../hub/app:/app/app:ro`, то есть **рабочее дерево**. Правка `hub/app` меняет и стенд, и прод. Изоляция тестового прогона сейчас держится только на отдельной БД, но не на отдельной копии кода. Учесть в CORE-02/изоляции runner.

Размер репозитория: 65 отслеживаемых файлов, ~10,3 тыс. строк в коде/доках. Крупнейшие: `hub/app/main.py` (1272), `hub/app/db.py` (1016), `web/app.js` (849), `web/index.html` (153), `web/styles.css`.

## 3. API: что реально существует

35 маршрутов в `hub/app/main.py`:

- служебные: `GET /health`, `GET /agents`, `GET /agents/{id}`, `GET /agent-profiles/{agent_id}`, `GET /models`, `POST /v1/chat/completions`;
- задания: `POST /agents/{id}/run`, `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{job_id}/events`, `POST /jobs/{id}/cancel`, `GET /late-results`;
- мост воркеров: `GET /bridge/jobs`, `POST /bridge/heartbeat`, `POST /bridge/result`;
- проекты и роли: `GET/POST /projects`, `GET/PATCH /projects/{id}`, `PATCH /projects/{id}/roles/{role_id}`;
- чаты: `GET/POST /projects/{id}/conversations`, `GET /conversations/{id}`, `GET /conversations/{id}/messages`, `POST /conversations/{id}/send`;
- вложения: `GET/POST /projects/{id}/attachments`, `GET /attachments/{id}`, `GET /attachments/{id}/preview`;
- качество каналов: `GET /quality/suites`, `GET/PATCH /quality/channels`, `POST/GET /quality/runs`, `GET /quality/runs/{run_id}`.

Чего из плана нет (нужно создавать в C–E):
`/projects/{id}/context`, `/projects/{id}/context/preview`, `/workflow-templates`, `/projects/{id}/workflow-runs`, `/workflow-runs/{id}` (+events/tasks/pause/resume/cancel/decisions), `/workflow-steps/{id}/context`, `/projects/{id}/artifacts`, `/artifacts/{id}`, `/channels/diagnostics`, `/diagnostic-runs`, `/channel-evaluations`.
Пагинации по курсору и параметра `q` у списка чатов тоже нет: `GET /projects/{id}/conversations` отдаёт весь список.

## 4. Схема БД (схема `aihub`)

Фактические таблицы (14): `jobs`, `job_attempts`, `job_events`, `late_results`, `projects`, `roles`, `conversations`, `messages`, `attachments`, `message_attachments`, `quality_runs`, `quality_run_items`, `quality_channels`, `quality_channel_audit`.

`jobs` содержит: `id, agent_id, prompt, prompt_sha, timeout_s, status, idempotency_key, attempts, needs_reconcile, created_at, started_at, finished_at, result_output, result_error, result_usage(jsonb), lease_until, worker, requested_model, effective_model, requested_profile, project_id, conversation_id, role_id, run_snapshot(jsonb)`.

Живые данные на момент снятия: 132 задания, 3 проекта, 4 чата, 14 сообщений. Миграции — только добавочные.

Чего из плана нет: `workflow_templates`, `workflow_runs`, `workflow_tasks`, `workflow_steps`, `workflow_events`, `context_packets`, `project_artifacts`, `budget_reservations`, `resource_leases`, `diagnostic_runs`, `diagnostic_observations`, `channel_baselines`, `channel_evaluations`.

## 5. Адаптеры и воркеры

В `config/agents.yaml` 9 карточек: `codex`, `openclaw-laptop`, `hermes-laptop`, `qwen-code`, `llm-server`, `xiaomi-ocr-server`, `hermes-server`, `openclaw-server`, `qwen-server`.

| id | transport | host | заявлено |
|---|---|---|---|
| codex | cli | laptop | workdir, выбор модели |
| openclaw-laptop | cli | laptop | без выбора модели (обоснование: отдельный запуск > 300 с) |
| hermes-laptop | cli | laptop | workdir, выбор модели |
| qwen-code | cdp | laptop | без workdir и выбора модели |
| llm-server | http | server | выбор модели |
| xiaomi-ocr-server | http | server | выбор модели |
| hermes-server | cli | server | без workdir |
| openclaw-server | cli | server | без workdir |
| qwen-server | cdp | server | без workdir |

Все девять помечены `status: verified`. Это статический флаг реестра, а не свежее наблюдение: ни времени, ни источника, ни TTL, ни разделения configured/compatibility/availability/quality. Ровно то, что должна заменить DIAG-01.

Матрицы реальных возможностей нет: нет отдельных полей для patch, работы в cwd, запуска команд, файловых операций, usage, streaming, подтверждённой остановки. Есть только `supports_workdir` и `model_selection`. ADAPTER-01 должна подтвердить каждую возможность отдельным вызовом.

Воркеры pull-модели: ноутбук `C:\Users\lavy\aihub-bridge\worker_v2.py` (агент `codex`), сервер `C:\Users\user\aihub-worker` (агент `qwen-server` через CDP 9333). Задача планировщика `AIHub-Backup` существует, ближайший запуск 10.11 03:00, состояние «готово».

## 6. Тестовый baseline (реально выполнено)

Выполнено на сервере 10.11.2026. Прежнее число 58 не принималось на веру — набор запущен заново.

| Набор | Команда | Результат |
|---|---|---|
| Модульные backend-тесты | `python -m pytest -q tests/test_bridge_codex_executable.py tests/test_conversation_context.py tests/test_image_context_api.py tests/test_image_validation.py tests/test_outbox_delivery.py tests/test_quality.py tests/test_reported_model.py tests/test_server_ocr_prompt.py` | **58 passed, 20 subtests passed, 1.63 s** |
| Живые тесты стенда :8100 | `pytest -q tests/test_911.py tests/test_913.py tests/test_isolation.py` с `HUB_TOKEN` | **8 passed, 4.48 s** |
| Контрактные (идемпотентность, границы timeout, лимит prompt, duplicate/late, конкурентная очередь) | `contract_tests.py` против :8100 | **ALL CONTRACT TESTS PASSED** |
| DOM-регрессии фронтенда | `node tests/dom-*.cjs` (5 файлов) | **5 из 5 прошли** |
| `tests/test_server_models_runtime.py` | требует стенд :8201 | **не прогонялся**: порт закрыт, стенда нет |

Чтобы модульные тесты вообще запускались на хосте, пришлось поставить в user-site python хоста `pg8000`, `scramp`, `asn1crypto`, `fastapi` и `starlette==0.41.3` из `hub/wheels` (на хосте стоял starlette 1.7.0, несовместимый с fastapi 0.115.6). Это окружение хоста, не репозиторий; воспроизводимого `requirements-dev` пока нет — добавить в C.

Стенд поднимался командой `docker compose -p aihub-test -f infra/docker-compose.test.yml --env-file .env up -d --build`, health на :8100 отвечает `db:true`. Изоляция кода стенда от рабочего дерева не подтверждена (см. §2).

## 7. Расхождения с планом и риски

1. **`verified` недостоверен** — статический флаг у 9 агентов, без свежести. Первая задача B.
2. **Стенд не изолирован по коду** — mount `../hub/app` вместо копии, комментарий обещает обратное.
3. **Нет воспроизводимого test-окружения** — модульные тесты зависят от пакетов, вручную поставленных на хост.
4. **Нет remote** — внешний PR невозможен; выпуск = локальный review bundle.
5. **Нет cap-ов и бюджетов** — `MAX_QUEUE`, `MAX_PROMPT_CHARS`, `MAX_RESULT_CHARS`, `LEASE_SECONDS` есть; вызовов/токенов/времени на run и циклов исправления нет.
6. **Нет изоляции исполнения** — единственный test runner отсутствует; `cwd` у адаптеров не sandbox.
7. **Нет pagination/поиска чатов** — список отдаётся целиком, левая панель без собственного scroll-контракта (проверить в UX-02/03 на 50+ чатах).
8. **`test_server_models_runtime` не проверен** — стенд :8201 не поднят.
9. **Живые данные в проде** — 132 задания; любые изменения схемы только добавочные, с бэкапом `tools/backup.sh`.

## 8. Порядок работ (подтверждён пользователем)

START-00 (этот документ) → A: UX-01…03 → B: DIAG-01…03, Q-01 → C: CORE-01…03, ADAPTER-01 → D: CTX-01…02, FLOW-01 → E: FLOW-02…04. F–I — после отдельного решения.

Источники для следующего шага: `web/index.html`, `web/styles.css`, `web/app.js` (UX); `hub/app/main.py`, `config/agents.yaml` (DIAG/ADAPTER); `hub/app/db.py`, `infra/docker-compose*.yml`, `agents/*/worker_v2.py` (CORE).