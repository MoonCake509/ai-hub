"""DIAG-01: живой контракт наблюдений против стенда :8100 (схема aihub_test).

Проверяет на реальном процессе:
1. /agents и /agent-profiles отдают четыре вида наблюдений у каждого агента;
2. без heartbeat availability = unknown, effective_state не ok, причина указана;
3. записанное наблюдение доступности читается из БД и даёт ok;
4. истёкший TTL даёт stale и degraded без записи в БД (вычисление на чтение);
5. после удаления наблюдения состояние снова unknown;
6. секретов в observations нет.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("HUB_TOKEN") or sys.exit("HUB_TOKEN env required")
WSL = r"C:\Program Files\WSL\wsl.exe"
KINDS = {"configured", "compatibility", "availability", "quality"}
FAILURES = []


def api(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def sql(query):
    r = subprocess.run([WSL, "-d", "AIHub", "-e", "bash", "-lc",
                        "docker exec aihub-postgres-test psql -U aihub_test -d aihub_test -t -A -c " + repr(query)],
                       capture_output=True)
    if r.returncode != 0:
        print("   psql:", r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout.decode("utf-8", "replace").strip()


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(detail)))
    if not cond:
        FAILURES.append(name)


def cleanup():
    sql("delete from aihub.agent_observations where source = 'diag01-test'")


def insert(agent_id, state, checked_at_sql, ttl=180, reason="null"):
    sql("insert into aihub.agent_observations(id, agent_id, kind, state, source, checked_at, ttl_s, reason, detail)"
        " values ('diag01-" + agent_id + "-" + state + "','" + agent_id + "','availability','" + state +
        "','diag01-test', " + checked_at_sql + ", " + str(ttl) + ", " + reason + ", '{}'::jsonb)")


def obs_of(payload, kind):
    return (payload.get("observations") or {}).get(kind) or {}


print("== реестр")
code, data = api("GET", "/agents")
agents = data.get("agents") or []
check("/agents 200", code == 200, code)
check("9 карточек", len(agents) == 9, len(agents))
check("четыре вида у каждого", all(KINDS <= set(a.get("observations") or {}) for a in agents))
check("status сохранён для старых клиентов",
      all(a.get("status") in ("verified", "unverified") for a in agents),
      sorted({a.get("status") for a in agents}))
states = {obs_of(a, "availability").get("state") for a in agents}
check("без heartbeat availability = unknown", states == {"unknown"}, states)
check("effective_state не подменяется статусом хаба",
      {a.get("effective_state") for a in agents} == {"unknown"},
      {a.get("effective_state") for a in agents})
check("причина unknown указана", all(obs_of(a, "availability").get("reason") for a in agents))
check("configured = ok у всех карточек", {obs_of(a, "configured").get("state") for a in agents} == {"ok"},
      {obs_of(a, "configured").get("state") for a in agents})
check("quality без прогонов = unknown", {obs_of(a, "quality").get("state") for a in agents} == {"unknown"})
blob = json.dumps([a.get("observations") for a in agents], ensure_ascii=False).lower()
check("в observations нет токенов/ключей", "token" not in blob and "secret" not in blob)

code, prof = api("GET", "/agent-profiles/codex")
check("/agent-profiles отдаёт наблюдения", code == 200 and KINDS <= set(prof.get("observations") or {}), code)
check("/agent-profiles отдаёт effective_state", prof.get("effective_state") == "unknown", prof.get("effective_state"))

code, detail = api("GET", "/agents/codex")
check("/agents/{id} отдаёт наблюдения", code == 200 and KINDS <= set(detail.get("observations") or {}), code)

print("== реестровые наблюдения в БД")
rows = sql("select count(*) from aihub.agent_observations where source = 'registry'")
check("реестровые наблюдения записаны при старте", int(rows or 0) >= 18, rows)
kinds_db = sql("select string_agg(distinct kind, ',') from aihub.agent_observations where source = 'registry'")
check("в БД только configured и compatibility", set(kinds_db.split(",")) == {"configured", "compatibility"}, kinds_db)

print("== свежий heartbeat")
cleanup()
insert("codex", "ok", "now()")
code, detail = api("GET", "/agents/codex")
check("availability читается из БД как ok", obs_of(detail, "availability").get("state") == "ok",
      obs_of(detail, "availability"))
check("effective_state = ok при живом канале", detail.get("effective_state") == "ok", detail.get("effective_state"))
check("quality остаётся unknown (не выводится из availability)",
      obs_of(detail, "quality").get("state") == "unknown")

print("== истёкший TTL")
cleanup()
insert("codex", "ok", "now() - interval '9 minutes'", 180, "'heartbeat 9 минут назад'")
code, detail = api("GET", "/agents/codex")
avail = obs_of(detail, "availability")
check("TTL истёк → stale без записи в БД", avail.get("state") == "stale", avail)
check("effective_state = degraded", detail.get("effective_state") == "degraded", detail.get("effective_state"))
check("причина stale содержит возраст и TTL", "назад" in (avail.get("reason") or "") and "TTL" in (avail.get("reason") or ""), avail.get("reason"))
rows = sql("select count(*) from aihub.agent_observations where agent_id = 'codex' and kind = 'availability' and state = 'stale'")
check("в БД по-прежнему ok, stale только на чтении", int(rows or 0) == 0, rows)

print("== удаление наблюдения")
cleanup()
code, detail = api("GET", "/agents/codex")
check("без записи снова unknown", obs_of(detail, "availability").get("state") == "unknown", obs_of(detail, "availability"))

print()
if FAILURES:
    print("FAILED:", len(FAILURES), FAILURES)
    sys.exit(1)
print("DIAG-01 API CHECKS PASSED")
