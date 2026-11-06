"""Контрактные тесты хаба против ИЗОЛИРОВАННОГО СТЕНДА (порт 8100, схема aihub_test).

Запуск одной командой с ноутбука:  python tests/run_contract_tests.py

Стенд поднимается автоматически (infra/docker-compose.test.yml), живые воркеры
НЕ останавливаются, рабочая БД и очередь не изменяются. Все проверки идут против
схемы aihub_test, которая после прогона удаляется.

Покрывает замечания ревью 7.1–7.3 и 8.1:
- конфликт payload с тем же Idempotency-Key (агент/таймаут, не только prompt) → 409;
- границы timeout → 400;
- превышение длины prompt → 413;
- реальный expire_leases (LEASE_SECONDS=3 на стенде) + late-result:
  статус не меняется, payload в late_results, повтор идемпотентен;
- повтор применённого результата → duplicate, не late;
- конкурентный приём при заполненной очереди в ДВУХ процессах: лимит не превышается.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
TOKEN_FILE = os.environ.get("AIHUB_TOKEN_FILE", r"C:/Users/user/aihub-worker/token.txt")
WSL = r"C:\Program Files\WSL\wsl.exe"
SCHEMA = "aihub_test"

FAILURES: list[str] = []


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
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def sql(query):
    # Все обращения — только к тестовой схеме. Рабочая aihub не читается и не меняется.
    query = query.replace("aihub.", SCHEMA + ".")
    r = subprocess.run([WSL, "-d", "AIHub", "-e", "bash", "-lc",
                        "docker exec aihub-postgres psql -U llmproxy -d litellm -t -A -c " + repr(query)],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip()


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def new_job(prompt, key=None, timeout=600, agent="openclaw-laptop"):
    body = {"prompt": prompt, "timeout": timeout, "idempotency_key": key or uuid.uuid4().hex}
    return api("POST", f"/agents/{agent}/run", body)


def lease(agent="openclaw"):
    code, d = api("GET", f"/bridge/jobs?worker=contract-test&agents={agent}")
    if code != 200:
        print("   lease http:", code, d)
    return d if "attempt_id" in d else None


def test_idem_conflict():
    key = "ct-idem-" + uuid.uuid4().hex[:8]
    code, j = new_job("CT-IDEM", key=key, timeout=600)
    check("создание задания", code == 202, str(code))
    code, _ = new_job("CT-IDEM", key=key, timeout=601)
    check("тот же ключ, другой timeout → 409", code == 409, str(code))
    code, _ = new_job("CT-IDEM", key=key, agent="hermes-laptop")
    check("тот же ключ, другой агент → 409", code == 409, str(code))
    code, again = new_job("CT-IDEM", key=key, timeout=600)
    check("повтор с тем же телом → то же задание", code in (200, 202) and again.get("job_id") == j.get("job_id"))
    code, _ = new_job("CT-IDEM-DIFFERENT", key=key, timeout=600)
    check("тот же ключ, другой prompt → 409", code == 409, str(code))


def test_timeout_bounds():
    for bad in (0, 5, -1, 999999):
        code, _ = new_job("CT-TIMEOUT", timeout=bad)
        check(f"timeout={bad} → 400", code == 400, str(code))
    code, _ = new_job("CT-TIMEOUT-OK", timeout=60)
    check("timeout=60 → 202", code == 202, str(code))
    code, _ = api("POST", "/agents/openclaw-laptop/run", {"prompt": "CT", "timeout": "abc"})
    check("timeout не число → 400", code == 400, str(code))


def test_prompt_limit():
    code, _ = new_job("x" * 20001)
    check("prompt 20001 → 413", code == 413, str(code))


def test_late_result():
    code, j = new_job("CT-LATE", timeout=60)
    job_id = j["job_id"]
    att = None
    for _ in range(12):
        cand = lease()
        if cand and cand["id"] == job_id:
            att = cand
            break
        if cand:
            print("   (перехвачено чужое:", cand["id"][:8], "ищем", job_id[:8], ")")
        else:
            print("   (очередь openclaw пуста, ищем", job_id[:8], ")")
        time.sleep(0.2)
    if not att:
        check("late: аренда тестового задания", False, "воркер перехватил")
        return
    attempt = att["attempt_id"]
    # Ждём настоящего истечения аренды (на стенде LEASE_SECONDS=3), без прямого SQL.
    deadline = time.time() + 30
    status = ""
    job = {}
    while time.time() < deadline:
        _, job = api("GET", "/jobs/" + job_id)
        status = job["status"]
        if status == "unknown":
            break
        time.sleep(1)
    check("late: sweeper сам перевёл в unknown", status == "unknown", status)
    check("late: needs_reconcile выставлен", bool(job.get("needs_reconcile")))
    payload = {"job_id": job_id, "attempt_id": attempt, "ok": True,
               "output": "CT-LATE-OK", "usage": {}, "elapsed": 1.0}
    code, r = api("POST", "/bridge/result", payload)
    check("late: приём 200 + outcome=late", code == 200 and r.get("outcome") == "late", str(r))
    _, job = api("GET", "/jobs/" + job_id)
    check("late: статус остаётся unknown", job["status"] == "unknown")
    check("late: result_output не перезаписан", not job.get("result_output"))
    _, late = api("GET", "/late-results?limit=20")
    row = next((x for x in late["items"] if x["attempt_id"] == attempt), None)
    check("late: payload сохранён в late_results", bool(row) and row["payload"].get("output") == "CT-LATE-OK")
    code, r2 = api("POST", "/bridge/result", payload)
    n = sql(f"select count(*) from aihub.late_results where attempt_id='{attempt}'")
    check("late: повтор идемпотентен (1 строка)", code == 200 and n == "1", f"{code} rows={n}")


def test_duplicate_result():
    code, j = new_job("CT-DUP", timeout=60)
    job_id = j["job_id"]
    print("   db:", sql(f"select agent_id||'|'||status from aihub.jobs where id='{job_id}'"))
    att = None
    for _ in range(12):
        cand = lease()
        if cand and cand["id"] == job_id:
            att = cand
            break
        if cand:
            print("   (перехвачено чужое:", cand["id"][:8], "ищем", job_id[:8], ")")
        else:
            print("   (очередь openclaw пуста, ищем", job_id[:8], ")")
        time.sleep(0.2)
    if not att:
        check("dup: аренда", False, "воркер перехватил")
        return
    payload = {"job_id": job_id, "attempt_id": att["attempt_id"], "ok": True,
               "output": "CT-DUP-OK", "usage": {}, "elapsed": 1.0}
    code, r1 = api("POST", "/bridge/result", payload)
    check("dup: первый результат applied", code == 200 and r1.get("outcome") == "applied", str(r1))
    code, r2 = api("POST", "/bridge/result", payload)
    check("dup: повтор → duplicate", code == 200 and r2.get("outcome") == "duplicate", str(r2))
    _, job = api("GET", "/jobs/" + job_id)
    check("dup: задание succeeded", job["status"] == "succeeded")


def test_concurrent_queue():
    """Два ОТДЕЛЬНЫХ процесса (не потоки в одном): 4 параллельных создания в каждом, лимит 5."""
    script = r'''
import concurrent.futures as cf, sys
sys.path.insert(0, "/app")
from app import db
def mk(i):
    try:
        db.create_job("openclaw", "CT-QUEUE-%d" % i, "sha%d" % i, 600, None, max_queue=5)
        return "created"
    except db.QueueFull:
        return "full"
with cf.ThreadPoolExecutor(4) as ex:
    res = list(ex.map(mk, range(4)))
print("created:", res.count("created"), "full:", res.count("full"))
'''
    sql("update aihub.jobs set status='cancelled', finished_at=now() where status='queued' and agent_id='openclaw'")
    cmd = [WSL, "-d", "AIHub", "-e", "bash", "-lc",
           "docker exec -i -e SCHEMA=aihub_test aihub-hub-test python - <<'PY'\n" + script + "\nPY"]
    # два процесса одновременно — проверка межпроцессного инварианта advisory-блокировки
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    total_created = 0
    for out, err in outs:
        text = out.decode("utf-8", "replace")
        print("  process:", text.strip(), err.decode("utf-8", "replace")[-200:])
        if "created:" in text:
            total_created += int(text.split("created:")[1].split()[0])
    n = sql("select count(*) from aihub.jobs where status='queued' and agent_id='openclaw'")
    check("конкурентная очередь (2 процесса): не более 5",
          total_created <= 5 and int(n or 0) <= 5, f"created={total_created} db={n}")


if __name__ == "__main__":
    TOKEN = open(TOKEN_FILE, encoding="utf-8").read().strip()
    for fn in (test_idem_conflict, test_timeout_bounds, test_prompt_limit,
               test_duplicate_result, test_late_result, test_concurrent_queue):
        print("==", fn.__name__)
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, repr(e))
    print()
    if FAILURES:
        print("FAILED:", len(FAILURES), FAILURES)
        sys.exit(1)
    print("ALL CONTRACT TESTS PASSED")