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

## Этап 6 — асинхронные задания и история прогонов (2026-11-06)

Сделано по ARCH-001 с правками ревью архитектора (`docs/proposals/ARCH-001-review-codex.md`).

- Postgres-слой `hub/app/db.py`: схема `aihub`, таблицы `jobs`, `job_attempts`, `job_events`;
  клиент `pg8000` (без SQLAlchemy — образ собирается офлайн из колёс).
- `hub/app/main.py` v0.3.0: `POST /agents/{id}/run` → `202 + job_id`; `GET /jobs/{id}`;
  `GET /jobs`; `POST /jobs/{id}/cancel`; `/bridge/jobs|heartbeat|result`.
- Аренда с heartbeat, истёкшая аренда → `unknown` + `needs_reconcile`, без автоповтора.
- Идемпотентность по `Idempotency-Key`: повтор с тем же телом → тот же job, с другим → `409`.
- Воркеры v2 (`worker_v2.py`) на ноутбуке и на серверном хосте: аренда, heartbeat-поток,
  локальный `outbox` с досылкой после перезапуска.
- Фильтр по возможностям: `GET /bridge/jobs?worker=X&agents=codex,qwen` — иначе серверный воркер
  забирал задания Codex и падал с `agent 'codex' is not served by this worker`.
- Зависимости ставятся из локальных колёс (`hub/wheels`): `pip` по Wi-Fi рвёт TLS.
- Qwen Desktop на сервере авторизован; CDP-таргет чата — тип `webview` (`https://chat.qwen.ai/`),
  а не `page` — `findTarget()` расширен; `qwen-server` переведён в `verified`.

Проверено вживую:

| Сценарий | Результат |
|---|---|
| Codex через хаб | `succeeded`, `FINAL-CODEX-V3`, worker `DESKTOP-BHMJN55` |
| Qwen на сервере через хаб | `succeeded`, `FINAL-QWEN-V3`, worker `WIN-V76QC2G60Q1-host` |
| Повтор с тем же `idempotency_key` | тот же `job_id`, `created: false` |
| Тот же ключ, другой промпт | `409 idempotency_key already used with another payload` |
| Отмена в очереди | `cancelled`, попыток 0 |
| Рестарт хаба с заданием в очереди | задание осталось `queued` |
| Аренда 20 с без heartbeat | `unknown`, `needs_reconcile: true`, попыток 1, повтора нет |

## Этап 7 — веб-интерфейс (2026-11-06)

- `web/` — статический UI без сборки: реестр агентов, запуск, история, карточка задания, отмена.
  Отдаётся контейнером `hub` по `/ui/` (`StaticFiles`, каталог `../web:/app/web:ro`), `/` → `/ui/`.
- API дополнено: `GET /jobs/{id}/events` (таймлайн), в списке заданий — `prompt_preview`.
- Вход по токену хаба: гейт в браузере, токен только в `localStorage`; неверный токен → сообщение.
- `finish_attempt` сбрасывает `needs_reconcile` после получения результата: если отмена была
  запрошена, но агент успел завершить работу, задание закрывается фактическим статусом.
- Воркеры: при ненулевом коде возврата пишут причину в `error` (`процесс агента прерван (kill)`),
  иначе карточка показывала пустой вывод без объяснения.
- `qwen.js`: жёсткий предел выполнения (`QWEN_HARD_TIMEOUT`, по умолчанию 200 с) и `process.exit(0)`
  после ответа — раньше процесс мог висеть из-за открытого WebSocket, а задание оставалось `running`.
- В `extractAnswer` отфильтрованы служебные строки UI («I prefer this response», «Auto»).

Проверено в браузере (Chromium, страница `http://192.168.31.95:8000/ui/`):

| Сценарий | Результат |
|---|---|
| Вход без токена | гейт; неверный токен → «Токен не подошёл» |
| Реестр | 8 агентов, статусы и транспорты |
| Запуск `qwen-server` из формы | `202`, карточка открылась, `succeeded` 13.7 с, вывод `UI-OK-2` |
| Таймлайн события | `queued → leased → succeeded` с временем и id попытки |
| Отмена работающего | `cancel_requested`, пометка «агент остановку не подтверждает», результат сохранён |
| Фильтр по статусу | 3 задания `failed`, карточка показывает причину |
| История после действий | 12 записей, превью промптов, длительности |


## Этап 7.1 — стабилизация интерфейса

- `ui13`: фоновый опрос запускается и после первого входа по токену (раньше требовалась перезагрузка страницы);
  поздний ответ `/jobs/{id}` больше не подменяет открытую карточку (проверка поколения запроса);
  повтор после потерянного ответа идёт с тем же `idempotency_key`; в истории показывается имя агента из реестра.
