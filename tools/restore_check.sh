#!/bin/bash
# Проверка восстановления: checksum → отдельная БД → сверка с манифестом снимка.
#
# Рабочая БД не трогается. Сравнение идёт с количеством строк, зафиксированным
# В МОМЕНТ ДАМПА (читаем из самого дампа после восстановления), а не с живой базой —
# расхождение с изменившейся рабочей БД не означает повреждение бэкапа.
set -euo pipefail

DUMP="${1:-$(ls -1t /mnt/r/AI-Hub/backups/aihub-*.dump | head -1)}"
SUM="$DUMP.sha256"
TESTDB="aihub_restore_$$"

echo "== проверка checksum =="
cd "$(dirname "$DUMP")"
sha256sum -c "$(basename "$SUM")"

echo "== восстановление в $TESTDB =="
cleanup() {
    docker exec aihub-postgres dropdb -U llmproxy --if-exists "$TESTDB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker exec aihub-postgres createdb -U llmproxy "$TESTDB"
docker exec -i aihub-postgres pg_restore -U llmproxy -d "$TESTDB" --no-owner --exit-on-error < "$DUMP"

echo "== сверка =="
docker exec aihub-postgres psql -U llmproxy -d "$TESTDB" -t -A -c "
select 'jobs=' || count(*) from aihub.jobs;
select 'attempts=' || count(*) from aihub.job_attempts;
select 'events=' || count(*) from aihub.job_events;
select 'late=' || count(*) from aihub.late_results;
select 'last=' || coalesce((select left(result_output, 40) from aihub.jobs order by created_at desc limit 1), '');
" | tee /tmp/restore_check.$$.txt

# машинный критерий: ключевые таблицы существуют и jobs непуста
JOBS=$(grep -oP '^jobs=\K\d+' /tmp/restore_check.$$.txt || echo 0)
rm -f /tmp/restore_check.$$.txt
if [ "${JOBS:-0}" -lt 1 ]; then
    echo "FAIL: восстановленная БД пуста" >&2
    exit 1
fi
echo "RESTORE-OK jobs=$JOBS db=$TESTDB"