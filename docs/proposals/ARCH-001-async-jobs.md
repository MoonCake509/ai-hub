# ARCH-001: асинхронные задачи, история прогонов, адаптеры агентов

Статус: предложение на ревью архитектору
Дата: 2026-11-06
Автор: ведущий (ассистент), исполнитель — он же

## 1. Что уже работает (факты, не планы)

- LiteLLM на `:4000`, Postgres 16, оба в Docker внутри WSL2 Ubuntu 24.04 на сервере `win-v76qc2g60q1` (R:\AI-Hub).
- Подключены два OpenAI-совместимых провайдера: `router.cheap` (76 моделей) и `claudehub` (46). Схема — wildcard
  `router.cheap/*` → `openai/*`, плюс 6 алиасов. Проверено живыми ответами.
- `hub-agent` на `:8000` (FastAPI): `/health`, `/agents`, `/agents/{id}`, `/models`, `/v1/chat/completions`.
  Авторизация — единый `HUB_TOKEN`.
- Адаптеры `codex` и `qwen-code` работают сквозным вызовом: `POST /agents/codex/run` → ответ за 39 с,
  `POST /agents/qwen-code/run` → за 23 с.
- Доставка заданий на ноутбук — **pull**: ноутбук опрашивает `GET /bridge/jobs`, выполняет, отдаёт
  `POST /bridge/result`. Входящие порты на ноутбуке не нужны (был заблокирован брандмауэр; сейчас брандмауэр
  отключён, но схему менять не спешим — см. вопрос Q4).
- Всё в git (`R:\AI-Hub`, 4 коммита), секреты только в `infra/.env`.

## 2. Проблема текущей схемы

`POST /agents/{id}/run` синхронный: HTTP-соединение держится всё время работы агента (Codex — десятки секунд,
большие задачи — до 15 минут). Следствия:

1. Обрыв Wi-Fi = потерянный результат: задание выполнилось, а ответ ушёл в никуда.
2. Нет истории: что запускали, чем закончилось, сколько заняло — нигде не хранится.
3. Нет отмены и нет статуса: клиент не знает, выполняется задача или зависла.
4. Очередь в памяти процесса: рестарт контейнера теряет всё.
5. Параллелизм не ограничен ничем, кроме блокировки в мосте (один прогон на агента).

## 3. Предложение A. Асинхронная модель задач

```
POST /agents/{id}/run        -> 202 {"job_id": "...", "status": "queued"}
GET  /jobs/{job_id}          -> {"status": "queued|running|succeeded|failed|timeout|cancelled", "result": {...}}
GET  /jobs?status=&agent=&limit=  -> список (для UI и разбора)
POST /jobs/{job_id}/cancel   -> 202 (мягкая отмена: помечаем, воркер увидит на следующем шаге)
```

Правила:

- `POST` принимает `Idempotency-Key` (или хеш `agent+prompt+opts`), чтобы повтор при обрыве не плодил дубли.
- Переходы состояний только вперёд; каждое изменение пишется в `job_events` (аудит).
- Воркер забирает задание на срок аренды (`lease_seconds`, по умолчанию 300). Нет `heartbeat` — задание
  возвращается в очередь (at-least-once). Значит, операция должна быть повторяемой; для Codex повтор
  допустим, но должен быть виден в истории (`attempt`).
- Таймаут на уровне задания, отдельно от таймаута процесса.
- Ограничение параллелизма: `max_parallel` на агента (Codex = 1, Qwen = 1).
- Ожидание результата остаётся удобным: `GET /jobs/{id}?wait=60` — long-poll, чтобы клиенту не крутить цикл.

## 4. Предложение B. История в Postgres

Минимальная схема:

