"""9.1.3 — идемпотентность с назначением, границы поддержки, профиль в истории.

Проверяет:
1. повтор с тем же ключом и тем же назначением → то же задание (created=false);
2. тот же ключ, другая модель → 409 (не возвращает старое задание);
3. тот же ключ, другой профиль → 409;
4. агент без model_selection: явная модель → 400;
5. агент без model_selection: без модели → 202 (обычный запуск работает);
6. requested_profile сохраняется в задании, истории и аренде;
7. effective_model пишется из usage, когда инструмент его раскрывает.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, uuid

BASE = "http://127.0.0.1:8100"
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
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def sql(q):
    r = subprocess.run(["docker", "exec", "aihub-postgres-test", "psql", "-U", "aihub_test",
                        "-d", "aihub_test", "-t", "-A", "-c", q], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip()


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def new_key(tag):
    return f"i-{tag}-{uuid.uuid4().hex[:10]}"


def test_same_key_same_params():
    key = new_key("same")
    body = {"prompt": "ping", "timeout": 30, "idempotency_key": key,
            "profile": "openai", "model": "gpt-6.1-sol"}
    c1, j1 = api("POST", "/agents/codex/run", body)
    c2, j2 = api("POST", "/agents/codex/run", body)
    check("повтор → 202", c1 == 202 and c2 == 202, f"{c1}/{c2}")
    check("то же задание", j1.get("job_id") == j2.get("job_id"), f"{j1.get('job_id')} vs {j2.get('job_id')}")
    check("created=false на повторе", j2.get("created") is False, str(j2.get("created")))


def test_same_key_other_model():
    key = new_key("model")
    base = {"prompt": "ping", "timeout": 30, "idempotency_key": key, "profile": "openai"}
    c1, j1 = api("POST", "/agents/codex/run", {**base, "model": "gpt-6.1-sol"})
    c2, _ = api("POST", "/agents/codex/run", {**base, "model": "gpt-6.1-terra"})
    check("модель A принята", c1 == 202, str(c1))
    check("смена модели под тем же ключом → 409", c2 == 409, str(c2))
    job_model = sql(f"select requested_model from aihub.jobs where id='{j1.get('job_id')}'")
    check("задание осталось с моделью A", job_model == "gpt-6.1-sol", job_model)


def test_same_key_other_profile():
    # У codex сейчас один профиль, поэтому смену профиля проверяем на уровне
    # db.create_job внутри контейнера: ветка сравнения профиля должна давать конфликт.
    code = """
from app import db
import uuid
k = "dbchk-" + uuid.uuid4().hex[:8]
try:
    db.create_job("codex", "p", "sha", 30, k, None, "gpt-6.1-sol", "openai")
    db.create_job("codex", "p", "sha", 30, k, None, "gpt-6.1-sol", "router-cheap")
    print("NO_CONFLICT")
except ValueError as e:
    print("CONFLICT" if str(e) == "idempotency_key_conflict" else "OTHER:" + str(e))
"""
    r = subprocess.run(["docker", "exec", "aihub-hub-test", "python", "-c", code],
                       capture_output=True)
    out = r.stdout.decode("utf-8", "replace").strip()
    check("смена профиля под тем же ключом → конфликт", out == "CONFLICT", out or r.stderr.decode()[:120])

    # Нормализация: отсутствие профиля равно профилю по умолчанию — не конфликт.
    key = new_key("norm")
    c1, j1 = api("POST", "/agents/codex/run",
                 {"prompt": "ping", "timeout": 30, "idempotency_key": key})
    c2, j2 = api("POST", "/agents/codex/run",
                 {"prompt": "ping", "timeout": 30, "idempotency_key": key, "profile": "openai",
                  "model": "gpt-6.1-sol"})
    check("профиль по умолчанию не конфликтует", c1 == 202 and c2 == 202 and j1.get("job_id") == j2.get("job_id"),
          f"{c1}/{c2} {j1.get('job_id')} {j2.get('job_id')}")


def test_unsupported_agent():
    # qwen-code не объявляет model_selection
    c1, e = api("POST", "/agents/qwen-code/run",
                {"prompt": "ping", "timeout": 30, "idempotency_key": new_key("bad"),
                 "model": "qwen3.8-max"})
    check("агент без поддержки: явная модель → 400", c1 == 400, f"{c1} {e.get('detail','')[:90]}")
    c2, _ = api("POST", "/agents/qwen-code/run",
                {"prompt": "ping", "timeout": 30, "idempotency_key": new_key("ok")})
    check("агент без поддержки: без модели → 202", c2 == 202, str(c2))


def test_profile_persisted():
    key = new_key("persist")
    c, j = api("POST", "/agents/codex/run",
               {"prompt": "ping", "timeout": 30, "idempotency_key": key,
                "profile": "openai", "model": "gpt-6.1-sol"})
    check("запуск с профилем → 202", c == 202, str(c))
    jid = j.get("job_id", "")
    check("requested_profile в БД", sql(f"select requested_profile from aihub.jobs where id='{jid}'") == "openai")
    _, job = api("GET", "/jobs/" + jid)
    check("requested_profile в задании", job.get("requested_profile") == "openai", str(job.get("requested_profile")))
    _, hist = api("GET", "/jobs?limit=1")
    row = (hist.get("jobs") or [{}])[0]
    check("requested_profile в истории", row.get("requested_profile") == "openai", str(row.get("requested_profile")))

    # аренда отдаёт профиль + результат пишет effective_model
    lease = {}
    for _ in range(25):
        c, cand = api("GET", "/bridge/jobs?worker=test-913&agents=codex")
        if cand.get("id") == jid:
            lease = cand
            break
        time.sleep(0.3)
    check("аренда содержит requested_profile", lease.get("requested_profile") == "openai",
          str(lease.get("requested_profile")))
    check("аренда содержит requested_model", lease.get("requested_model") == "gpt-6.1-sol",
          str(lease.get("requested_model")))
    if lease.get("attempt_id"):
        api("POST", "/bridge/result", {"job_id": jid, "attempt_id": lease["attempt_id"], "ok": True,
                                       "output": "OK", "usage": {"model": "gpt-6.1-terra"}, "elapsed": 1.0})
        _, job2 = api("GET", "/jobs/" + jid)
        check("effective_model из usage", job2.get("effective_model") == "gpt-6.1-terra",
              str(job2.get("effective_model")))


if __name__ == "__main__":
    test_same_key_same_params()
    test_same_key_other_model()
    test_same_key_other_profile()
    test_unsupported_agent()
    test_profile_persisted()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} {FAILURES}")
        sys.exit(1)
    print("\nALL 9.1.3 TESTS PASSED")
