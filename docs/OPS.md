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
