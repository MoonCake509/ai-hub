"""Один процесс хаба: HTTP для воркеров и HTTPS для людей.

Два листенера в одном процессе — значит один экземпляр приложения, один sweeper
и один db._lock. Отдельный контейнер-близнец для TLS не нужен: два процесса делили бы
одну базу без общей блокировки.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from .main import app


async def _run() -> None:
    port = int(os.environ.get("PORT", "8000"))
    tls_port = int(os.environ.get("TLS_PORT", "8443"))
    cert = os.environ.get("TLS_CERT", "/certs/hub.crt")
    key = os.environ.get("TLS_KEY", "/certs/hub.key")

    configs = [uvicorn.Config(app, host="0.0.0.0", port=port, lifespan="off", log_level="info")]
    if os.path.exists(cert) and os.path.exists(key):
        configs.append(uvicorn.Config(app, host="0.0.0.0", port=tls_port, lifespan="off",
                                      log_level="info", ssl_certfile=cert, ssl_keyfile=key))
    else:
        print(f"TLS disabled: нет сертификата {cert} / ключа {key}", flush=True)

    servers = [uvicorn.Server(cfg) for cfg in configs]
    # lifespan запускаем один раз на приложение, а не на каждый листенер
    async with app.router.lifespan_context(app):
        await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(_run())