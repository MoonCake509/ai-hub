"""CORE-01: короткая аренда не истекает у живого исполнителя.

Стенд :8100 работает с LEASE_SECONDS=3 и уборкой аренд каждые 2 с. Тест берёт
задание, подтверждает аренду ровно с интервалом, который прислал хаб, и
проверяет, что через несколько аренд попытка всё ещё running. Контрольный
прогон без подтверждений должен честно стать unknown — иначе тест доказывал бы
только то, что уборка аренд выключена.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("HUB_TOKEN") or sys.exit("HUB_TOKEN env required")
FAILURES = []


def api(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except ValueError:
            payload = {}
        return e.code, payload


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(detail)))
    if not cond:
        FAILURES.append(name)


def job_state(job_id):
    code, payload = api("GET", "/jobs/" + job_id)
    return (payload.get("job") or payload) if code == 200 else {}


def drain_queue():
    """Убрать чужие незавершённые задания: аренда берёт старейшее в очереди."""
    code, queued = api("GET", "/jobs?status=queued&limit=200")
    for stale in (queued.get("jobs") or []):
        api("POST", "/jobs/%s/cancel" % stale["id"])


def new_job():
    key = "core01-lease-" + uuid.uuid4().hex[:12]
    code, payload = api("POST", "/agents/codex/run",
                        {"prompt": "CORE-01 lease probe", "idempotency_key": key, "timeout": 60})
    if code != 202 or not payload.get("job_id"):
        print("[FAIL] не удалось поставить задание:", code, payload)
        sys.exit(1)
    return payload["job_id"]


# --- 1. живой исполнитель с корректным heartbeat ---
drain_queue()
job_id = new_job()
code, lease = api("GET", "/bridge/jobs?worker=lease-alive&agents=codex&protocol=2")
item = lease if lease.get("id") else (lease.get("job") or {})
check("задание арендовано", code == 200 and item.get("id") == job_id, str(code) + " " + str(lease)[:160])
lease_seconds = int(item.get("lease_seconds") or 0)
interval = int(item.get("heartbeat_interval_s") or 0)
attempt_id = item.get("attempt_id")
check("хаб прислал интервал короче аренды", 0 < interval < lease_seconds,
      str(interval) + "/" + str(lease_seconds))

beats = 0
deadline = time.time() + max(6, lease_seconds * 3)
while time.time() < deadline:
    time.sleep(interval)
    code, hb = api("POST", "/bridge/heartbeat", {"attempt_id": attempt_id, "hb_interval_s": interval})
    if code == 200 and hb.get("ok") is True:
        beats += 1
    else:
        check("heartbeat отклонён живой попытке", False, str(code) + " " + str(hb))
        break
state = job_state(job_id)
attempts = state.get("attempts_detail") or []
statuses = [a.get("status") for a in attempts]
check("задание всё ещё running после " + str(round(deadline - time.time() + max(6, lease_seconds * 3))) + " с",
      state.get("status") == "running", state.get("status"))
check("попытка не стала unknown при живых heartbeat",
      statuses == ["running"], statuses)
check("heartbeat действительно отправлялись", beats >= 2, beats)

# закрываем задание, чтобы не оставлять арендованное в очереди
api("POST", "/bridge/result", {"job_id": job_id, "attempt_id": attempt_id, "ok": True,
                               "output": "lease probe", "usage": {}, "elapsed": 0.1})
after = job_state(job_id)
check("результат применён к живой попытке", after.get("status") == "succeeded", after.get("status"))

# --- 2. контроль: без heartbeat аренда обязана истечь ---
dead_job = new_job()
code, lease2 = api("GET", "/bridge/jobs?worker=lease-dead&agents=codex&protocol=2")
item2 = lease2 if lease2.get("id") else (lease2.get("job") or {})
check("контрольное задание арендовано", item2.get("id") == dead_job, str(lease2)[:120])
time.sleep(max(6, int(item2.get("lease_seconds") or 3) * 3))
dead_state = job_state(dead_job)
attempts2 = dead_state.get("attempts_detail") or []
check("без heartbeat задание стало unknown", dead_state.get("status") == "unknown",
      dead_state.get("status"))
check("без heartbeat попытка стала unknown",
      [a.get("status") for a in attempts2] == ["unknown"],
      [a.get("status") for a in attempts2])

print()
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("CORE-01 short lease: all checks passed")
