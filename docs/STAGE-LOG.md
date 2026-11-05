# Журнал этапов

Формат: ЭТАП → ЗАДАЧА → СДЕЛАНО → КАК ПРОВЕРЕНО → ОШИБКИ.

## Этап 0. Серверная среда (2026-11-05 — 2026-11-06)

**Задача:** подготовить сервер под AI-Hub: Linux-среда, Docker, рабочее пространство.

**Сделано**
- Рабочее пространство `R:\AI-Hub` и диск WSL `R:\WSL\AIHub` — на основном диске (обоснование в README).
- WSL 3.0.3 (MSI), ядро 6.18.54.1; Ubuntu 24.04 LTS импортирован как дистрибутив `AIHub`.
- systemd, пользователь `hub` (sudo без пароля, группа docker).
- Docker Engine 29.1.3 + Compose 2.40.3 внутри Ubuntu; Docker Desktop не используется.
- Удалён cloud-init, замаскированы snapd/apport/landscape/ua-reboot — устранено зависание загрузки.

**Как проверено**
- `wsl --import` → `--list --verbose`: AIHub, version 2.
- `systemctl is-system-running` → running; `systemctl is-active docker` → active.
- `docker run --rm hello-world` → «Hello from Docker!».
- Холодный старт после `wsl --shutdown`: пользователь hub, `DOCKER_READY`, сервер 29.1.3, hello-world повторно успешен.

**Ошибки и обходы**
- `C:\Windows\System32\wsl.exe` → «Отказано в доступе» на любой аргумент. Используется `C:\Program Files\WSL\wsl.exe`.
- `wsl --install -d Ubuntu` → `WININET_E_INVALID_SERVER_RESPONSE`. Обход: rootfs с cloud-images.ubuntu.com + `wsl --import`.
- MSI и rootfs сначала скачались обрезанными (33.8 МБ и 277 МБ) — лечится `curl -C -` с циклом докачки и проверкой размера.

**Открыто**
- Прямой канал Tailscale между ноутбуком и сервером не устанавливается (релей ~150 мс). Внутри дома используется LAN 192.168.31.95.

## Этап 1. LiteLLM + Postgres (2026-11-06)

**Задача:** поднять шлюз LiteLLM с базой, доступный с ноутбука.

**Сделано**
- `infra/docker-compose.yml`: `postgres:16-alpine` + `ghcr.io/berriai/litellm:main-stable`.
- Секреты в `infra/.env` (не в git): пароль Postgres и мастер-ключ LiteLLM сгенерированы локально.
- Данные Postgres — в именованном томе `pgdata` (bind-mount на `/mnt/r` не работает, см. ошибки).
- Порт 4000 проброшен на Windows через `portproxy` → WSL IP; обновление IP автоматизировано
  задачами планировщика `AIHub-Net-OnLogon` и `AIHub-Net-Keep` (каждые 10 минут).

**Как проверено**
- `docker compose ps` — оба контейнера Up, `postgres` healthy.
- `curl 127.0.0.1:4000/health/liveliness` → `"I'm alive!"`, код 200.
- `curl 127.0.0.1:4000/health/readiness` → `{"status":"healthy","db":"connected"}`, код 200.
- С ноутбука: `curl http://192.168.31.95:4000/health/liveliness` → 200.

**Ошибки и обходы**
- `docker pull` падал с `tls: bad record MAC` из-за USB Wi-Fi адаптера сервера.
  Обход: маршруты AWS/Cloudflare/GitHub мимо happ через роутер, DNS в Ubuntu на роутер,
  отключён IPv6, `max-concurrent-downloads: 1`, до 20 повторов. LiteLLM скачался с 6-й попытки.
- Postgres не инициализировался на bind-mount `/mnt/r/...`: `could not change permissions of directory`.
  Решение: именованный том `pgdata` (живёт в ext4 внутри `R:\WSL\AIHub`).
- LiteLLM стартует ~90 секунд — прогоняет 220 prisma-миграций при первом запуске.

