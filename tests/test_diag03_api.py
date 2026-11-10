"""DIAG-03 — живой контракт heartbeat воркера на стенде :8100.

Проверяет: heartbeat принимается и превращается в наблюдение availability по агентам
воркера; /workers показывает исполнителя с версией протокола и путём CLI; секреты не
сохраняются и не отдаются; чужая версия протокола отвергается 409; карточки агентов,
которых воркер не обслуживает, не появляются.

Запуск: python run_diag03.py <base_url> <token>
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
WSL = r"C:\Program Files\WSL\wsl.exe"
WID = "diag03-live-" + str(int(time.time()))
FAILED = []


def check(name, cond, extra=""):
    if cond:
        print("[PASS]", name)
    else:
        print("[FAIL]", name, extra)
        FAILED.append(name)


def api(method, path, body=None, token=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token and TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def cleanup():
    sql = ("delete from aihub.agent_observations where source like 'worker:diag03-live-%';"
           "delete from aihub.workers where worker_id like 'diag03-live-%'")
    subprocess.run([WSL, "-d", "AIHub", "-e", "bash", "-lc",
                    "docker exec aihub-postgres-test psql -U aihub_test -d aihub_test -c " + repr(sql)],
                   capture_output=True)


cleanup()
payload = {
    "worker_id": WID,
    "host": "CT-DIAG03-HOST",
    "os_user": "diag03-user",
    "pid": 5150,
    "protocol": 2,
    "adapter_version": "2.1-live",
    "identity": {"python": "3.12", "note": "live contract"},
    "capabilities": {"attachments": True, "token": "SHOULD-NOT-BE-STORED"},
    "agents": [
        {"id": "codex", "cli_path": "C:/tools/codex.exe", "cli_version": "codex 9.9",
         "cli_size": 12345, "cli_mtime": "2026-11-10T08:00:00",
         "capabilities": {"workdir": True, "secret": "SHOULD-NOT-BE-STORED"}},
        {"id": "qwen-server", "cli_path": "C:/node/qwen.js", "cli_version": "qwen 1.0"},
        {"id": "ghost-agent", "cli_path": "C:/ghost.exe"},
    ],
}

status, body = api("POST", "/bridge/worker/heartbeat", payload)
check("heartbeat 200", status == 200, str(status) + " " + json.dumps(body)[:200])
check("heartbeat reports agents", body.get("agents") == 3, json.dumps(body))

status, agents = api("GET", "/agents")
by_id = {a["id"]: a for a in agents.get("agents", [])}
codex = by_id.get("codex") or {}
av = (codex.get("observations") or {}).get("availability") or {}
check("codex availability ok", av.get("state") == "ok", json.dumps(av)[:300])
check("codex effective ok", codex.get("effective_state") == "ok", str(codex.get("effective_state")))
detail = av.get("detail") or {}
check("detail os user", detail.get("os_user") == "diag03-user", json.dumps(detail)[:300])
check("detail cli path", detail.get("cli_path") == "C:/tools/codex.exe")
check("detail cli version", detail.get("cli_version") == "codex 9.9")
check("detail protocol", detail.get("protocol") == 2)
check("detail worker id", detail.get("worker_id") == WID)
check("availability short ttl", av.get("ttl_s") == 180, str(av.get("ttl_s")))
check("source is worker", av.get("source") == "worker:" + WID, str(av.get("source")))
check("ghost agent not a card", "ghost-agent" not in by_id)

status, workers = api("GET", "/workers")
items = workers.get("workers") or []
mine = [w for w in items if w.get("worker_id") == WID]
check("workers list has mine", len(mine) == 1, json.dumps(workers)[:200])
if mine:
    me = mine[0]
    check("worker state ok", me.get("state") == "ok", str(me.get("state")))
    check("worker protocol", me.get("protocol") == 2)
    check("worker adapter", me.get("adapter_version") == "2.1-live")
    check("worker agents", sorted(me.get("agents") or []) == ["codex", "ghost-agent", "qwen-server"], str(me.get("agents")))
    check("worker capabilities kept", (me.get("capabilities") or {}).get("attachments") is True)
    check("worker capabilities no token", "token" not in (me.get("capabilities") or {}), json.dumps(me.get("capabilities")))
    check("worker age small", isinstance(me.get("age_s"), int) and me["age_s"] <= 30, str(me.get("age_s")))

raw = json.dumps(agents, ensure_ascii=False) + json.dumps(workers, ensure_ascii=False)
check("no secret in api output", "SHOULD-NOT-BE-STORED" not in raw)

status, body = api("POST", "/bridge/worker/heartbeat", {**payload, "protocol": 99})
check("protocol 99 rejected", status == 409, str(status))
status, body = api("POST", "/bridge/worker/heartbeat", {"worker_id": ""})
check("empty worker id rejected", status == 409, str(status))
status, body = api("POST", "/bridge/worker/heartbeat", payload, token=False)
check("heartbeat requires token", status in (401, 403), str(status))

payload2 = json.loads(json.dumps(payload))
payload2["agents"] = [{"id": "codex", "cli_path": "C:/tools/codex-moved.exe", "cli_version": "codex 9.9"}]
status, body = api("POST", "/bridge/worker/heartbeat", payload2)
check("second heartbeat 200", status == 200, str(status))
status, agents = api("GET", "/agents")
av = ((by_id2 := {a["id"]: a for a in agents.get("agents", [])}).get("codex") or {}).get("observations", {}).get("availability") or {}
check("cli path updated", (av.get("detail") or {}).get("cli_path") == "C:/tools/codex-moved.exe", json.dumps(av)[:300])

# псевдоним воркера (openclaw → карточка openclaw-laptop) и честная недоступность
alias_payload = {
    "worker_id": WID + "-alias",
    "host": "CT-DIAG03-HOST",
    "os_user": "diag03-user",
    "pid": 5151,
    "protocol": 2,
    "adapter_version": "2.1-live",
    "agents": [
        {"id": "openclaw", "cli_path": "C:/tools/openclaw.cmd", "ready": False,
         "problem": "порт исполнителя 8787 не отвечает"},
        {"id": "qwen", "cli_path": "C:/tools/qwen.js", "ready": True},
    ],
}
status, body = api("POST", "/bridge/worker/heartbeat", alias_payload)
check("alias heartbeat 200", status == 200, str(status))
status, agents = api("GET", "/agents")
by_id3 = {a["id"]: a for a in agents.get("agents", [])}
oc = ((by_id3.get("openclaw-laptop") or {}).get("observations") or {}).get("availability") or {}
qc = ((by_id3.get("qwen-code") or {}).get("observations") or {}).get("availability") or {}
check("alias openclaw→card", oc.get("state") == "degraded", json.dumps(oc)[:200])
check("alias degraded reason", "8787" in (oc.get("reason") or ""), str(oc.get("reason")))
check("alias qwen→qwen-code ok", qc.get("state") == "ok", json.dumps(qc)[:200])
check("alias source", qc.get("source") == "worker:" + WID + "-alias", str(qc.get("source")))

cleanup()
status, workers = api("GET", "/workers")
check("cleanup removed worker", not [w for w in (workers.get("workers") or []) if w.get("worker_id") == WID])

if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL DIAG-03 LIVE CHECKS PASSED")
