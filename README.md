# AI-Hub — серверная платформа агентов

Платформа-оркестратор для программного доступа к агентам (Codex, OpenClaw, Hermes, Qwen, Ollama).
Шлюз — LiteLLM, ядро — hub-agent, доступ — веб-UI. Реализация по ROADMAP v2.

## Где развёрнуто

- Сервер: `win-v76qc2g60q1`, Windows 10 Pro 19045, i5-4430 (4/4), 32 ГБ RAM.
- Рабочее пространство: **`R:\AI-Hub`** (этот каталог).
- Linux-среда: WSL2, дистрибутив **AIHub** (Ubuntu 24.04 LTS), диск `R:\WSL\AIHub`.
- Доступ: LAN `192.168.31.95`, Tailscale `100.104.134.6`.

## Почему рабочее пространство на R:

Физические диски сервера:

- **Диск 0 — WDC WD10EZEX-08RKKA0, 932 ГБ, SATA HDD 7200 rpm (основной).**
  Разделы: `C:` 196 ГБ (свободно 32.8 ГБ — система Windows), `R:` 735 ГБ (свободно 220.8 ГБ).
- **Диск 1 — HGST HTS545050A7E680, 466 ГБ, SATA HDD 5400 rpm (ноутбучный, медленный).**
  Раздел: `D:` 465 ГБ (свободно 299.5 ГБ).
- `F:`, `G:` — USB-картридеры, пустые.

Причины выбора `R:`:

1. `R:` — раздел **основного** диска, того же, где система. Он не отключается и не зависит от внешних/дополнительных накопителей.
2. `C:` не подходит: свободно 32.8 ГБ. Один WSL-vhdx + слои Docker-образов (Postgres, LiteLLM и др.) съедят этот запас и положат систему.
3. `D:` не подходит: это второй, более медленный диск (5400 rpm). Рабочее пространство и виртуальные диски на нём дадут просадку I/O на сборках и БД, а также привяжут проект к состоянию второго накопителя.

Поэтому и рабочее пространство (`R:\AI-Hub`), и виртуальный диск WSL (`R:\WSL\AIHub`) размещены на основном диске.

## Что уже установлено

| Компонент | Версия |
|---|---|
| WSL | 3.0.3 (ядро 6.18.54.1-microsoft-standard-WSL2) |
| Ubuntu | 24.04 LTS (дистрибутив `AIHub`, systemd) |
| Docker Engine | 29.1.3 (overlayfs) |
| Docker Compose | 2.40.3 |
| git / jq / make / curl | 2.43.0 / 1.7.1 / 4.3 / 8.x |

Пользователь внутри WSL: `hub` (sudo без пароля).

## Особенности окружения

- **`C:\Windows\System32\wsl.exe` неисправен** — на любую команду возвращает «Отказано в доступе».
  Рабочий бинарь: **`C:\Program Files\WSL\wsl.exe`**. Все скрипты используют полный путь.
- Установка дистрибутива из каталога Microsoft падает с `WININET_E_INVALID_SERVER_RESPONSE`
  (канал рвёт соединения к CDN). Рабочий способ: скачивание rootfs с `cloud-images.ubuntu.com`
  + `wsl --import`.
- Крупные загрузки на сервере обрываются — качать только с докачкой (`curl -C -`) и сверкой размера.
- Внутри WSL рабочее пространство доступно как `/mnt/r/AI-Hub`, из Windows — `R:\AI-Hub`.

## Состояние

- [x] WSL2 + Ubuntu 24.04 на основном диске
- [x] Docker Engine + Compose, проверено `docker run hello-world`
- [x] LiteLLM gateway (порт 4000, Postgres подключён)
- [x] hub-agent (FastAPI, порт 8000)
- [ ] Веб-UI
- [x] Адаптеры Codex и Qwen (pull-воркеры, ноутбук + сервер)
- [x] Асинхронные задания + история прогонов (Postgres)
- [ ] Адаптеры OpenClaw и Hermes

## Стек (infra/)