**Открыто**
- Проводной Ethernet на сервере решит проблему обрывов радикально (сейчас кабеля нет).

## Этап 2. hub-agent (ядро) — 2026-11-06

**Задача:** ядро платформы с реестром агентов и проксированием в LiteLLM.

**Сделано**
- `hub/app/main.py` — FastAPI: `/health`, `/agents`, `/agents/{id}`, `/models`, `/v1/chat/completions` (passthrough в LiteLLM с мастер-ключом).
- `config/agents.yaml` — реестр из 7 агентов с полями id/name/transport/host/status.
- `hub/Dockerfile` (python:3.12-slim + fastapi/uvicorn/httpx/pyyaml), сервис `hub` в `infra/docker-compose.yml`, порт 8000.
- Добавлен `AIHub-Keepalive`: держит WSL-дистрибутив запущенным (см. ошибки).

**Как проверено**
- `curl 127.0.0.1:8000/health` → `{"status":"ok","service":"hub-agent","version":"0.1.0"}`.
- `curl 127.0.0.1:8000/agents` → `count=7`.
- `curl 127.0.0.1:8000/models` → ответ LiteLLM (`healthcheck-stub`), значит прокси и мастер-ключ работают.
- С ноутбука: `http://192.168.31.95:8000/health` → 200, `/models` → 200, `:4000/health/readiness` → `db: connected`.

**Ошибки и обходы**
- Сборка образа падала на шаге `WORKDIR` с `tls: bad record MAC` (BuildKit тянет метаданные базового образа через нестабильный Wi-Fi).
  Обход: сначала `docker pull python:3.12-slim` с повторами, затем сборка классическим билдером `DOCKER_BUILDKIT=0`.
- **Контейнеры перезапускались каждые 1–2 минуты.** Причина: WSL-виртуалка завершалась сразу после выхода последнего процесса,
  поэтому каждый `wsl.exe`-вызов поднимал её заново (uptime 25–27 с), а Docker пересоздавал контейнеры.
  Решение: задача планировщика `AIHub-Keepalive` (`C:\Users\user\bin\aihub-keepalive.cmd`) запускает
  `wsl -d AIHub -u root -- bash -lc "exec sleep 2147483647"` и держит сессию.
  Проверка: `/proc/uptime` 27 с → 127 с, контейнеры `Up About a minute`, оба health-эндпоинта 200.

## Этап 3. Реальные модели и авторизация — 2026-11-06

**Задача:** подключить провайдеров к шлюзу и закрыть ядро токеном.

**Сделано**
- В `infra/litellm/config.yaml` два провайдера: `router.cheap` и `claudehub`, по схеме wildcard
  (`model_name: router.cheap/*`, `model: openai/*`) плюс 6 коротких алиасов.
- Ключи и базовые URL — в `infra/.env` (`ROUTER_CHEAP_KEY`, `CLAUDEHUB_KEY`, `*_BASE`), прокинуты в контейнер через compose.
- `hub`: проверка `HUB_TOKEN` для `/models` и `/v1/*`; `/health` и `/agents` открыты.
- Код `hub/app` смонтирован в контейнер (`../hub/app:/app/app:ro`) — правки применяются рестартом, без пересборки.

**Как проверено**
- `GET /v1/models` → 440 записей, алиасы на месте.
- `deepseek-v4.1-flash` через `:8000` → ответ «Да»; `router.cheap/gpt-6.1-sol` → `AI-HUB-OK`; `claudehub/claude-opus-5` → `HUB-CLAUDE-OK`.
- С ноутбука по LAN: `router.cheap/qwen3.8-max` через `:8000` → `LAN-HUB-OK`.
- Авторизация: без токена `401`, с токеном `200`, `/agents` открыт.

**Ошибки**
- Запрос с кириллицей в теле из git-bash отдавал `400 Missing required parameter: 'messages'`
  (curl подставлял cp1251, LiteLLM не мог разобрать JSON). Через UTF-8/ASCII — норма. Платформа ни при чём.

## Этап 4. Адаптеры Codex и Qwen (мост с ноутбука) — 2026-11-06

