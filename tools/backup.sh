#!/usr/bin/env bash
# Резервная копия базы хаба (схема aihub) в R:\AI-Hub\backups.
# Запуск: wsl -d AIHub -e bash /mnt/r/AI-Hub/tools/backup.sh
set -euo pipefail

STAMP="${1:-$(date +%Y%m%d-%H%M%S)}"
OUT=/mnt/r/AI-Hub/backups
mkdir -p "$OUT"
FILE="$OUT/aihub-$STAMP.dump"

docker exec aihub-postgres pg_dump -U llmproxy -d litellm -Fc -n aihub > "$FILE"

SIZE=$(stat -c%s "$FILE")
if [ "$SIZE" -lt 2000 ]; then
  echo "FAIL: дамп подозрительно мал ($SIZE байт): $FILE"
  exit 1
fi

# контрольная сумма рядом с дампом — чтобы копию можно было проверить после переноса
sha256sum "$FILE" | awk '{print $1}' > "$FILE.sha256"

# оставляем 7 последних
ls -1t "$OUT"/aihub-*.dump 2>/dev/null | tail -n +8 | while read -r old; do
  rm -f "$old" "$old.sha256"
done

echo "OK $FILE $SIZE $(cat "$FILE.sha256")"