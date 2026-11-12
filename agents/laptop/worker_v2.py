#!/usr/bin/env python3
"""AI-Hub laptop agent — pull-воркер (протокол v2: аренда + heartbeat + outbox).

Цикл:
  1. GET  {HUB}/bridge/jobs?worker=<имя>  — атомарная аренда задания (приходит attempt_id и lease_seconds);
  2. heartbeat-поток продлевает аренду, пока агент работает;
  3. результат сначала пишется в outbox (локальный файл), потом отправляется
     POST {HUB}/bridge/result с повторами до подтверждения — потеря сети не теряет результат;
  4. при старте воркер досылает всё, что осталось в outbox.

Автоматического повторного запуска агента нет: если аренда истекла, хаб сам помечает попытку unknown.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import msvcrt
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bridge import AGENTS, TOKEN, _agent_path, log  # noqa: E402

# CORE-02b: управляемые операции рабочего пространства на этом устройстве.
# Модуль необязателен: без него воркер обслуживает только kind=agent.
try:
    import workspace_host  # noqa: E402
except Exception as _ws_exc:  # noqa: BLE001
    workspace_host = None
    log(f"workspace ops disabled: {_ws_exc}")

KINDS = ["agent"] + (["workspace"] if workspace_host is not None else [])
# Служебный агент workspace: хаб адресует ему control-задания, а фильтр
# аренды отбирает их по списку агентов поллера.
POLL_AGENTS = list(AGENTS) + (["workspace"] if workspace_host is not None else [])

HUB_FILE = os.path.join(HERE, "hub.txt")
OUTBOX = os.path.join(HERE, "outbox")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
WORKER = os.environ.get("COMPUTERNAME", "laptop")
os.makedirs(OUTBOX, exist_ok=True)


def hub_url() -> str:
    try:
        with open(HUB_FILE, encoding="utf-8") as fh:
            return fh.read().strip().rstrip("/")
    except OSError:
        return os.environ.get("HUB_URL", "").rstrip("/")


def http(method: str, url: str, body: dict | None = None, timeout: float = 45) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {"error": f"http {exc.code}"}
        return exc.code, payload


def outbox_path(job_id: str, attempt_id: str) -> str:
    return os.path.join(OUTBOX, f"{job_id}__{attempt_id}.json")


def deliver(path: str) -> bool:
    """Keep the outbox item until the Hub confirms durable result storage."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"deliver retained unreadable outbox file: {type(exc).__name__}")
        return False
    base = hub_url()
    for attempt in range(3):
        try:
            code, resp = http("POST", f"{base}/bridge/result", payload, timeout=60)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log(f"deliver retained {payload.get('attempt_id')}: invalid JSON acknowledgement ({type(exc).__name__})")
            return False
        except Exception as exc:  # noqa: BLE001
            log(f"deliver transport error {payload.get('attempt_id')}: {type(exc).__name__}")
            if attempt < 2:
                time.sleep(5)
                continue
            return False
        outcome = resp.get("outcome") if isinstance(resp, dict) else None
        confirmed = (
            code == 200
            and isinstance(resp, dict)
            and resp.get("ok") is True
            and resp.get("recorded") is True
            and outcome in ("applied", "duplicate", "late", "stale", "unknown")
        )
        if confirmed:
            if (workspace_host is not None
                    and (payload.get("usage") or {}).get("workspace_op") == "apply_patch"):
                workspace_host.mark_effect_reported(
                    payload.get("job_id") or "", payload.get("attempt_id") or "",
                    hashlib.sha256(base.rstrip("/").lower().encode("utf-8")).hexdigest())
            try:
                os.remove(path)
            except OSError as exc:
                log(f"deliver stored but could not remove outbox {payload.get('attempt_id')}: {type(exc).__name__}")
                return False
            return True
        if code in (404, 409):
            log(f"deliver retained {payload.get('attempt_id')}: HTTP {code}, server did not confirm durable storage")
            return False
        if code == 200:
            ack_ok = resp.get("ok") if isinstance(resp, dict) else None
            ack_recorded = resp.get("recorded") if isinstance(resp, dict) else None
            log(f"deliver retained {payload.get('attempt_id')}: invalid acknowledgement ok={ack_ok} recorded={ack_recorded} outcome={outcome}")
            return False
        log(f"deliver retained {payload.get('attempt_id')}: HTTP {code}, server did not confirm durable storage")
        return False
    return False


def drain_outbox() -> None:
    for name in sorted(os.listdir(OUTBOX)):
        if name.endswith(".json"):
            deliver(os.path.join(OUTBOX, name))


