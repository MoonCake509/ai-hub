"""CORE-02b — host worker: управляемые операции рабочего пространства.

Воркер исполняет фиксированный набор операций на своём устройстве: сервер не
предполагает, что видит диски ноутбука. Команды оболочки из задания не
принимаются: run_checks берёт план из доверенных настроек проекта (хаб кладёт
его в задание из settings, не из текста модели), остальные операции работают
только с путями внутри корня проекта.

Каждый путь разрешается в канонический вид и проверяется на выход за корень, в
том числе через симлинки и junction. Секреты (.env, ключи, dumps) и служебные
каталоги (.git, node_modules, outbox) не читаются и не пакуются.

Изменяющие операции пишут durable receipt и копию перезаписанных файлов, поэтому
повтор задания с тем же job_id не применяет патч второй раз.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

OPS = ["inspect", "snapshot", "list_files", "read_files", "apply_patch",
       "run_checks", "package"]

MAX_LIST = 200
MAX_WALK = 20000
MAX_READ_FILES = 8
MAX_READ_BYTES = 64 * 1024
MAX_PATCH_FILES = 50
MAX_PATCH_BYTES = 1024 * 1024
MAX_SNAPSHOT_FILES = 20000
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
MAX_FILE_HASH = 512 * 1024

DENY_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             "outbox", "models", ".ssh", "appdata"}
DENY_SUFFIX = (".pem", ".key", ".pfx", ".p12", ".dump", ".bak", ".kdbx")
DENY_NAMES = {"token.txt", "credentials", ".netrc", "id_rsa", "id_ed25519"}


class OpFail(Exception):
    """Отказ операции: код машинный, текст — человеку."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


def state_root() -> str:
    base = os.environ.get("AIHUB_WORKSPACE_DIR")
    if not base:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                            "aihub", "workspaces")
    path = os.path.abspath(base)
    os.makedirs(path, exist_ok=True)
    return path


def _index_path() -> str:
    return os.path.join(state_root(), "index.json")


def load_index() -> dict:
    try:
        with open(_index_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"snapshots": {}, "bundles": {}}
    except (OSError, ValueError):
        return {"snapshots": {}, "bundles": {}}


def save_index(data: dict) -> None:
    path = _index_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def denied(rel: str) -> str | None:
    parts = [p.lower() for p in re.split(r"[\\/]+", rel or "") if p]
    if not parts:
        return "пустой путь"
    for part in parts[:-1]:
        if part in DENY_DIRS:
            return f"служебный каталог {part}"
    name = parts[-1]
    if name == ".env" or name.startswith(".env."):
        return "файл окружения"
    if name in DENY_NAMES or name.startswith("id_rsa"):
        return "секрет"
    if name.endswith(DENY_SUFFIX):
        return "секрет/дамп"
    return None


def resolve(root: str, rel: str) -> str:
    """Полный путь внутри корня; симлинк/junction наружу отвергается."""
    text = (rel or "").strip().replace("\\", "/")
    if not text or text in (".", "/"):
        raise OpFail("bad_path", "относительный путь не задан")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise OpFail("absolute_path", "ожидался путь относительно корня")
    parts = [p for p in text.split("/") if p]
    if any(p == ".." for p in parts):
        raise OpFail("traversal", "`..` в относительном пути запрещён")
    full = os.path.abspath(os.path.join(root, *parts))
    real_root = os.path.realpath(root)
    real_full = os.path.realpath(full)
    if os.path.normcase(real_full) != os.path.normcase(real_root) and not os.path.normcase(
            real_full).startswith(os.path.normcase(real_root) + os.sep):
        raise OpFail("outside_root", "путь выходит за корень (симлинк/junction)")
    return full


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_files(root: str, limit: int = MAX_WALK):
    """Файлы корня без служебных каталогов. Возвращает (rel, size)."""
    count = 0
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in DENY_DIRS)
        for name in sorted(names):
            rel = _rel(root, os.path.join(base, name))
            if denied(rel):
                continue
            try:
                size = os.path.getsize(os.path.join(base, name))
            except OSError:
                continue
            yield rel, size
            count += 1
            if count >= limit:
                return


def tree_digest(root: str, files: list[tuple[str, int]] | None = None) -> str:
    """Digest дерева по содержимому: путь + размер + хеш или mtime для крупных.

    Размер и путь сами по себе не различают две свежие копии шаблона, поэтому
    база для apply_patch обязана включать содержимое.
    """
    digest = hashlib.sha256()
    items = files if files is not None else list(walk_files(root))
    for rel, size in sorted(items):
        full = os.path.join(root, *rel.split("/"))
        if size <= MAX_FILE_HASH:
            try:
                mark = sha256_file(full)
            except OSError:
                mark = "unreadable"
        else:
            try:
                mark = "mtime:" + str(int(os.path.getmtime(full)))
            except OSError:
                mark = "missing"
        digest.update((rel + "|" + str(size) + "|" + mark + '\n').encode("utf-8"))
    return digest.hexdigest()


