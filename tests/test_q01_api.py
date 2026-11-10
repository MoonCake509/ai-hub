"""Q-01: живой контракт быстрого режима против стенда :8100."""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("HUB_TOKEN")
if not TOKEN:
    for candidate in (r"C:/Users/user/aihub-worker/token.txt", r"C:/Users/lavy/aihub-bridge/token.txt"):
        if os.path.exists(candidate):
            TOKEN = open(candidate, encoding="utf-8").read().strip()
            break
if not TOKEN:
    sys.exit("HUB_TOKEN or token file required")
FAILURES = []


def api(method, path, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " - " + str(detail)))
    if not cond:
        FAILURES.append(name)


def channels():
    code, body = api("GET", "/quality/channels")
    return code, (body.get("channels") or [])


def pick(chs, want_image):
    for ch in chs:
        caps = ch.get("capabilities") or {}
        image = bool(caps.get("requires_image") or "image" in set(caps.get("required_modalities") or []))
        if image == want_image and ch.get("enabled", True):
            return ch
    return None


def start(payload):
    return api("POST", "/quality/runs", payload)


def wait_run(run_id, seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        code, body = api("GET", "/quality/runs/" + run_id)
        last = (code, body)
        if body.get("verdict", {}).get("complete"):
            return last
        time.sleep(2)
    return last


def main():
    code, chs = channels()
    check("каталог каналов доступен", code == 200 and len(chs) > 0, code)
    text_ch = pick(chs, False)
    image_ch = pick(chs, True)
    check("есть включённый текстовый канал", text_ch is not None)

    key = "q01-live-" + uuid.uuid4().hex[:8]
    code, run = start({"mode": "quick", "agent_id": text_ch["agent_id"],
                       "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                       "budget_s": 10, "idempotency_key": key})
    check("quick запуск принят 202", code == 202, (code, run.get("detail")))
    check("режим сохранён как quick", run.get("mode") == "quick", run.get("mode"))
    check("бюджет сохранён 10 с", run.get("budget_s") == 10, run.get("budget_s"))
    check("кап вызовов 3, а не весь каталог", run.get("max_calls") == 3, run.get("max_calls"))
    check("в наборе ровно 3 короткие пробы", len(run.get("items") or []) == 3, len(run.get("items") or []))
    check("таймаут задачи равен бюджету", run.get("timeout_s") == 10, run.get("timeout_s"))
    check("вердикт в начале - идёт проверка", run.get("verdict", {}).get("state") == "running", run.get("verdict"))

    code, again = start({"mode": "quick", "agent_id": text_ch["agent_id"],
                         "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                         "budget_s": 10, "idempotency_key": key})
    check("повтор ключа возвращает тот же прогон", code == 200 and again.get("id") == run.get("id"), (code, again.get("id")))
    code, _ = start({"mode": "extended", "agent_id": text_ch["agent_id"],
                     "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                     "case_count": 2, "idempotency_key": key})
    check("другой режим на том же ключе -> 409", code == 409, code)

    for bad in (9, 121, "45", True):
        code, _ = start({"mode": "quick", "agent_id": text_ch["agent_id"],
                         "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                         "budget_s": bad, "idempotency_key": uuid.uuid4().hex})
        check("budget_s=%r отклонён 400" % (bad,), code == 400, code)
    code, _ = start({"mode": "quick", "agent_id": text_ch["agent_id"],
                     "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                     "case_ids": ["json-shape"], "idempotency_key": uuid.uuid4().hex})
    check("quick не принимает свой список проб", code == 400, code)
    code, _ = start({"mode": "turbo", "agent_id": text_ch["agent_id"],
                     "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                     "idempotency_key": uuid.uuid4().hex})
    check("неизвестный режим отклонён 400", code == 400, code)

    started = time.time()
    code, final = wait_run(run["id"], 45)
    elapsed = time.time() - started
    check("прогон завершился по бюджету, а не завис", final.get("status") in ("completed", "failed"),
          final.get("status"))
    check("ожидание не превысило бюджет на минуты", elapsed < 40, round(elapsed, 1))
    check("бюджет помечен исчерпанным", final.get("budget_exhausted") is True, final.get("budget_exhausted"))
    items = final.get("items") or []
    check("незапущенные пробы помечены бюджетом, а не ошибкой",
          all(i.get("status") == "budget_exhausted" for i in items),
          [i.get("status") for i in items])
    check("у каждой пропущенной пробы есть причина",
          all(i.get("grader_reason") for i in items), [i.get("grader_reason") for i in items])
    verdict = final.get("verdict") or {}
    check("вердикт честный: данных недостаточно", verdict.get("state") == "insufficient", verdict)
    check("пропуски не считаются провалами", (verdict.get("failed_count") or 0) == 0, verdict)
    check("в вердикте есть объяснение", len(verdict.get("reasons") or []) > 0, verdict)
    check("балл не выдуман", final.get("score") is None, final.get("score"))

    code, ext = start({"mode": "extended", "agent_id": text_ch["agent_id"],
                       "profile": text_ch.get("profile"), "model": text_ch.get("model"),
                       "case_count": 2, "timeout_s": 30, "idempotency_key": uuid.uuid4().hex})
    check("extended по-прежнему принимается", code == 202, (code, ext.get("detail")))
    check("у extended нет бюджета", ext.get("budget_s") is None, ext.get("budget_s"))
    check("у extended свой таймаут", ext.get("timeout_s") == 30, ext.get("timeout_s"))

    if image_ch:
        key = "q01-img-" + uuid.uuid4().hex[:8]
        code, img = start({"mode": "quick", "agent_id": image_ch["agent_id"],
                           "profile": image_ch.get("profile"), "model": image_ch.get("model"),
                           "budget_s": 30, "idempotency_key": key})
        check("quick принят для OCR-канала", code == 202, (code, img.get("detail")))
        items = img.get("items") or []
        check("у OCR-канала одна проба, а не текстовый набор", len(items) == 1, len(items))
        case = (items[0].get("case") if items else {}) or {}
        fixture = case.get("fixture") or {}
        check("проба ссылается на PNG", fixture.get("kind") == "image/png", fixture)
        check("ожидаемый текст известен", fixture.get("text") == "LUNA 73", fixture)
        code, _ = start({"mode": "extended", "agent_id": image_ch["agent_id"],
                         "profile": image_ch.get("profile"), "model": image_ch.get("model"),
                         "case_count": 2, "idempotency_key": uuid.uuid4().hex})
        check("extended для OCR-канала отклонён с причиной", code == 400, code)
    else:
        print("[SKIP] OCR-канал с поддержкой изображений в каталоге не найден")

    print()
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL Q-01 LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