- `infra/docker-compose.yml` — `postgres:16-alpine` + `ghcr.io/berriai/litellm:main-stable`.
- `infra/.env` — секреты (в git не попадает), генерируются локально.
- `infra/litellm/config.yaml` — карта моделей, подключается в контейнер.
- Адрес шлюза: `http://192.168.31.95:4000` (LAN). Проверка: `/health/liveliness`, `/health/readiness`.

## Эндпоинты

- `http://192.168.31.95:8000/health` — живость ядра.
- `http://192.168.31.95:8000/agents` — реестр агентов (`config/agents.yaml`).
- `http://192.168.31.95:8000/models` — прокси к LiteLLM `/v1/models`.
- `http://192.168.31.95:4000` — LiteLLM: `/health/liveliness`, `/health/readiness`, `/v1/*`.

## Модели (LiteLLM)

Два OpenAI-совместимых провайдера подключены в `infra/litellm/config.yaml`:

- `router.cheap` — 76 моделей, доступ через префикс: `router.cheap/gpt-6.1-sol`.
- `claudehub` — 46 моделей: `claudehub/claude-opus-5`.

Алиасы: `gpt-6.1-sol`, `deepseek-v4.1-flash`, `qwen3.8-max`, `claude-opus-5`, `claude-sonnet-5`, `gemini-4-argon`.

```
curl -X POST http://192.168.31.95:8000/v1/chat/completions \
  -H "Authorization: Bearer $HUB_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"ping"}]}'
```

Ключи провайдеров и `HUB_TOKEN` — только в `infra/.env` (в git не попадает).

## Агенты

- `GET /agents` — реестр (`config/agents.yaml`).
- Реализованы: `codex` (ноутбук), `qwen-code` (Qwen Desktop на ноутбуке), `qwen-server` (Qwen Desktop на сервере, CDP 9333).

Связь **pull**: воркер сам опрашивает `GET /bridge/jobs?worker=<имя>&agents=<список>`,
забирает только те задания, которые умеет обслуживать, выполняет и отдаёт результат
`POST /bridge/result`. Входящие порты на ноутбуке и серверном хосте не нужны.

- Ноутбук: `C:\Users\lavy\aihub-bridge\worker_v2.py`, автозапуск — `run-bridge.cmd` (Startup),
  конфиг `hub.txt`, `token.txt`.
- Серверный хост: `C:\Users\user\aihub-worker\worker_v2.py`, задача планировщика `AIHub-Worker`,
  обслуживает `qwen-server` через `node qwen.js` и CDP-порт 9333.

## Задания (async)

Запуск асинхронный: долгий HTTP не держится, обрыв связи не теряет результат.

```
POST /agents/{id}/run
  {"prompt": "...", "timeout": 300, "idempotency_key": "любая-строка"}
  -> 202 {"job_id": "...", "status": "queued", "created": true}
```

- `GET /jobs/{id}` — состояние, попытки, вывод: `queued | running | succeeded | failed | cancelled | timeout | unknown`.
- `GET /jobs?limit=50&status=` — история прогонов (Postgres, схема `aihub`).
- `POST /jobs/{id}/cancel` — отмена (queued → cancelled; running помечается, результат отбрасывается).

Гарантии:

- **Идемпотентность.** Тот же `idempotency_key` с тем же телом возвращает тот же `job_id`
  (`created: false`); с другим телом — `409`.
- **Аренда.** Воркер берёт задание в аренду (`LEASE_SECONDS`, по умолчанию 300) и продлевает её
  heartbeat-ом. Если связь пропала дольше аренды, попытка и задание становятся `unknown`
  с `needs_reconcile: true` — **автоматического повтора нет** (агент мог успеть выполнить работу).
- **Outbox.** Воркер сначала пишет результат в локальный `outbox/`, затем досылает его до
  подтверждения хаба; после перезапуска недосланное отправляется повторно.
- **Рестарт хаба** не теряет задания: очередь и история в Postgres.

Таблицы: `aihub.jobs`, `aihub.job_attempts`, `aihub.job_events` (события `created`, `leased`,
`lease_expired`, `finished`, `cancelled`).
