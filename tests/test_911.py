"""9.1.1 — сквозной выбор агента, провайдера и модели.

Проверяет:
1. /agent-profiles/{agent} возвращает профили из реестра;
2. запуск с model=... сохраняет requested_model в задании;
3. аренда пробрасывает requested_model воркеру;
4. несовместимое сочетание (неизвестная модель/профиль) отклоняется 400;
5. после результата effective_model заполнен, если инструмент его раскрыл.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, uuid

BASE = "http://127.0.0.1:8100"
TOKEN = os.environ.get("HUB_TOKEN") or sys.exit("HUB_TOKEN env required")
WSL = r"C:\Program Files\WSL\wsl.exe"

FAILURES = []


def api(method, path, body=None, timeout=90):
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
    # Тест исполняется внутри WSL — docker доступен напрямую.
    r = subprocess.run(["docker", "exec", "aihub-postgres-test", "psql", "-U", "aihub_test",
                        "-d", "aihub_test", "-t", "-A", "-c", q], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip()


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_profiles():
    code, d = api("GET", "/agent-profiles/codex")
    check("профили Codex", code == 200 and "profiles" in d, str(code))
    models = [m for p in d.get("profiles", []) for m in p.get("models", [])]
    check("модели Codex не пустые", len(models) > 0, str(models))
    code, d = api("GET", "/agent-profiles/nonexistent")
    check("неизвестный агент → 404", code == 404)


def test_model_selection():
    # Запуск с конкретной моделью
    key = "ct-model-" + uuid.uuid4().hex[:8]
    code, j = api("POST", "/agents/codex/run",
                  {"prompt": "Say hi", "timeout": 30, "idempotency_key": key,
                   "model": "gpt-6.1-sol"})
    check("запуск с model → 202", code == 202, str(code))
    if code != 202:
        return
    job_id = j["job_id"]
    requested = sql(f"select requested_model from aihub.jobs where id='{job_id}'")
    check("requested_model сохранён в БД", requested == "gpt-6.1-sol", requested)

    # Несовместимое сочетание: неизвестная модель
    code, e = api("POST", "/agents/codex/run",
                  {"prompt": "hi", "timeout": 30, "idempotency_key": "ct-bad-" + uuid.uuid4().hex[:8],
                   "model": "gpt-99-nonexistent"})
    check("неизвестная модель → 400", code == 400, f"{code} {e.get('detail','')[:80]}")

    # Неизвестный профиль
    code, e = api("POST", "/agents/codex/run",
                  {"prompt": "hi", "timeout": 30, "idempotency_key": "ct-badp-" + uuid.uuid4().hex[:8],
                   "profile": "no-such-profile"})
    check("неизвестный профиль → 400", code == 400, f"{code} {e.get('detail','')[:80]}")


def test_lease_carries_model():
    # Запуск и аренда: requested_model должен дойти до воркера
    key = "ct-lease-" + uuid.uuid4().hex[:8]
    code, j = api("POST", "/agents/codex/run",
                  {"prompt": "Say OK", "timeout": 30, "idempotency_key": key,
                   "model": "gpt-6.1-terra"})
    job_id = j.get("job_id", "")
    # Аренда: ищем именно наше задание, пропуская чужие queued
    d = {}
    for _ in range(20):
        code, cand = api("GET", "/bridge/jobs?worker=test-911&agents=codex")
        if "attempt_id" in cand and cand.get("id") == job_id:
            d = cand
            break
        time.sleep(0.3)
    got_model = d.get("requested_model")
    check("аренда пробрасывает requested_model", got_model == "gpt-6.1-terra", str(got_model))
    # Отправляем результат как «выполнено воркером» (без реального Codex — стенд)
    if "attempt_id" in d:
        payload = {"job_id": job_id, "attempt_id": d["attempt_id"], "ok": True,
                   "output": "OK", "usage": {"model": "gpt-6.1-terra"}, "elapsed": 1.0}
        code, r = api("POST", "/bridge/result", payload)
        check("результат applied", code == 200 and r.get("outcome") == "applied", str(r))
        code, job = api("GET", "/jobs/" + job_id)
        check("effective_model из usage", job.get("effective_model") == "gpt-6.1-terra",
              str(job.get("effective_model")))
        check("requested_model в задании", job.get("requested_model") == "gpt-6.1-terra",
              str(job.get("requested_model")))


if __name__ == "__main__":
    test_profiles()
    test_model_selection()
    test_lease_carries_model()
    print()
    if FAILURES:
        print("FAILED:", len(FAILURES), FAILURES)
        sys.exit(1)
    print("ALL 9.1.1 TESTS PASSED")