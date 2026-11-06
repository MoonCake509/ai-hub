#!/bin/bash
# Атомарный бэкап: временный файл → checksum → публикация готовой пары.
# Потребитель (pull_backup.py) берёт только завершённые наборы .dump + .sha256.
set -euo pipefail
DEST=/mnt/r/AI-Hub/backups
STAMP=$(date +%Y%m%d-%H%M%S)
FINAL="$DEST/aihub-$STAMP.dump"
TMP="$DEST/.aihub-$STAMP.dump.part"

mkdir -p "$DEST"
trap 'rm -f "$TMP" "$TMP.sha256"' EXIT

docker exec aihub-postgres pg_dump -U llmproxy -d litellm -Fc -n aihub > "$TMP"
SIZE=$(stat -c%s "$TMP")
if [ "$SIZE" -lt 2000 ]; then
    echo "FAIL: дамп подозрительно мал ($SIZE байт)" >&2
    exit 1
fi

sha256sum "$TMP" | sed "s|$TMP|$FINAL|" > "$TMP.sha256"
mv "$TMP" "$FINAL"
mv "$TMP.sha256" "$FINAL.sha256"
trap - EXIT

echo "OK $FINAL $SIZE $(cut -d' ' -f1 "$FINAL.sha256")"

# храним 7 последних полных наборов
ls -1t "$DEST"/aihub-*.dump 2>/dev/null | tail -n +8 | while read -r f; do
    rm -f "$f" "$f.sha256"
done