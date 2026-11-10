"""CORE-01: живой контракт аренды на стенде :8100.

Проверяет поверх реального контейнера: старый клиент без версии протокола не
получает задание, повторная доставка результата не создаёт нового исполнения,
heartbeat возвращает интервал короче аренды, чужой клиент не крадёт задание.
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
    sys.exit("refusing to run the lease probe against production (:8000)")
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


# 1. Старый клиент (без версии протокола) и клиент v1 не арендуют задание.
code, body = api("GET", "/bridge/jobs?worker=legacy-probe")
check("без версии протокола — 426", code == 426, str(code) + " " + str(body)[:120])
check("в отказе перечислены поддерживаемые версии",
      isinstance(body.get("supported"), list) and 2 in body.get("supported"), body.get("supported"))
check("в отказе назван текущий протокол", body.get("current") == 2, body.get("current"))
code_v1, body_v1 = api("GET", "/bridge/jobs?worker=legacy-probe&protocol=1")
check("protocol=1 — 426", code_v1 == 426, str(code_v1))
code_junk, _ = api("GET", "/bridge/jobs?worker=legacy-probe&protocol=abc")
check("protocol=abc — 426", code_junk == 426, str(code_junk))

# 2. Очередь стенда могла остаться от прошлых прогонов: аренда берёт старейшее
# задание, и проверка «арендовали именно наше» была бы ложной. Чистим очередь.
code, queued = api("GET", "/jobs?status=queued&limit=200")
for stale in (queued.get("jobs") or []):
    api("POST", "/jobs/%s/cancel" % stale["id"])
code, queued = api("GET", "/jobs?status=queued&limit=200")
check("очередь стенда пуста перед проверкой", (queued.get("total") or 0) == 0,
      queued.get("total"))

# 3. Ставим задание от имени реального агента.
key = "core01-" + uuid.uuid4().hex[:12]
code, job = api("POST", "/agents/codex/run",
                {"prompt": "CORE-01 contract probe: ответь одним словом ok",
                 "idempotency_key": key, "timeout": 60})
check("задание поставлено в очередь", code == 202 and job.get("job_id"), str(code) + " " + str(job)[:160])
job_id = job.get("job_id")
if not job_id:
    print("\n".join(FAILURES))
    sys.exit(1)

# 3. Клиент v2 арендует задание и получает контракт аренды.
code, lease = api("GET", "/bridge/jobs?worker=core01-probe&agents=codex&protocol=2")
# Аренда отдаётся плоским объектом (без обёртки "job"): так её видит воркер.
item = lease if lease.get("id") else (lease.get("job") or {})
check("клиент v2 получил задание", code == 200 and item.get("id") is not None,
      str(code) + " " + str(lease)[:160])
check("задание то же, что поставлено", item.get("id") == job_id, item.get("id"))
check("в аренде указан протокол 2", item.get("protocol") == 2, item.get("protocol"))
lease_seconds = item.get("lease_seconds") or 0
interval = item.get("heartbeat_interval_s") or 0
check("heartbeat короче аренды", 0 < interval < lease_seconds, str(interval) + "/" + str(lease_seconds))
attempt_id = item.get("attempt_id")

# 5. Второй клиент не крадёт уже арендованное задание.
code, empty = api("GET", "/bridge/jobs?worker=other-probe&agents=codex&protocol=2")
check("задание не отдано второму клиенту",
      code == 200 and not empty.get("id"), str(empty)[:120])

# 5. Heartbeat: интервал проверяется относительно аренды.
code, hb = api("POST", "/bridge/heartbeat", {"attempt_id": attempt_id, "hb_interval_s": interval})
check("heartbeat принят", code == 200 and hb.get("ok") is True, str(code) + " " + str(hb))
check("heartbeat возвращает интервал короче аренды",
      hb.get("heartbeat_interval_s", 999) < hb.get("lease_seconds", 0), str(hb))
check("корректный интервал не помечен риском", hb.get("lease_risk") is None, hb.get("lease_risk"))
code, hb_bad = api("POST", "/bridge/heartbeat", {"attempt_id": attempt_id, "hb_interval_s": 100000})
check("слишком длинный интервал помечен риском",
      code == 200 and hb_bad.get("lease_risk"), str(code) + " " + str(hb_bad)[:160])
code, hb_unknown = api("POST", "/bridge/heartbeat", {"attempt_id": "nonexistent" + uuid.uuid4().hex[:6]})
check("heartbeat неизвестной попытки — ok=false", code == 200 and hb_unknown.get("ok") is False, str(hb_unknown))

# 6. Результат записывается ровно один раз; повтор — duplicate.
# Стенд работает с LEASE_SECONDS=3, поэтому перед записью результата аренду
# продлеваем: иначе проверялся бы путь late, а не идемпотентность записи.
api("POST", "/bridge/heartbeat", {"attempt_id": attempt_id, "hb_interval_s": interval})
result = {"job_id": job_id, "attempt_id": attempt_id, "ok": True, "output": "ok",
          "usage": {}, "elapsed": 0.1}
code, first = api("POST", "/bridge/result", result)
check("результат применён", code == 200 and first.get("outcome") == "applied" and first.get("recorded"),
      str(code) + " " + str(first))
code, second = api("POST", "/bridge/result", result)
check("повторная доставка — duplicate, без нового исполнения",
      code == 200 and second.get("outcome") == "duplicate" and second.get("recorded"),
      str(code) + " " + str(second))
code, job_state = api("GET", "/jobs/" + job_id)
attempts = job_state.get("job", {}).get("attempts_detail") or job_state.get("attempts_detail") or []
check("у задания ровно одна попытка", len(attempts) == 1, len(attempts))
check("задание завершено успешно",
      (job_state.get("job") or job_state).get("status") == "succeeded",
      (job_state.get("job") or job_state).get("status"))

# 7. Завершённое задание больше не арендуется.
code, after = api("GET", "/bridge/jobs?worker=core01-probe&agents=codex&protocol=2")
check("завершённое задание не арендуется", code == 200 and not after.get("id"), str(after)[:120])

# 8. Диагностика воркеров показывает интервал и риск.
code, workers = api("GET", "/workers")
items = workers.get("workers") or []
if items:
    item = items[0]
    check("в /workers есть интервал подтверждения", "hb_interval_s" in item, list(item)[:12])
    check("в /workers есть оценка риска аренды", "lease_risk" in item, list(item)[:12])
else:
    print("[SKIP] в /workers нет ни одного воркера — проверка риска пропущена")

print()
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("CORE-01 contract: all checks passed")
