#!/usr/bin/env bash
# Проверка восстановления: разворачивает свежий дамп в отдельную БД aihub_restore
# и сравнивает количество строк с рабочей базой. Рабочая БД не изменяется.
set -euo pipefail

DIR=/mnt/r/AI-Hub/backups
DUMP="${1:-$(ls -1t "$DIR"/aihub-*.dump | head -1)}"
[ -f "$DUMP" ] || { echo "FAIL: нет дампа"; exit 1; }

echo "dump: $DUMP ($(stat -c%s "$DUMP") байт)"

docker exec aihub-postgres psql -U llmproxy -d litellm -q -c "drop database if exists aihub_restore" 
docker exec aihub-postgres psql -U llmproxy -d litellm -q -c "create database aihub_restore"
docker exec aihub-postgres psql -U llmproxy -d aihub_restore -q -c "create schema aihub"

docker cp "$DUMP" aihub-postgres:/tmp/restore.dump
docker exec aihub-postgres pg_restore -U llmproxy -d aihub_restore -n aihub --no-owner --exit-on-error /tmp/restore.dump
docker exec aihub-postgres rm -f /tmp/restore.dump

count() {  # $1 = db, $2 = table
  docker exec aihub-postgres psql -U llmproxy -d "$1" -t -A -c "select count(*) from aihub.$2"
}

fail=0
for t in jobs job_attempts job_events; do
  a=$(count litellm "$t")
  b=$(count aihub_restore "$t")
  if [ "$a" = "$b" ]; then
    echo "OK  $t: $a = $b"
  else
    echo "FAIL $t: рабочая $a, восстановленная $b"
    fail=1
  fi
done

# выборочная сверка содержимого последнего задания
docker exec aihub-postgres psql -U llmproxy -d aihub_restore -t -A -c \
  "select id||'|'||status||'|'||coalesce(left(result_output,40),'') from aihub.jobs order by created_at desc limit 1" \
  | sed 's/^/restored last: /'
docker exec aihub-postgres psql -U llmproxy -d litellm -t -A -c \
  "select id||'|'||status||'|'||coalesce(left(result_output,40),'') from aihub.jobs order by created_at desc limit 1" \
  | sed 's/^/live last:     /'

docker exec aihub-postgres psql -U llmproxy -d litellm -q -c "drop database aihub_restore"
echo "restore db dropped"

exit $fail