**Задача:** дергать Codex и Qwen через тот же API, что и модели. Оба живут только на ноутбуке:
Codex на сервере не авторизован, Qwen — десктоп-приложение с CDP.

**Сделано**
- Ноутбук: `C:\Users\lavy\aihub-bridge\agent-poller.py` — тянет задания с хаба, локально запускает
  `codex exec --json` или `node tools/qwen.js ask`, возвращает разобранный результат
  (текст, usage, thread_id, elapsed). Автозапуск: `run-bridge.cmd` в Startup.
- Хаб: очередь заданий и три эндпоинта — `POST /agents/{id}/run`, `GET /bridge/jobs`, `POST /bridge/result`.
  Запуск агента = постановка в очередь + ожидание результата (долгий HTTP-запрос).
- `config/agents.yaml`: у `codex` и `qwen-code` появилось поле `bridge_agent`.

**Почему pull, а не прямой HTTP с сервера на ноутбук**
Прямой вариант был реализован первым (`POST http://192.168.31.152:8010/run`) и не заработал:
`bridge unreachable` — брандмауэр Windows на ноутбуке блокирует входящие (`BlockInbound`),
а прав администратора на ноутбуке нет. Pull-схема не требует входящих портов вообще.

**Как проверено**
- `POST /agents/qwen-code/run` с ноутбука → `{"ok": true, "output": "HUB-QWEN-E2E", "elapsed": 23.13}`.
- `POST /agents/codex/run` → `{"ok": true, "output": "HUB-CODEX-E2E", "elapsed": 39.91, "usage": {...}}`.
- Очередь в `/health`: `queue`, `running`.

**Ошибки**
- Первый прогон codex дал `agent did not answer in time`: мост ходил со своим токеном (`brg-...`),
  а хаб ждал `HUB_TOKEN` → 401 на `/bridge/jobs`. Исправлено переходом на единый токен.
- Задание, поставленное до фикса, провисело в очереди и истекло по таймауту; повторный вызов прошёл за 39 с.

## Этап 5. Qwen на сервере + воркер хоста — 2026-11-06 (ожидает авторизации)

**Задача:** убрать зависимость Qwen от ноутбука — поставить десктоп-приложение Qwen на сервер.

**Сделано**
- Перенос инсталлятора 125 МБ по LAN: первая копия пришла битой (sha256 не совпал, 7z дал
  `Data Error` на `Qwen.exe`/`icudtl.dat`). Сделал передачу кусками по 8 МБ с проверкой sha256
  каждого куска и повтором — итоговый хеш совпал, инсталлятор валиден.
- Установка: `QwenSetup.exe /S` → `%LOCALAPPDATA%\Programs\Qwen\Qwen.exe` (версия 1.0.3.44).
  С битым файлом тот же ключ давал `exit=2`, то есть ошибка была в данных, а не в установщике.
- Qwen запускается задачей `AIHub-Qwen` (logon, интерактивная сессия) с `--remote-debugging-port=9333`:
  порт 9222 на сервере занят пользовательским Chrome. `qwen.js` теперь читает порт из `QWEN_CDP_PORT`
  и ищет страницу по трём правилам (chat.qwen.ai → app.asar/out/renderer → title содержит Qwen).
- Воркер хоста: `C:\Users\user\aihub-worker\worker.py`, задача `AIHub-Worker`, работает как pull-клиент
  хаба (`127.0.0.1:8000`), обслуживает агента `qwen-server`.
- В реестре агентов появился `qwen-server` (host: server).

**Состояние**
- CDP 9333 отвечает (`Chrome/134`), процессы Qwen живут в сессии 1 (Console).
- `qwen.js read` возвращает пусто, `ask` висит до таймаута: приложение не авторизовано.
  Нужен вход в аккаунт в окне Qwen на сервере — действие пользователя.

**Проверить после входа**
- `powershell -Command "$env:QWEN_CDP_PORT=9333; node C:\Users\user\bin\qwen.js read"` → текст чата.
- `POST /agents/qwen-server/run` → ответ агента.