- Единственный экземпляр воркера: файловая блокировка ОС (`worker.lock`), второй запуск завершается с кодом 3.
  Проверено на ноутбуке и на сервере.
- Исходники воркеров и `qwen.js` перенесены в репозиторий (`agents/laptop`, `agents/server`).

## Этап 7.2 — эксплуатационный минимум

- Бэкап и восстановление: `tools/backup.sh`, `tools/restore_check.sh`, задачи `AIHub-Backup` и `AIHub-Backup-Pull`.
  Проверка восстановления: `jobs 19=19`, `job_attempts 17=17`, `job_events 56=56`, последняя запись совпала.
- Лимиты: `MAX_PROMPT_CHARS` (413), `MAX_QUEUE` (429 + Retry-After), `MAX_RESULT_CHARS` (усечение с пометкой).
- Пагинация: `offset`/`total`/`has_more` в API, кнопка «Показать ещё» в интерфейсе (`ui14`).


## Этап 7.3 — серверные адаптеры и HTTPS

- `openclaw-server`: `openclaw agent --message-file <file> --json`, проверено — `OC-SRV-HUB-OK` за 92,7 с.
- `hermes-server`: `hermes -z <prompt>`, проверено — `HERMES-SRV-HUB-OK` за 93,4 с.
- `llm-server` (бывший `ollama-server`): на 11434 слушает не Ollama, а llama.cpp (`dolphin-2.9-llama3-8b.Q4_K_M`,
  OpenAI-совместимый `/v1/chat/completions`). Проверено — `succeeded` за 282 с. Модель на CPU медленная
  и не выполняет инструкцию «ответь ровно так», это её свойство, а не ошибка адаптера.
- HTTPS: `aihub-hub-tls` на `:8443`, самоподписанный сертификат, portproxy и правило брандмауэра
  добавлены в `aihub-net.ps1`. Проверено: `/health`, `/jobs`, `/ui/` по TLS, интерфейс грузит 26 заданий.
- Реестр: все 8 агентов в статусе `verified`, у каждого есть `bridge_agent`.


## Этап 8.1 — сохранность результата и конкурентные операции (ревью Codex 7.1–7.3)

- Поздний результат: `finish_attempt` различает applied/duplicate/late/stale/not_found.
  Поздний payload пишется в `aihub.late_results` (attempt_id уникален), статус задания
  не переписывается. `GET /late-results` — очередь ручной сверки. Воркер удаляет outbox
  при applied/duplicate/late/stale — то есть только когда хаб подтвердил сохранение.
- Outbox воркера пишется атомарно: временный файл + `os.replace`.
- TLS больше не плодит второй backend: `app/serve.py` — один процесс, два листенера
  (8000 и 8443), один lifespan и один sweeper. Контейнер `hub-tls` удалён.
- Лимит очереди — в транзакции под `pg_advisory_xact_lock(7401)`: два процесса не могут
  одновременно увидеть свободное место. `expire_leases` — один атомарный оператор
  (попытка + задание + событие), а не три запроса.
- Идемпотентность сравнивает prompt_sha + agent_id + timeout_s. Таймаут ограничен
  10…7200 с; `timeout=0` больше не подменяется дефолтом (было `payload.get() or 600`).
- Пагинация ui15: опрос запрашивает столько, сколько показано (был баг схлопывания
  после 200 строк), поколение запроса `listGen`, блокировка повторной загрузки,
  дедупликация по id.
- Дампы удалены из индекса Git; `backups/` и `*.dump` в .gitignore. Удалённого
  репозитория нет — историю не переписывали, копии на диске сохранены.
- `tools/backup.sh`: дамп во временный файл → sha256 → атомарная публикация пары.
  `tools/pull_backup.py`: публикация на ноутбуке только после сверки sha256.
  `tools/restore_check.sh`: проверка checksum до pg_restore, уникальная тестовая БД
  (aihub_restore_$$), машинный критерий RESTORE-OK.
- Контрактные тесты `tests/contract_tests.py`, запуск одной командой
  `python tests/run_contract_tests.py` (останавливает воркеры, прогоняет, поднимает).

Проверено: 18 контрактных проверок PASS (идемпотентность 409, границы timeout 400,
prompt 413, duplicate, late + идемпотентный повтор, конкурентная очередь ≤ лимита).
Восстановление из атомарного дампа: RESTORE-OK jobs=34.
UI ui15 в браузере: 50 → «ещё» → 100 строк, 100 уникальных id, после опроса 7 с
осталось 100 (не схлопнулось); смена фильтра показывает только выбранный статус.
