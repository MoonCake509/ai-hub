#!/bin/bash
# Быстрая выкладка AI Hub: код смонтирован в контейнер, поэтому обычно достаточно перезапуска.
# Сборка нужна только при изменении зависимостей (hub/requirements.txt).
#   tools/deploy.sh            перезапуск + проверка здоровья
#   tools/deploy.sh --build    пересборка образа + перезапуск
set -e
cd "$(dirname "$0")/../infra"
if [ "$1" = "--build" ]; then
  echo "building..."
  docker compose -p aihub --env-file .env build hub >/dev/null
fi
echo "restarting hub..."
docker restart aihub-hub >/dev/null
for i in $(seq 1 40); do
  H=$(curl -s --max-time 2 http://127.0.0.1:8000/health || true)
  case "$H" in *'"db":true'*) echo "OK $H"; exit 0;; esac
  sleep 1
done
echo "FAILED: $H"
exit 1