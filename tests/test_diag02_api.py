"""DIAG-02: живой контракт проб против стенда :8100.

Проверяет на реальном контейнере: план без сети, разные причины отказов
на разных сервисах, запись наблюдения доступности, повтор внутри TTL и
принудительный прогон. Пробы агентов на хосте выполняет воркер (DIAG-03).
"""
import json
import os
import sys
import urllib.error
import urllib.request

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
        return e.code, {}


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(detail)))
    if not cond:
        FAILURES.append(name)


code, plan = api("GET", "/probes/plan")
items = plan.get("items") or []
check("план отдаётся без сети", code == 200 and len(items) == 2, str(code) + " " + str(len(items)))
check("в плане litellm и postgres", {i["agent_id"] for i in items} == {"litellm", "postgres"}, items)

code, run = api("POST", "/probes/run", {"force": True})
results = run.get("results") or []
by_id = {r["agent_id"]: r for r in results}
check("прогон выполнен", code == 200 and run.get("ran") == 2, str(code) + " ran=" + str(run.get("ran")))
for r in results:
    print("   ", r["agent_id"], r["state"], "|", r.get("reason"), "|", r.get("detail"))
check("у каждого результата причина или ok",
      all(r.get("reason") or r.get("state") == "ok" for r in results), results)
check("причины различаются по существу, а не одним словом",
      len({(r.get("state"), (r.get("reason") or "")[:24]) for r in results}) == len(results), results)
check("postgres проверен по TCP", by_id.get("postgres", {}).get("probe") == "tcp_port", by_id.get("postgres"))
check("litellm проверен по HTTP", by_id.get("litellm", {}).get("probe") == "http_json", by_id.get("litellm"))

code, agents = api("GET", "/agents")
probed = {a["id"]: a for a in agents.get("agents") or []}
check("карточки агентов не подменяются пробами сервисов",
      all((a.get("observations") or {}).get("availability", {}).get("source") != "probe:tcp_port"
          for a in agents.get("agents") or []), True)

code, again = api("POST", "/probes/run", {})
check("повтор внутри TTL не ходит в сеть", again.get("reused") == 2, again.get("reused"))
code, forced = api("POST", "/probes/run", {"force": True})
check("force перепроверяет", forced.get("reused") == 0 and forced.get("ran") == 2, forced.get("reused"))

code, only = api("POST", "/probes/run", {"agent_id": "postgres", "force": True})
check("можно проверить один сервис", code == 200 and len(only.get("results") or []) == 1, code)
code, none = api("POST", "/probes/run", {"agent_id": "codex"})
check("неизвестная цель → 404", code == 404, code)

print()
if FAILURES:
    print("FAILED:", len(FAILURES), FAILURES)
    sys.exit(1)
print("DIAG-02 API CHECKS PASSED")