def _git(root: str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=30)
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _tool_version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=15)
        text = (proc.stdout or proc.stderr or b"").decode("utf-8", "replace").strip()
        return text.splitlines()[0][:120] if text else None
    except (OSError, subprocess.SubprocessError):
        return None


def op_inspect(root: str, args: dict) -> dict:
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    files = sorted(walk_files(root))
    code, head = _git(root, "rev-parse", "--verify", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        head = ""
    _, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _, status = _git(root, "status", "--porcelain")
    dirty = [line for line in status.splitlines() if line.strip()]
    return {
        "root": os.path.abspath(root),
        "exists": True,
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "os_user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "is_git": bool(head),
        "head": head or None,
        "branch": branch or None,
        "dirty_entries": len(dirty),
        "dirty_digest": hashlib.sha256("\n".join(dirty).encode("utf-8")).hexdigest()[:16],
        "tree_digest": tree_digest(root, files),
        "files": len(files),
        "bytes": sum(size for _, size in files),
        "tools": {
            "git": _tool_version(["git", "--version"]),
            "python": _tool_version([sys.executable, "--version"]),
            "node": _tool_version(["node", "--version"]),
        },
        "policy": {"deny_dirs": sorted(DENY_DIRS), "deny_suffix": list(DENY_SUFFIX)},
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def op_list_files(root: str, args: dict) -> dict:
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    pattern = args.get("glob") or "**/*"
    limit = min(int(args.get("limit") or 100), MAX_LIST)
    offset = int(args.get("offset") or 0)
    matched = []
    total = 0
    for rel, size in walk_files(root):
        flat = pattern.replace("**/", "")
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, flat) or fnmatch.fnmatch(
                rel.rsplit("/", 1)[-1], flat):
            total += 1
            if offset <= total - 1 < offset + limit:
                item = {"path": rel, "size": size}
                try:
                    item["mtime"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%S",
                        time.localtime(os.path.getmtime(os.path.join(root, rel))))
                except OSError:
                    pass
                if size <= MAX_FILE_HASH:
                    try:
                        item["sha256"] = sha256_file(os.path.join(root, rel))
                    except OSError:
                        item["sha256"] = None
                matched.append(item)
    return {"root": os.path.abspath(root), "glob": pattern, "total": total,
            "offset": offset, "items": matched,
            "next_offset": offset + len(matched) if offset + len(matched) < total else None}


def op_read_files(root: str, args: dict) -> dict:
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    files = args.get("files") or []
    if not files or len(files) > MAX_READ_FILES:
        raise OpFail("bad_arg", f"files: 1..{MAX_READ_FILES}")
    out = []
    for item in files:
        rel = item.get("path") or ""
        reason = denied(rel)
        if reason:
            raise OpFail("denied_path", f"{rel}: {reason}")
        full = resolve(root, rel)
        if not os.path.isfile(full):
            raise OpFail("not_found", f"{rel}: файла нет")
        size = os.path.getsize(full)
        start = max(0, int(item.get("offset") or 0))
        length = min(int(item.get("length") or MAX_READ_BYTES), MAX_READ_BYTES)
        with open(full, "rb") as fh:
            fh.seek(start)
            raw = fh.read(length)
        entry = {"path": rel, "size": size, "offset": start, "length": len(raw),
                 "sha256": sha256_file(full) if size <= MAX_FILE_HASH else None}
        try:
            entry["text"] = raw.decode("utf-8")
            entry["encoding"] = "utf-8"
        except UnicodeDecodeError:
            entry["content_b64"] = base64.b64encode(raw).decode("ascii")
            entry["encoding"] = "base64"
        out.append(entry)
    return {"root": os.path.abspath(root), "files": out}


def op_snapshot(root: str, args: dict, job_id: str = "") -> dict:
    """Управляемая копия для проверок: не трогает рабочее дерево и не делает stash."""
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.get("label") or "snapshot")[:32]
    snap_id = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}-{(job_id or uuid.uuid4().hex)[:6]}"
    target = os.path.join(state_root(), "snapshots", snap_id)
    if os.path.exists(target):
        raise OpFail("snapshot_exists", f"{snap_id}: уже существует")
    os.makedirs(target, exist_ok=False)
    copied = 0
    total = 0
    files = []
    for rel, size in walk_files(root):
        if copied >= MAX_SNAPSHOT_FILES or total + size > MAX_SNAPSHOT_BYTES:
            raise OpFail("snapshot_too_large",
                         f"снимок превышает {MAX_SNAPSHOT_FILES} файлов / "
                         f"{MAX_SNAPSHOT_BYTES} байт")
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(target, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError:
            continue
        copied += 1
        total += size
        files.append((rel, size))
    digest = tree_digest(target, files)
    meta = {
        "id": snap_id,
        "root": os.path.abspath(root),
        "path": target,
        "files": copied,
        "bytes": total,
        "tree_digest": digest,
        "source_tree_digest": tree_digest(root),
        "job_id": job_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": platform.node(),
    }
    with open(os.path.join(target, ".aihub-snapshot.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    index = load_index()
    index.setdefault("snapshots", {})[snap_id] = meta
    save_index(index)
    return {"snapshot_id": snap_id, "path": target, "files": copied, "bytes": total,
            "tree_digest": digest, "source_tree_digest": meta["source_tree_digest"],
            "created_at": meta["created_at"]}


def _snapshot_dir(snapshot_id: str) -> str:
    index = load_index()
    meta = (index.get("snapshots") or {}).get(snapshot_id)
    if not meta:
        raise OpFail("unknown_snapshot", f"{snapshot_id}: нет такого снимка на этом устройстве")
    path = meta.get("path") or ""
    if not os.path.isdir(path):
        raise OpFail("snapshot_missing", f"{snapshot_id}: каталог снимка не найден")
    return path


def _receipt_path(job_id: str) -> str:
    folder = os.path.join(state_root(), "receipts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{job_id or 'nokey'}.json")


def op_apply_patch(root: str, args: dict, job_id: str = "") -> dict:
    """Применяет write_set после проверки preimages; повтор job_id не применяется дважды."""
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    receipt_file = _receipt_path(job_id)
    if job_id and os.path.isfile(receipt_file):
        with open(receipt_file, encoding="utf-8") as fh:
            previous = json.load(fh)
        if previous.get("base_tree_digest") == args.get("base_tree_digest"):
            previous["replayed"] = True
            return previous
    base = args.get("base_tree_digest")
    current = tree_digest(root)
    if base and current != base:
        raise OpFail("tree_changed",
                     "рабочее дерево изменилось после inspect: нужен новый base_tree_digest")
    changes = args.get("changes") or []
    if not changes or len(changes) > MAX_PATCH_FILES:
        raise OpFail("bad_arg", f"changes: 1..{MAX_PATCH_FILES}")
    backup_dir = os.path.join(state_root(), "backups",
                              f"{time.strftime('%Y%m%d-%H%M%S')}-{(job_id or 'nokey')[:8]}")
    plan = []
    for change in changes:
        rel = change.get("path") or ""
        reason = denied(rel)
        if reason:
            raise OpFail("denied_path", f"{rel}: {reason}")
        full = resolve(root, rel)
        preimage = (change.get("preimage_sha256") or "").lower() or None
        content_b64 = change.get("content_b64")
        if content_b64 is None and not preimage:
            raise OpFail("bad_arg", f"{rel}: нужен content_b64 или preimage_sha256")
        exists = os.path.isfile(full)
        if preimage:
            if not exists:
                raise OpFail("preimage_missing", f"{rel}: файл не найден, патч не применён")
            actual = sha256_file(full)
            if actual != preimage:
                raise OpFail("preimage_mismatch",
                             f"{rel}: файл изменён (ожидался {preimage[:12]}…, получен {actual[:12]}…)")
        elif exists:
            raise OpFail("preimage_missing",
                         f"{rel}: файл уже существует, нужен preimage_sha256")
        plan.append((rel, full, content_b64, exists))
    applied = []
    for rel, full, content_b64, exists in plan:
        if exists:
            dst = os.path.join(backup_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
        if content_b64 is None:
            os.remove(full)
            applied.append({"path": rel, "action": "delete"})
            continue
        raw = base64.b64decode(content_b64, validate=True)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + f".aihub-{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, full)
        applied.append({"path": rel, "action": "update" if exists else "create",
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    result = {
        "root": os.path.abspath(root),
        "applied": applied,
        "base_tree_digest": base,
        "tree_digest": tree_digest(root),
        "backup_dir": backup_dir if os.path.isdir(backup_dir) else None,
        "job_id": job_id,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if job_id:
        tmp = receipt_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, receipt_file)
    return result


def op_run_checks(root: str, args: dict) -> dict:
    """Утверждённый план проверок в тестовой среде (снимок), не в рабочем дереве."""
    snap_id = args.get("snapshot_id") or ""
    workdir = _snapshot_dir(snap_id)
    plan = (args.get("plan") or {}).get("checks") or []
    if not plan:
        raise OpFail("no_check_plan", "план проверок пуст")
    results = []
    ok = True
    for item in plan:
        name = item.get("name")
        cmd = item.get("cmd") or []
        timeout = int(item.get("timeout_s") or 120)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise OpFail("bad_check_plan", f"{name}: cmd должен быть списком строк")
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
            code = proc.returncode
            out = (proc.stdout or b"").decode("utf-8", "replace")
            err = (proc.stderr or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            code, out, err = -1, "", f"timeout after {timeout}s"
        except OSError as exc:
            code, out, err = -2, "", f"не запустилось: {exc}"
        passed = code == 0
        ok = ok and passed
        results.append({"name": name, "cmd": cmd, "exit_code": code, "passed": passed,
                        "elapsed_s": round(time.time() - started, 2),
                        "stdout_tail": out[-4000:], "stderr_tail": err[-2000:]})
    return {"snapshot_id": snap_id, "workdir": workdir, "ok": ok, "checks": results,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def op_package(root: str, args: dict, job_id: str = "") -> dict:
    """Review bundle: zip снимка + manifest. Исходники пользователя не меняются."""
    snap_id = args.get("snapshot_id") or ""
    snap_dir = _snapshot_dir(snap_id)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.get("label") or "bundle")[:32]
    include = args.get("include") or []
    bundle_dir = os.path.join(state_root(), "bundles")
    os.makedirs(bundle_dir, exist_ok=True)
    bundle_name = f"{label}-{snap_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    bundle_path = os.path.join(bundle_dir, bundle_name)
    manifest = []
    for rel, size in walk_files(snap_dir):
        if rel == ".aihub-snapshot.json":
            continue
        if include and not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(
                rel.rsplit("/", 1)[-1], pat) for pat in include):
            continue
        full = os.path.join(snap_dir, *rel.split("/"))
        try:
            digest = sha256_file(full)
        except OSError:
            continue
        manifest.append({"path": rel, "size": size, "sha256": digest})
    meta = {"label": label, "snapshot_id": snap_id, "root": os.path.abspath(root),
            "files": manifest, "job_id": job_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": platform.node()}
    manifest_body = json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True)
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_body)
        for item in manifest:
            full = os.path.join(snap_dir, *item["path"].split("/"))
            zf.write(full, arcname=item["path"])
    manifest_digest = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()
    index = load_index()
    entry = {"id": bundle_name, "path": bundle_path, "snapshot_id": snap_id,
             "files": len(manifest), "bytes": os.path.getsize(bundle_path),
             "manifest_digest": manifest_digest, "job_id": job_id,
             "created_at": meta["created_at"]}
    index.setdefault("bundles", {})[bundle_name] = entry
    save_index(index)
    return {"bundle": entry, "manifest": manifest[:MAX_LIST],
            "manifest_truncated": len(manifest) > MAX_LIST}


HANDLERS = {
    "inspect": op_inspect,
    "snapshot": op_snapshot,
    "list_files": op_list_files,
    "read_files": op_read_files,
    "apply_patch": op_apply_patch,
    "run_checks": op_run_checks,
    "package": op_package,
}

TAKES_JOB = {"snapshot", "apply_patch", "package"}


def capabilities() -> dict:
    """Что этот хост реально умеет: хаб пускает control-задание только по этому списку."""
    return {"workspace_ops": list(OPS), "workspace_root": state_root(),
            "host": platform.node(), "kinds": ["agent", "workspace"]}


def run_job(job: dict) -> dict:
    """Исполняет control-задание и возвращает payload для /bridge/result."""
    job_id = job.get("id") or ""
    attempt_id = job.get("attempt_id") or ""
    started = time.time()
    try:
        envelope = json.loads(job.get("prompt") or "{}")
    except ValueError:
        return {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                "error": "задание не разобрано как JSON-конверт операции"}
    op = envelope.get("op") or ""
    args = envelope.get("args") or {}
    root = args.get("root") or ""
    handler = HANDLERS.get(op)
    if not handler:
        return {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                "error": f"операция {op!r} не поддерживается этим хостом"}
    try:
        result = handler(root, args, job_id) if op in TAKES_JOB else handler(root, args)
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": True,
                   "output": json.dumps(result, ensure_ascii=False),
                   "usage": {"workspace_op": op}}
    except OpFail as exc:
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                   "error": f"{exc.code}: {exc}", "output": json.dumps(
                       {"error": exc.as_dict(), "op": op, "root": root},
                       ensure_ascii=False),
                   "usage": {"workspace_op": op}}
    except Exception as exc:  # noqa: BLE001
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "usage": {"workspace_op": op}}
    payload["elapsed_total"] = round(time.time() - started, 2)
    return payload
