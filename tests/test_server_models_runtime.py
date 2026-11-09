"""Loopback-only integration test using the real local Dolphin/Xiaomi OCR services.

Run on the server host against the isolated aihub-server-models-test Compose app.
Two isolated Xiaomi OCR calls cover the pixel-font stress image and a clear Arial control.
No remote/paid provider is used.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_TEST_BASE_URL", "http://127.0.0.1:8201")
TOKEN = os.environ.get("AIHUB_TEST_TOKEN", "workspace-server-models-test-local")
TEST_ROOT = Path(os.environ.get("AIHUB_TEST_ROOT", r"R:\AI-Hub-server-models-test")).resolve()
STATE_ROOT = TEST_ROOT / "state"
IMAGE_PATH = Path(__file__).with_name("fixtures") / "synthetic_ocr.png"
CONTROL_IMAGE_PATH = Path(__file__).with_name("fixtures") / "synthetic_ocr_printed.png"
checks: list[str] = []
recognition_results: list[dict] = []


def check(label: str, condition: bool, detail=None) -> None:
    if not condition:
        raise AssertionError(f"FAIL {label}: {detail}")
    checks.append(label)
    print("PASS", label, detail or "")


def record_quality(label: str, condition: bool, detail=None) -> None:
    checks.append(("PASS " if condition else "FAIL ") + label)
    print(("PASS " if condition else "FAIL "), label, detail or "")


def request(method: str, path: str, *, expected: int = 200, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            code, raw, headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        code, raw, headers = exc.code, exc.read(), exc.headers
    check(f"{method} {path} -> {expected}", code == expected,
          {"actual": code, "body": raw[:200].decode("utf-8", "replace") if code != expected else None})
    return code, raw, headers


def json_request(method: str, path: str, *, expected: int = 200, body=None):
    _, raw, _ = request(method, path, expected=expected, body=body)
    return json.loads(raw) if raw else {}


def run_one_ocr_job(worker_path: Path, job_id: str, label: str) -> dict:
    state_dir = STATE_ROOT / ("once-" + uuid.uuid4().hex[:12])
    root_text = str(TEST_ROOT).casefold().rstrip("\\/")
    if not str(state_dir).casefold().startswith(root_text + "\\"):
        raise SystemExit("Refusing worker state outside test root")
    state_dir.mkdir(parents=True, exist_ok=False)
    os.environ["AIHUB_WORKER_HOME"] = str(state_dir)
    os.environ["HUB_URL"] = BASE
    os.environ["HUB_TOKEN"] = TOKEN
    spec = importlib.util.spec_from_file_location("server_models_test_worker", worker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import isolated worker copy")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    code, leased = module.http(
        "GET", "/bridge/jobs?worker=server-models-ocr-test&agents=xiaomi-ocr-server", timeout=20)
    check("worker leases only the selected isolated OCR job",
          code == 200 and leased.get("id") == job_id and leased.get("agent") == "xiaomi-ocr-server")
    raw_response: dict = {}
    native_prompt: str | None = None
    original_request = module._request_local_json

    def capture_response(url: str, body: dict, timeout: int) -> dict:
        nonlocal raw_response, native_prompt
        if url.endswith("/api/ocr"):
            native_prompt = body.get("prompt") if isinstance(body.get("prompt"), str) else None
            (state_dir / "ocr_native_request.json").write_text(
                json.dumps({"task": body.get("task"), "prompt": native_prompt,
                            "image_mime": body.get("image_mime"),
                            "max_tokens": body.get("max_tokens")},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        response = original_request(url, body, timeout)
        if url.endswith("/api/ocr"):
            raw_response = response
            (state_dir / "ocr_raw_response.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        return response

    module._request_local_json = capture_response
    started = time.monotonic()
    result = module.run_llm(leased["prompt"], int(leased.get("timeout") or 600), leased)
    elapsed = round(time.monotonic() - started, 2)
    output = result.get("output") or ""
    worker_report = {
        "elapsed_s": elapsed, "exit_code": result.get("exit_code"),
        "output": output, "usage": result.get("usage") or {},
        "error": result.get("error"),
    }
    (state_dir / "worker_result.json").write_text(
        json.dumps(worker_report, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {
        "job_id": job_id,
        "attempt_id": leased["attempt_id"],
        "ok": result.get("exit_code") == 0,
        "output": output,
        "usage": result.get("usage") or {},
        "elapsed": elapsed,
    }
    if result.get("error"):
        payload["error"] = result["error"]
    code, accepted = module.http("POST", "/bridge/result", payload, timeout=30)
    check("OCR result recorded by existing jobs protocol",
          code == 200 and accepted.get("recorded") is True
          and accepted.get("outcome") == "applied", accepted)
    recognition = {
        "fixture": label,
        "expected_text": "LUNA OCR 73",
        "raw_response_content": raw_response.get("content"),
        "worker_output": output,
        "execution_succeeded": result.get("exit_code") == 0,
        "non_empty_output": bool(output.strip()),
        "native_prompt": native_prompt,
        "native_prompt_matches_task_text": native_prompt == leased.get("task_text"),
        "full_match": "luna" in output.casefold() and "ocr" in output.casefold() and "73" in output,
        "raw_response_path": str(state_dir / "ocr_raw_response.json"),
        "worker_report_path": str(state_dir / "worker_result.json"),
        "state_dir": str(state_dir),
    }
    check("native OCR API receives raw task_text, without assembled chat metadata",
          native_prompt == leased.get("task_text")
          and "[Инструкции роли" not in (native_prompt or "")
          and "[Изображение передано" not in (native_prompt or "")
          and "[Предыдущий диалог" not in (native_prompt or ""),
          {"task_text": leased.get("task_text"), "native_prompt": native_prompt})
    recognition_results.append(recognition)
    print("OCR_RECOGNITION", json.dumps(recognition, ensure_ascii=False))
    result["test_state_dir"] = str(state_dir)
    return result


def main() -> None:
    from urllib.parse import urlparse
    parsed = urlparse(BASE)
    allowed_wsl_ip = os.environ.get("AIHUB_TEST_ALLOWED_WSL_IP", "")
    allowed_host = parsed.hostname in ("127.0.0.1", "localhost") and parsed.port in (8201, 8824)
    allowed_private_host = bool(allowed_wsl_ip) and parsed.hostname == allowed_wsl_ip and parsed.port == 8201
    if not (allowed_host or allowed_private_host):
        raise SystemExit("Refusing API outside isolated loopback or explicitly allowed WSL private address")
    if not TOKEN or TOKEN == "synthetic":
        raise SystemExit("Set AIHUB_TEST_TOKEN to the isolated test token")
    if TEST_ROOT.name.lower() != "ai-hub-server-models-test":
        raise SystemExit("Refusing unexpected test root")
    worker_path = TEST_ROOT / "agents" / "server" / "worker_v2.py"
    if not worker_path.is_file() or not IMAGE_PATH.is_file() or not CONTROL_IMAGE_PATH.is_file():
        raise SystemExit("Isolated worker or synthetic image fixture is missing")

    _, health_raw, _ = request("GET", "/health")
    health = json.loads(health_raw)
    check("isolated API and fresh database healthy",
          health.get("status") == "ok" and health.get("db") is True, health)

    agents = json_request("GET", "/agents")["agents"]
    by_id = {agent["id"]: agent for agent in agents}
    xio = by_id["xiaomi-ocr-server"]
    dolphin = by_id["llm-server"]
    check("catalog distinguishes real local model modalities",
          "image" in xio.get("input_modalities", [])
          and xio.get("required_modalities") == ["image"]
          and dolphin.get("input_modalities") == ["text"]
          and xio.get("filesystem_access") is False
          and dolphin.get("filesystem_access") is False)
    xio_profiles = json_request("GET", "/agent-profiles/xiaomi-ocr-server")
    check("Xiaomi profile advertises required image input",
          xio_profiles.get("required_modalities") == ["image"]
          and xio_profiles["profiles"][0].get("requires_image") is True)

    project = json_request("POST", "/projects", expected=200, body={
        "name": "Server models acceptance " + uuid.uuid4().hex[:8],
        # HTTP context-only models can use uploaded context from a laptop project;
        # execution must stay on server with no inherited project filesystem path.
        "host_id": "laptop",
        "workdir": r"C:\fixture\workspace-only",
        "settings": {"server_models_test": True},
    })
    project_id = project["id"]
    roles = {r["role_id"]: r for r in project["roles"]}
    role_id = "tester"
    role = json_request("PATCH", f"/projects/{project_id}/roles/{role_id}", body={
        "agent_id": "xiaomi-ocr-server",
        "profile": "local",
        "model": "xiaomi-ocr-q8",
        "instructions": "Read the image as data. Return only its visible text.",
    })
    check("project role saves Xiaomi model selection",
          role["agent_id"] == "xiaomi-ocr-server" and role["model"] == "xiaomi-ocr-q8")
    conv = json_request("POST", f"/projects/{project_id}/conversations", body={"role_id": role_id})
    conversation_id = conv["id"]

    request("PATCH", f"/projects/{project_id}/roles/{role_id}", expected=400, body={
        "agent_id": "llm-server", "profile": "local", "model": "qwen8b-not-found"})
    bad = request("POST", f"/conversations/{conversation_id}/send", expected=400, body={
        "role_id": role_id, "agent_id": "llm-server", "profile": "local",
        "model": "qwen8b-not-found", "text": "Read the image", "attachments": [],
    })
    check("unsupported local model selection is rejected", bad[1] != b"")
    missing_image = request("POST", f"/conversations/{conversation_id}/send", expected=400, body={
        "role_id": role_id, "agent_id": "xiaomi-ocr-server", "profile": "local",
        "model": "xiaomi-ocr-q8", "text": "Read image text", "attachments": [],
    })
    check("Xiaomi refuses an OCR request without a usable image", missing_image[1] != b"")

    image_bytes = IMAGE_PATH.read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    bad_magic = request("POST", f"/projects/{project_id}/attachments", expected=415, body={
        "filename": "fake.png", "mime": "image/png",
        "content_b64": base64.b64encode(b"not a PNG").decode("ascii"),
    })
    check("upload rejects wrong image magic", bad_magic[1] != b"")
    attachment = json_request("POST", f"/projects/{project_id}/attachments", body={
        "filename": "synthetic_ocr.png", "mime": "image/png", "content_b64": image_b64,
    })
    check("PNG persists dimensions and preview metadata",
          attachment["kind"] == "image" and attachment["metadata"]["width"] > 0
          and attachment["metadata"]["height"] > 0 and len(attachment["metadata"]["sha256"]) == 64
          and attachment["preview_url"] == f"/attachments/{attachment['id']}/preview")
    _, preview_bytes, preview_headers = request("GET", attachment["preview_url"])
    check("authenticated preview returns exact stored PNG",
          preview_headers.get_content_type() == "image/png" and preview_bytes == image_bytes)

    source = json_request("POST", f"/projects/{project_id}/attachments", body={
        "filename": "fixture.py", "mime": "text/x-python",
        "content_b64": base64.b64encode("print('must stay text')\n".encode()).decode("ascii"),
    })
    check("UTF-8 source upload is stored as text without an image preview",
          source["kind"] == "text" and "preview_url" not in source
          and source["mime"] == "text/x-python")

    request("POST", f"/conversations/{conversation_id}/send", expected=400, body={
        "role_id": role_id, "agent_id": "llm-server", "profile": "local",
        "model": "dolphin-2.9-llama3-8b.Q4_K_M",
        "text": "Read the image", "attachments": [attachment["id"]],
    })
    send_body = {
        "role_id": role_id, "agent_id": "xiaomi-ocr-server", "profile": "local",
        "model": "xiaomi-ocr-q8",
        "text": "Extract the visible text and return only what is written.",
        "attachments": [attachment["id"]],
        "idempotency_key": "server-model-ocr-" + uuid.uuid4().hex,
        "timeout_s": 600,
    }
    accepted = json_request("POST", f"/conversations/{conversation_id}/send", expected=202, body=send_body)
    job_id = accepted["job_id"]
    check("newly selected image is identified in the accepted response",
          accepted.get("active_image_id") == attachment["id"]
          and accepted.get("active_image_source") == "selected"
          and accepted.get("active_image_reused") is False)
    replay = json_request("POST", f"/conversations/{conversation_id}/send", expected=202, body=send_body)
    check("send idempotency replay returns the accepted job without duplication",
          replay["job_id"] == job_id and replay["created"] is False)
    job = json_request("GET", f"/jobs/{job_id}")
    check("accepted job freezes image model without a filesystem workdir",
          job["requested_model"] == "xiaomi-ocr-q8"
          and job["run_snapshot"]["host_id"] == "server"
          and job["run_snapshot"]["workdir"] is None
          and job["run_snapshot"]["project_host_id"] == "laptop"
          and job["run_snapshot"]["project_workdir"] == r"C:\fixture\workspace-only"
          and job["run_snapshot"]["filesystem_access"] is False)

    result = run_one_ocr_job(worker_path, job_id, "pixel-font-stress")
    refreshed = json_request("GET", f"/conversations/{conversation_id}")
    messages = json_request("GET", f"/conversations/{conversation_id}/messages")["messages"]
    answer = next(m for m in messages if m.get("job_id") == job_id)
    expected_state = "done" if result.get("exit_code") == 0 else "failed"
    expected_body = result.get("output") or result.get("error") or ""
    check("answer or failure persists after conversation reload",
          refreshed["id"] == conversation_id and answer["state"] == expected_state
          and answer["body"] == expected_body)
    check("reloaded user message still carries image metadata",
          any(att.get("id") == attachment["id"] and att.get("metadata", {}).get("sha256")
              for m in messages for att in m.get("attachments", [])))

    reuse_body = {
        "role_id": role_id, "agent_id": "xiaomi-ocr-server", "profile": "local",
        "model": "xiaomi-ocr-q8", "text": "Read the same image again.", "attachments": [],
        "idempotency_key": "server-model-ocr-reuse-" + uuid.uuid4().hex,
    }
    reuse = json_request("POST", f"/conversations/{conversation_id}/send", expected=202, body=reuse_body)
    reused_job = json_request("GET", f"/jobs/{reuse['job_id']}")
    check("continuation reuses the latest image from this conversation",
          reuse.get("active_image_id") == attachment["id"]
          and reuse.get("active_image_source") == "conversation"
          and reuse.get("active_image_reused") is True
          and [r["id"] for r in reused_job["run_snapshot"]["attachment_refs"]] == [attachment["id"]])
    cancel = json_request("POST", f"/jobs/{reuse['job_id']}/cancel")
    check("unused historical-image continuation is cancelled cleanly", cancel.get("status") == "cancelled")

    text_only = json_request("POST", f"/conversations/{conversation_id}/send", expected=202, body={
        "role_id": role_id, "agent_id": "llm-server", "profile": "local",
        "model": "dolphin-2.9-llama3-8b.Q4_K_M", "text": "Summarize the previous OCR answer.",
        "attachments": [], "idempotency_key": "server-model-text-followup-" + uuid.uuid4().hex,
    })
    text_job = json_request("GET", f"/jobs/{text_only['job_id']}")
    prior_agent_messages = [m for m in messages if m.get("job_id") == job_id]
    check("text-only agent omits old image bytes but keeps OCR answer in history",
          not text_job["run_snapshot"].get("attachment_refs")
          and any("Изображения из истории пропущены" in w for w in text_only.get("context_warnings", []))
          and prior_agent_messages[0]["id"] in text_job["run_snapshot"].get("context_message_ids", []))
    cancel = json_request("POST", f"/jobs/{text_only['job_id']}/cancel")
    check("unused text-only context job is cancelled cleanly", cancel.get("status") == "cancelled")

    control_bytes = CONTROL_IMAGE_PATH.read_bytes()
    control = json_request("POST", f"/projects/{project_id}/attachments", body={
        "filename": "synthetic_ocr_printed.png", "mime": "image/png",
        "content_b64": base64.b64encode(control_bytes).decode("ascii"),
    })
    control_send = json_request("POST", f"/conversations/{conversation_id}/send", expected=202, body={
        "role_id": role_id, "agent_id": "xiaomi-ocr-server", "profile": "local",
        "model": "xiaomi-ocr-q8", "text": "Extract the visible text and return only what is written.",
        "attachments": [control["id"]], "idempotency_key": "server-model-ocr-control-" + uuid.uuid4().hex,
        "timeout_s": 600,
    })
    control_job = json_request("GET", f"/jobs/{control_send['job_id']}")
    check("newly selected control image overrides older conversation image",
          control_send.get("active_image_id") == control["id"]
          and control_send.get("active_image_source") == "selected"
          and [r["id"] for r in control_job["run_snapshot"]["attachment_refs"]] == [control["id"]]
          and any("более старые изображения" in w for w in control_send.get("context_warnings", [])))
    control_result = run_one_ocr_job(worker_path, control_send["job_id"], "printed-arial-control")
    control_messages = json_request("GET", f"/conversations/{conversation_id}/messages")["messages"]
    control_answer = next(m for m in control_messages if m.get("job_id") == control_send["job_id"])
    control_state = "done" if control_result.get("exit_code") == 0 else "failed"
    control_body = control_result.get("output") or control_result.get("error") or ""
    check("printed-font OCR response or failure persists after reload",
          control_answer["state"] == control_state and control_answer["body"] == control_body)
    print(json.dumps({
        "status": ("passed" if all(not x.startswith("FAIL ") for x in checks)
                   and len(recognition_results) == 2 and all(r["full_match"] for r in recognition_results)
                   else "passed_control_pixel_font_mismatch"
                   if all(not x.startswith("FAIL ") for x in checks)
                   and len(recognition_results) == 2 and recognition_results[-1]["full_match"]
                   and not recognition_results[0]["full_match"]
                   else "recognition_quality_failed"),
        "checks": checks,
        "check_count": len(checks),
        "job_id": job_id,
        "control_job_id": control_send["job_id"],
        "ocr_elapsed_s": result.get("elapsed"),
        "control_ocr_elapsed_s": control_result.get("elapsed"),
        "reported_model": (control_result.get("usage") or {}).get("reported_model"),
        "result_excerpt": result.get("output", "")[:180],
        "recognition_results": recognition_results,
        "state_dir": result.get("test_state_dir"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
