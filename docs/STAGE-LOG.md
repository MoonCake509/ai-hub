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
