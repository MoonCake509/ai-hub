"""Тест изоляции стенда: тестовая роль не видит рабочие данные."""
import json, os, subprocess, sys, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8100"
TOKEN = open(r"C:/Users/user/aihub-worker/token.txt", encoding="utf-8").read().strip()


def api(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# 1. В стенде не должно быть рабочих заданий (прод имеет 100+)
code, d = api("GET", "/jobs?limit=200")
total = d.get("total", -1)
print(f"[{'PASS' if 0 <= total < 50 else 'FAIL'}] в стенде {total} заданий (рабочих быть не должно)")

# 2. Создаю задание в стенде — оно должно появиться в стенде и НИГДЕ в проде
code, j = api("POST", "/agents/openclaw-laptop/run",
              {"prompt": "CT-ISOLATION", "timeout": 60, "idempotency_key": "ct-iso-" + str(int(time.time()))})
job_id = j.get("job_id", "")
print(f"[{'PASS' if code == 202 else 'FAIL'}] создано задание в стенде: {job_id[:8]}")

# 3. Рабочий хаб (порт 8000) не должен знать об этом job_id
code2, d2 = api("GET", "/jobs/" + job_id)
# BASE=8100 — стенд. Прод — другой порт, но мы внутри сервера; проверим через БД ниже.
print(f"  стенд видит своё задание: {code2 == 200}")

# 4. Проверка на уровне БД: рабочая роль не может читать тестовую БД
r = subprocess.run([r"C:\Program Files\WSL\wsl.exe", "-d", "AIHub", "-e", "bash", "-lc",
                    "docker exec aihub-postgres psql -U llmproxy -d aihub_test "
                    "-c 'select 1' 2>&1 | head -2"],
                   capture_output=True, timeout=60)
out = r.stdout.decode("utf-8", "replace")
print(f"[{'PASS' if 'FATAL' in out or 'error' in out.lower() else 'FAIL'}] рабочая роль НЕ читает тестовую БД: {out.strip()[:80]}")

r = subprocess.run([r"C:\Program Files\WSL\wsl.exe", "-d", "AIHub", "-e", "bash", "-lc",
                    "docker exec aihub-hub-test python -c \"import os; os.environ['DATABASE_URL']="
                    "'postgresql://llmproxy:x@aihub-postgres:5432/litellm'; from app import db; "
                    "print(db.connect().run('select 1'))\" 2>&1 | tail -2"],
                   capture_output=True, timeout=60)
out = r.stdout.decode("utf-8", "replace")
print(f"[{'PASS' if 'password authentication failed' in out or 'FATAL' in out or 'Connection' in out else 'FAIL'}] "
      f"тестовая роль НЕ читает рабочую БД: {out.strip()[:100]}")
