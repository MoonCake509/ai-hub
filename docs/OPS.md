# Операционные заметки по серверу

## Запуск и остановка Linux-среды

```
"C:\Program Files\WSL\wsl.exe" -d AIHub -- bash -lc "systemctl is-system-running"
"C:\Program Files\WSL\wsl.exe" --shutdown          # остановить все дистрибутивы
```

Дистрибутив `AIHub` — по умолчанию, WSL запускается при обращении к `wsl.exe` или к
`\\wsl.localhost\AIHub`. Автозапуск при загрузке Windows: задача планировщика
`wsl.exe -d AIHub -u root -- bash -lc "sleep infinity"` (пока не создана).

## Docker

Демон — внутри Ubuntu (`systemctl status docker`). Docker Desktop на Windows не используется:
его `dockerDesktopLinuxEngine` не поднимается без интерактивной сессии.

```
"C:\Program Files\WSL\wsl.exe" -d AIHub -- docker ps
```

## Что не работает и почему

- `C:\Windows\System32\wsl.exe` — «Отказано в доступе» на любой аргумент (сломанный стаб).
  Использовать `C:\Program Files\WSL\wsl.exe`.
- `wsl --install -d Ubuntu` — `WININET_E_INVALID_SERVER_RESPONSE`. Обход: rootfs + `wsl --import`.
- Загрузки с github/microsoft CDN рвутся — обязательно `curl -C -` с проверкой полного размера.
- Tailscale: сервер ↔ ноутбук идёт через DERP-релей (~150 мс), прямой канал не устанавливается,
  хотя LAN-адрес сервера (`192.168.31.95:41641`) ноутбуку известен. Для работы внутри дома
  использовать LAN, Tailscale — только для внешнего доступа.

## WSL и контейнеры

- Дистрибутив должен работать постоянно, иначе Docker перезапускает контейнеры при каждом обращении.
  Держит его задача `AIHub-Keepalive` (при входе пользователя): `aihub-keepalive.cmd` → `wsl ... exec sleep 2147483647`.
  Проверка: `wsl -d AIHub -u root -- cat /proc/uptime` — uptime должен расти, а не быть ~30 с.
- Если задача не запущена: `schtasks /run /tn AIHub-Keepalive`, состояние — `schtasks /query /tn AIHub-Keepalive /v /fo list`.

## Сборка образов

BuildKit на этом канале рвёт соединение (`tls: bad record MAC`). Сборка:

```
docker pull python:3.12-slim     # с повторами
cd /mnt/r/AI-Hub/infra && DOCKER_BUILDKIT=0 docker compose build hub
```

## Проброс портов в WSL

WSL-IP меняется при перезапуске виртуалки. Проброс 4000/8000 обновляют задачи
`AIHub-Net-OnLogon` и `AIHub-Net-Keep` (каждые 10 минут), лог — `C:\Users\user\aihub-net.log`.

## Агент ноутбука (Codex, Qwen)

- Скрипт: `C:\Users\lavy\aihub-bridge\agent-poller.py`, лог — `bridge.log` рядом.
- Автозапуск: `aihub-bridge.cmd` в `Startup` (при входе пользователя). Задачи планировщика на ноутбуке
  не создать — нет прав администратора.
- Если задачи не выполняются: проверить лог, затем `hub.txt` (адрес хаба) и `token.txt` (должен совпадать
  с `HUB_TOKEN` на сервере). Перезапуск: закрыть процесс `python.exe agent-poller.py` и запустить `run-bridge.cmd`.
- Локальная отладка без хаба: `python bridge.py` поднимает HTTP на :8010 (только для проверки с ноутбука).
