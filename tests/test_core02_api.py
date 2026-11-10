"""CORE-02: живой контракт аренды ресурсов на стенде :8100.

Проверяет поверх реального контейнера: занятый checkout не выдаётся второму
заданию, освобождение возвращает ресурс с новым поколением, чужая аренда не
снимается, alias одного CDP не обходит лимит (юнит на ключи).
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("HUB_TOKEN") or sys.exit("HUB_TOKEN env required")
if ":8000" in BASE:
    sys.exit("refusing to run the resource probe against production (:8000)")
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


def lease_of(payload):
    return payload if payload.get("id") else (payload.get("job") or {})


def job_of(payload):
    return payload.get("job_id") or (payload.get("job") or {}).get("job_id")


# Стенд мог остаться с заданиями и арендами прошлых прогонов: аренда берёт
# старейшее задание, поэтому без очистки проверка «арендовали именно наше» ложна.
code, queued = api("GET", "/jobs?status=queued&limit=200")
for stale in (queued.get("jobs") or []):
    api("POST", "/jobs/%s/cancel" % stale["id"])
code, held = api("GET", "/resources")
for row in (held.get("resources") or []):
    api("POST", "/resources/release", {"key": row.get("key"), "job_id": row.get("job_id")})

project = "core02-" + uuid.uuid4().hex[:8]
workdir = "R:" + chr(92) + "AI-Hub" + chr(92) + "tmp" + chr(92) + "core02-" + uuid.uuid4().hex[:6]

# 1. Проект с рабочим каталогом — единственный способ получить checkout-ключ.
code, proj = api("POST", "/projects", {"id": project, "name": "CORE-02 " + project,
                                       "host_id": "laptop", "workdir": workdir})
check("проект с рабочим каталогом создан", code in (200, 201), str(code) + " " + str(proj)[:140])
# id проекта назначает хаб, а не клиент: берём фактический.
project_id = proj.get("id") or (proj.get("project") or {}).get("id")
check("хаб вернул id проекта", bool(project_id), str(proj)[:160])
if not project_id:
    print(chr(10).join(FAILURES))
    sys.exit(1)
code, conv = api("POST", "/projects/%s/conversations" % project_id, {"role_id": "coder"})
conv_id = conv.get("id") or (conv.get("conversation") or {}).get("id")
check("разговор создан", code in (200, 201) and bool(conv_id), str(code) + " " + str(conv)[:120])
if not conv_id:
    print("\n".join(FAILURES))
    sys.exit(1)

# 2. Первое задание в каталоге — берёт checkout.
code, first = api("POST", "/conversations/%s/send" % conv_id,
                  {"text": "CORE-02 checkout probe A", "agent_id": "codex", "timeout": 60})
job1 = job_of(first)
check("первое задание поставлено", bool(job1), str(code) + " " + str(first)[:180])
if not job1:
    print("\n".join(FAILURES))
    sys.exit(1)

code, lease = api("GET", "/bridge/jobs?worker=core02-probe&protocol=2")
item = lease_of(lease)
resource = item.get("resource") or {}
check("задание арендовано", item.get("id") == job1, str(lease)[:160])
check("аренда взяла checkout-ресурс", str(resource.get("key") or "").startswith("checkout:"),
      resource.get("key"))
check("checkout привязан к заданию", resource.get("job_id") == job1, resource.get("job_id"))
check("у аренды есть поколение", int(resource.get("generation") or 0) >= 1, resource.get("generation"))
generation = int(resource.get("generation") or 0)

# 3. Второе задание в тот же каталог не может писать параллельно.
code, second = api("POST", "/conversations/%s/send" % conv_id,
                   {"text": "CORE-02 checkout probe B", "agent_id": "codex", "timeout": 60})
job2 = job_of(second)
check("второе задание поставлено", bool(job2) and job2 != job1, str(second)[:160])
code, blocked = api("GET", "/bridge/jobs?worker=core02-probe2&protocol=2")
check("занятый checkout не выдан второму исполнителю", not lease_of(blocked).get("id"),
      str(blocked)[:160])

# 4. Чужая аренда не снимается, своя видна в /resources.
code, foreign = api("POST", "/resources/release",
                    {"key": resource.get("key"), "job_id": "not-my-job"})
check("чужое освобождение отклонено", foreign.get("ok") is False, foreign)
code, listing = api("GET", "/resources")
keys = [x.get("key") for x in (listing.get("resources") or [])]
check("checkout виден в /resources", resource.get("key") in keys, keys[:5])

# 5. Закрываем первое задание: ресурс освобождается, второе берёт его с новым поколением.
api("POST", "/bridge/heartbeat", {"attempt_id": item.get("attempt_id")})
api("POST", "/bridge/result", {"job_id": job1, "attempt_id": item.get("attempt_id"),
                               "ok": True, "output": "done", "usage": {}, "elapsed": 0.1})
code, listing = api("GET", "/resources")
keys = [x.get("key") for x in (listing.get("resources") or [])]
check("после закрытия задания checkout освобождён", resource.get("key") not in keys, keys[:5])

code, lease2 = api("GET", "/bridge/jobs?worker=core02-probe2&protocol=2")
item2 = lease_of(lease2)
res2 = item2.get("resource") or {}
check("второе задание получило освободившийся checkout", item2.get("id") == job2,
      str(lease2)[:160])
check("поколение выросло", int(res2.get("generation") or 0) > generation,
      str(generation) + " -> " + str(res2.get("generation")))
check("тот же ключ checkout", res2.get("key") == resource.get("key"),
      str(res2.get("key")) + " vs " + str(resource.get("key")))

# 6. Убираем за собой.
api("POST", "/bridge/result", {"job_id": job2, "attempt_id": item2.get("attempt_id"),
                               "ok": True, "output": "done", "usage": {}, "elapsed": 0.1})

print()
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("CORE-02 resources: all checks passed")
