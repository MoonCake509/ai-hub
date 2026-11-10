"""DIAG-03 — юнит-тесты heartbeat воркеров (без сети)."""
import json
import sys
from datetime import timedelta

sys.path.insert(0, "/app")

from app import observations, workers  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print("ok", name)
    else:
        print("FAIL", name, extra)
        FAILED.append(name)


def expect_protocol(payload, needle):
    try:
        workers.heartbeat(payload)
    except workers.ProtocolError as exc:
        check("reject " + needle, needle in str(exc), str(exc))
        return
    check("reject " + needle, False, "no error")


check("protocol is 2", workers.PROTOCOL == 2)
check("ttl 180", workers.TTL_S == 180)

now = observations.now()
check("state ok now", workers.state_of(observations._iso(now), now) == "ok")
check("state stale after ttl", workers.state_of(observations._iso(now - timedelta(seconds=181)), now) == "stale")
check("state ok at boundary", workers.state_of(observations._iso(now - timedelta(seconds=179)), now) == "ok")
check("state unknown without hb", workers.state_of(None, now) == "unknown")

expect_protocol({}, "worker_id")
expect_protocol({"worker_id": "x", "protocol": 99}, "unsupported protocol")
expect_protocol({"worker_id": "x", "protocol": "abc"}, "protocol must be an integer")
expect_protocol({"worker_id": "x", "agents": "qwen"}, "agents must be a list")

captured = []
original = observations.record


def fake_record(agent_id, kind, state, source, checked_at, ttl, reason, detail):
    captured.append({"agent_id": agent_id, "kind": kind, "state": state, "source": source,
                     "ttl": ttl, "detail": detail})


observations.record = fake_record
try:
    result = workers.heartbeat({
        "worker_id": "unit-worker-1",
        "host": "UNIT-HOST",
        "os_user": "unit-user",
        "pid": 4242,
        "protocol": 2,
        "adapter_version": "2.1",
        "identity": {"python": "3.12", "api_key": "SECRET-1"},
        "capabilities": {"attachments": True, "token": "SECRET-2", "password": "SECRET-3"},
        "agents": [
            {"id": "codex", "cli_path": "C:/bin/codex.exe", "cli_version": "codex 1.2",
             "capabilities": {"workdir": True, "secret": "SECRET-4"}},
            {"id": "", "cli_path": "x"},
            "junk",
        ],
    })
    check("result ok", result.get("ok") is True)
    check("one agent written", result.get("agents") == 1, str(result))
    check("ttl from workers", captured and captured[0]["ttl"] == workers.TTL_S)
    check("source worker id", captured and captured[0]["source"] == "worker:unit-worker-1")
    check("kind availability", captured and captured[0]["kind"] == "availability")
    detail = captured[0]["detail"] if captured else {}
    check("detail has os user", detail.get("os_user") == "unit-user", json.dumps(detail))
    check("detail has cli path", detail.get("cli_path") == "C:/bin/codex.exe")
    check("detail has cli version", detail.get("cli_version") == "codex 1.2")
    check("detail has protocol", detail.get("protocol") == 2)
    captured.clear()
    cards = [{"id": "openclaw-laptop", "bridge_agent": "openclaw"},
             {"id": "codex", "bridge_agent": "codex"}]
    workers.heartbeat({"worker_id": "unit-worker-alias", "protocol": 2,
                       "agents": [{"id": "openclaw", "cli_path": "C:/x.cmd", "ready": False,
                                   "problem": "порт исполнителя 8787 не отвечает"},
                                  {"id": "codex", "cli_path": "C:/codex.exe", "ready": True}]},
                      None, cards)
    by_agent = {c["agent_id"]: c for c in captured}
    check("alias mapped to card", "openclaw-laptop" in by_agent, str(list(by_agent)))
    check("alias not stored as raw id", "openclaw" not in by_agent)
    check("not ready is degraded", by_agent.get("openclaw-laptop", {}).get("state") == "degraded",
          str(by_agent.get("openclaw-laptop", {}).get("state")))
    check("ready true is ok", by_agent.get("codex", {}).get("state") == "ok")
finally:
    observations.record = original

with workers.db._lock:
    conn = workers.db.connect()
    rows = workers.db._rows(conn, workers.db._qualify(
        "select capabilities::text as caps, identity::text as ident, agents::text as ag"
        " from aihub.workers where worker_id = 'unit-worker-1'"))
stored = json.dumps(rows, ensure_ascii=False) if rows else ""
check("worker row stored", len(rows) == 1, str(rows)[:200])
check("no secrets stored", "SECRET" not in stored, stored[:300])
with workers.db._lock:
    conn = workers.db.connect()
    conn.run(workers.db._qualify("delete from aihub.workers where worker_id = 'unit-worker-1'"))

with workers.db._lock:
    conn = workers.db.connect()
    conn.run(workers.db._qualify(
        "insert into aihub.workers(worker_id, host, os_user, pid, protocol, adapter_version, agents, capabilities, identity, hb_at)"
        " values ('unit-worker-2','H','u',7,2,'2.1','[]'::jsonb,'{\"attachments\":true}'::jsonb,'{}'::jsonb, now())"))
found = [w for w in workers.list_workers() if w["worker_id"] == "unit-worker-2"]
check("list has worker", len(found) == 1, str(found))
check("list state ok", found and found[0]["state"] == "ok")
check("list capabilities", found and found[0]["capabilities"].get("attachments") is True)
with workers.db._lock:
    conn = workers.db.connect()
    conn.run(workers.db._qualify("delete from aihub.workers where worker_id = 'unit-worker-2'"))

with workers.db._lock:
    conn = workers.db.connect()
    conn.run(workers.db._qualify("delete from aihub.agent_observations where source like 'worker:unit-worker-%'"))
    conn.run(workers.db._qualify("delete from aihub.workers where worker_id like 'unit-worker-%'"))
check("unit rows cleaned", True)

if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL WORKER UNIT TESTS PASSED")