```sql
create table agents (
  id text primary key, name text, host text, transport text,
  bridge_agent text, max_parallel int default 1, enabled boolean default true
);

create table jobs (
  id uuid primary key,
  agent_id text references agents(id),
  status text not null,
  prompt text not null,
  timeout_s int not null,
  attempt int not null default 1,
  worker text,
  idempotency_key text,
  created_at timestamptz default now(),
  started_at timestamptz,
  finished_at timestamptz,
  output text,
  error text,
  usage jsonb,
  meta jsonb
);
create unique index jobs_idem on jobs(idempotency_key) where idempotency_key is not null;
create index jobs_status_created on jobs(status, created_at desc);

create table job_events (
  id bigserial primary key,
  job_id uuid references jobs(id) on delete cascade,
  at timestamptz default now(),
  event text not null,   -- queued|leased|heartbeat|succeeded|failed|timeout|cancelled
  payload jsonb
);
```

Открытые вопросы: хранить ли полный `prompt` (он может содержать чувствительные данные) — предлагаю хранить
и обрезать до N КБ, с флагом `truncated`; ретеншн — 90 дней для `jobs`, `job_events` — 30 дней.

Отдельно: `agents` в БД или по-прежнему в `config/agents.yaml`? Предлагаю YAML как источник правды для репозитория,
а БД — только для истории и рантайм-состояния (иначе правки конфига требуют SQL).

## 5. Предложение C. Адаптеры OpenClaw и Hermes

Единый контракт адаптера:

```python
class Adapter(Protocol):
    id: str
    host: str            # server | laptop
    max_parallel: int
    def run(self, prompt: str, opts: dict) -> dict: ...   # {ok, output, usage, error, elapsed}
    def cancel(self, job_id: str) -> bool: ...
```

- **OpenClaw** — CLI `openclaw agent -m <prompt> --json`, работает и на ноутбуке, и на сервере (gateway поднят).
  Значит, адаптер можно поставить серверным: один `subprocess`, без моста.
- **Hermes** — CLI/локальный HTTP. На сервере тоже есть (Hermes + OpenClaw на сервере = claudehub/deepseek-v4.1-flash).
  Предлагаю серверный адаптер через CLI в неинтерактивном режиме.
- **Qwen** — ставим десктоп-приложение на сервер (в работе), тогда адаптер переезжает с ноутбука на сервер:
  CDP-порт 9222, тот же `qwen.js`, но без сетевого моста.
- **Codex** — остаётся на ноутбуке (на сервере не авторизован), транспорт pull.

Для серверных агентов мост не нужен: `subprocess` внутри контейнера или на хосте (нужно решить: если агент —
GUI-приложение, контейнер не подходит, запускать на хосте Windows через отдельный сервис).

## 6. Вопросы архитектору

1. Асинхронная модель vs синхронная с long-poll: не усложняем ли MVP? Может, достаточно `?wait=60` и без `/jobs`?
2. Где хранить очередь: Postgres (`select ... for update skip locked`) или Redis? Redis — ещё один контейнер,
   Postgres уже есть. Что выберешь и почему?
3. At-least-once с арендой и heartbeat: приемлемо ли для Codex, учитывая, что повторный прогон стоит денег?
4. Pull-мост оставить (брандмауэр ноутбука теперь отключён, но IP динамический) или перейти на push с сервера?
5. Хранить ли полные промпты и ответы в БД, или только метаданные + ссылку на файл?
6. Адаптеры: единый контракт в одном процессе хаба или отдельные воркер-процессы на каждом хосте?
7. Как ограничивать стоимость: лимит токенов на задание, дневной бюджет на агента/модель?
8. Что считаем критерием готовности этапа: какие 3–5 проверок должны проходить, чтобы этап закрывался?

## 7. Ограничения, которые нельзя обойти

- LAN-only, внешних портов нет; сервер за Wi-Fi с обрывами (кабель пока недоступен).
- Ноутбук без прав администратора (задачи планировщика не создать — только Startup).
- Один пользователь, без мультитенантности и ролей.
- Секреты только в `infra/.env`; в БД и логи не попадают.