def drain_workspace_effect_journals(base: str) -> None:
    """Report durable apply_patch journals left by a prior process crash."""
    if workspace_host is None:
        return
    hub_identity = hashlib.sha256(base.rstrip("/").lower().encode("utf-8")).hexdigest()
    for payload in workspace_host.pending_effect_payloads(hub_identity):
        path = outbox_path(payload["job_id"], payload["attempt_id"])
        if not os.path.exists(path):
            tmp_path = path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, path)
            except OSError as exc:
                log(f"could not persist effect journal outbox: {type(exc).__name__}")
                continue
        deliver(path)


def attempt_heartbeat_interval(lease: int, advertised=None) -> int:
    """Интервал подтверждения аренды: треть аренды, но не больше минуты.

    Раньше здесь было max(20, lease // 3): при короткой тестовой аренде (3 с)
    первое подтверждение приходило на 20-й секунде, уже после истечения, и
    живая попытка становилась unknown. Теперь интервал не длиннее трети
    аренды, а хаб присылает его же в ответе на аренду.
    """
    lease = max(1, int(lease or 0))
    value = int(advertised or 0) or lease // 3 or 1
    return max(1, min(60, value, lease // 3 or 1))


def heartbeat_loop(base: str, attempt_id: str, lease: int, stop: threading.Event,
                   advertised=None, resource_generation: int | None = None,
                   resource_key: str | None = None, lease_lost: threading.Event | None = None) -> None:
    interval = attempt_heartbeat_interval(lease, advertised)
    while not stop.wait(interval):
        try:
            body = {"attempt_id": attempt_id, "hb_interval_s": interval}
            if resource_generation is not None:
                body["resource_generation"] = resource_generation
            code, resp = http("POST", f"{base}/bridge/heartbeat", body, timeout=30)
            resource = resp.get("resource") or {}
            if (code != 200 or not resp.get("ok")
                    or (resource_generation is not None
                        and (resource.get("generation") != resource_generation
                             or resource.get("key") != resource_key))):
                log(f"heartbeat rejected for {attempt_id} (lease not verified)")
                if lease_lost is not None:
                    lease_lost.set()
                return
        except Exception as exc:  # noqa: BLE001
            log(f"heartbeat error {attempt_id}: {exc}")
            if lease_lost is not None:
                lease_lost.set()
            return


LOCK_PATH = os.path.join(HERE, "worker.lock")
_lock_handle = None


def acquire_single_instance() -> bool:
    """Один воркер на хост. Блокировка ОС снимается сама, когда процесс умирает, — чистить lock не нужно."""
    global _lock_handle
    try:
        _lock_handle = open(LOCK_PATH, "a+")
        _lock_handle.seek(0)
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        return False
    _lock_handle.seek(0)
    _lock_handle.truncate()
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True


PROTOCOL = 2
ADAPTER_VERSION = "2.2"
HEARTBEAT_S = int(os.environ.get("WORKER_HEARTBEAT_S", "60"))
_VERSION_CACHE: dict[str, str | None] = {}


def _cli_version(agent_id: str, path: str) -> str | None:
    if agent_id in _VERSION_CACHE:
        return _VERSION_CACHE[agent_id]
    value = None
    if path:
        if path.lower().endswith(".cmd") or path.lower().endswith(".bat"):
            argv = [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/c", path, "--version"]
        elif path.lower().endswith(".js"):
            argv = [r"C:\Program Files\nodejs\node.exe", path, "--version"]
        else:
            argv = [path, "--version"]
        try:
            proc = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
            text = (proc.stdout or b"").decode("utf-8", "replace") if proc.returncode == 0 else ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    value = line[:120]
                    break
        except Exception:  # noqa: BLE001
            value = None
    _VERSION_CACHE[agent_id] = value
    return value


def worker_heartbeat_payload() -> dict:
    served = []
    for agent_id, spec in AGENTS.items():
        item = {"id": agent_id, "capabilities": {"workdir": True}}
        # Путь берём тем же резолвером, которым запускается CLI: закреплённый в
        # реестре путь устаревает после обновления Codex.
        path = _agent_path(agent_id, spec)
        if path:
            item["cli_path"] = path
            try:
                st = os.stat(path)
                item["cli_size"] = st.st_size
                item["cli_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
            except OSError:
                pass
            version = _cli_version(agent_id, path)
            if version:
                item["cli_version"] = version
            ready = os.path.exists(path)
            item["ready"] = ready
            if not ready:
                item["problem"] = "CLI не найден по зарегистрированному пути"
        served.append(item)
    return {
        "worker_id": WORKER,
        "host": platform.node(),
        "os_user": getpass.getuser(),
        "pid": os.getpid(),
        "protocol": PROTOCOL,
        "hb_interval_s": HEARTBEAT_S,
        "adapter_version": ADAPTER_VERSION,
        "agents": served,
        "capabilities": {"workdir": True, "attachments": True, "outbox": "file",
                         "kinds": KINDS,
                         **(workspace_host.capabilities() if workspace_host else {})},
        "identity": {"worker_home": HERE, "python": platform.python_version(), "service": "laptop-worker_v2"},
    }


def send_worker_heartbeat(base: str) -> dict:
    payload = worker_heartbeat_payload()
    try:
        code, resp = http("POST", base + "/bridge/worker/heartbeat", payload, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"worker heartbeat error: {exc}")
        return {"ok": False, "error": str(exc)}
    if code != 200 or not resp.get("ok"):
        log(f"worker heartbeat rejected http={code} {str(resp)[:200]}")
        return {"ok": False, "http": code, "detail": resp}
    return {"ok": True, "http": code, "agents": resp.get("agents"), "worker_id": resp.get("worker_id")}


def worker_heartbeat_loop(base: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_S):
        send_worker_heartbeat(base)


def run_workspace_job(job: dict, base: str) -> dict:
    """Control-задание: операция рабочего пространства с heartbeat и без ретраев."""
    job_id = job.get("id")
    attempt_id = job.get("attempt_id")
    lease = int(job.get("lease_seconds") or 300)
    resource = job.get("resource") or {}
    generation = resource.get("generation")
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop,
                     args=(base, attempt_id, lease, stop, job.get("heartbeat_interval_s"), generation),
                     daemon=True).start()

    def verify_fence() -> bool:
        if not resource.get("key") or not isinstance(generation, int):
            return False
        try:
            code, resp = http("POST", f"{base}/bridge/heartbeat",
                              {"attempt_id": attempt_id, "resource_generation": generation}, timeout=15)
            live = resp.get("resource") or {}
            return (code == 200 and bool(resp.get("ok"))
                    and live.get("key") == resource.get("key")
                    and live.get("generation") == generation)
        except Exception as exc:  # noqa: BLE001
            log(f"fence verification failed for {attempt_id}: {type(exc).__name__}")
            return False
    try:
        if workspace_host is None:
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": "host не поддерживает workspace-операции"}
        else:
            payload = workspace_host.run_job(
                job, fence_check=verify_fence,
                hub_identity=hashlib.sha256(base.rstrip("/").lower().encode("utf-8")).hexdigest())
        log(f"workspace job {job_id} op={(payload.get('usage') or {}).get('workspace_op')} "
            f"ok={payload.get('ok')} {str(payload.get('error') or '')[:160]}")
    except Exception as exc:  # noqa: BLE001
        log("workspace job error " + traceback.format_exc())
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop.set()
    return payload


def task_precheck(job: dict) -> str | None:
    """Сверка рабочей папки и ветки задания до запуска агента.

    Копия задачи создаётся хабом; воркер обязан убедиться, что задание
    выполняется в ней и на ожидаемой ветке (и коммите для тестера/ревьюера).
    Несовпадение — шаг не начинается, файлы не меняются.
    """
    if workspace_host is None:
        # Модуль сверки не загрузился: для копии задачи это отказ, а не пропуск.
        return ("шаг остановлен: сверка рабочей копии недоступна"
                if job.get("task_copy") else None)
    root = str(job.get("workdir") or job.get("project_workdir") or "")
    branch = str(job.get("task_branch") or "")
    if job.get("task_copy"):
        # Копия задачи обязательна: без неё задание выполнялось бы в основной
        # рабочей копии. Это отказ, а не пропуск проверки.
        if not root:
            return "шаг остановлен: задание без рабочей копии задачи"
        if not branch:
            return "шаг остановлен: задание без ветки задачи"
    if not root or not branch:
        return None
    head = str(job.get("expect_head") or "")
    try:
        workspace_host.run_job("verify_task", {"root": root, "branch": branch,
                                               "head_sha": head})
    except workspace_host.OpFail as exc:
        return f"шаг остановлен до изменения файлов: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"сверка рабочей копии не удалась: {exc}"
    return None


def main() -> int:
    if "--heartbeat-once" in sys.argv:
        base = hub_url()
        if not base:
            print(json.dumps({"ok": False, "error": "hub.txt пуст"}, ensure_ascii=False))
            return 2
        result = send_worker_heartbeat(base)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    if not acquire_single_instance():
        log("poller: уже работает другой экземпляр — второй не запускаю")
        print("worker already running", file=sys.stderr)
        return 3
    base = hub_url()
    if not base:
        print("hub.txt пуст — укажи адрес хаба, например http://192.168.31.95:8000", file=sys.stderr)
        return 2
    log(f"poller start hub={base} worker={WORKER} token={'set' if TOKEN else 'none'}")
    threading.Thread(target=worker_heartbeat_loop, args=(base, threading.Event()), daemon=True).start()
    send_worker_heartbeat(base)
    print(f"aihub agent polling {base} as {WORKER}")
    drain_outbox()
    drain_workspace_effect_journals(base)

    while True:
        try:
            code, job = http("GET", f"{base}/bridge/jobs?worker={WORKER}"
                                    f"&agents={','.join(POLL_AGENTS)}&protocol={PROTOCOL}"
                                    f"&kinds={','.join(KINDS)}", timeout=45)
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            time.sleep(5)
            continue

        if code == 426:
            # Хаб требует другую версию протокола: опрос дальше бессмысленен.
            log(f"hub refused protocol {PROTOCOL}: {str(job.get('detail'))[:200]}")
            time.sleep(max(30.0, POLL_INTERVAL * 10))
            continue
        if code != 200:
            time.sleep(POLL_INTERVAL)
            continue

        job_id = job.get("id")
        attempt_id = job.get("attempt_id")
        agent_id = job.get("agent")
        prompt = job.get("prompt")
        if not job_id or not attempt_id or not agent_id or not prompt:
            time.sleep(POLL_INTERVAL)
            continue

        if (job.get("kind") or "agent") == "workspace":
            payload = run_workspace_job(job, base)
            path = outbox_path(job_id, attempt_id)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_path, path)
            deliver(path)
            drain_outbox()
            continue

        spec = AGENTS.get(agent_id)
        if spec is None:
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": f"unknown agent '{agent_id}'"}
            path = outbox_path(job_id, attempt_id)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_path, path)
            deliver(path)
            continue

        lease = int(job.get("lease_seconds") or 300)
        timeout = int(job.get("timeout") or spec["timeout"])
        requested_model = job.get("requested_model")
        requested_profile = job.get("requested_profile")
        workdir = job.get("workdir") or None
        blocked = task_precheck(job)
        if blocked:
            log(f"job {job_id} stopped: {blocked}")
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": blocked}
            path = outbox_path(job_id, attempt_id)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(path + ".tmp", path)
            deliver(path)
            drain_outbox()
            continue
        # Вложения задания: содержимое приходит с хабом; раскладываем во временный каталог,
        # чтобы Codex мог открыть файлы, и перечисляем пути в промпте.
        attachments = job.get("attachments") or []
        if attachments:
            import base64, tempfile
            att_dir = tempfile.mkdtemp(prefix="aihub-att-")
            paths = []
            for att in attachments:
                try:
                    raw = base64.b64decode(att.get("content_b64") or "")
                except Exception:
                    continue
                safe = os.path.basename(att.get("filename") or "file")
                p = os.path.join(att_dir, safe)
                with open(p, "wb") as fh:
                    fh.write(raw)
                paths.append(p)
            if paths:
                prompt += "\n\nФайлы приложены и доступны по путям:\n" + "\n".join(paths)
        stop = threading.Event()
        lease_lost = threading.Event()
        resource_generation = (job.get("resource") or {}).get("generation")
        threading.Thread(target=heartbeat_loop,
                         args=(base, attempt_id, lease, stop, job.get("heartbeat_interval_s"),
                               resource_generation, (job.get("resource") or {}).get("key"),
                               lease_lost),
                         daemon=True).start()

        log(f"job {job_id} attempt={attempt_id} agent={agent_id} timeout={timeout} "
            f"profile={requested_profile} model={requested_model}")
        started = time.time()
        try:
            if agent_id == "codex":
                # Preserve Codex model/cwd behavior; the updated bridge accepts
                # requested_profile and keyword-only cwd while legacy positional
                # model/workdir calls remain supported.
                result = spec["runner"](
                    prompt, timeout, requested_model, requested_profile, cwd=workdir)
            elif agent_id in ("openclaw", "hermes"):
                result = spec["runner"](
                    prompt, timeout, requested_model, requested_profile, cwd=workdir)
            else:
                result = spec["runner"](prompt, timeout)
            ok = result.get("exit_code") == 0
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": ok, **result}
            # The selected model is already stored separately on the job;
            # only a provider-reported model may be sent as the effective value.
            if not ok and not payload.get("error"):
                code = result.get("exit_code")
                tail = (result.get("output") or "").strip()[-500:]
                if code in (-1, 4294967295):
                    payload["error"] = "процесс агента прерван (kill)" + (": " + tail if tail else "")
                else:
                    payload["error"] = tail or f"процесс агента завершился с кодом {code}"
        except subprocess.TimeoutExpired:
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": f"timeout after {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            log("runner error " + traceback.format_exc())
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False, "error": str(exc)}
        finally:
            stop.set()

        if lease_lost.is_set() and (job.get("resource") or {}).get("key"):
            # Native adapter processes cannot be fenced between filesystem
            # syscalls. Preserve their outcome as unknown and let the Hub
            # quarantine the checkout instead of reporting a definite write.
            payload["uncertain"] = True
            payload["error"] = payload.get("error") or "heartbeat lost during native execution"

        payload["elapsed_total"] = round(time.time() - started, 2)
        path = outbox_path(job_id, attempt_id)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
        deliver(path)
        drain_outbox()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
