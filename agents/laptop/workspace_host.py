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
import contextlib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import stat
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


def _load_canonical_policy() -> dict:
    """Единый источник deny-политики — hub/app/security_policy.py репозитория.

    Модуль грузится по пути, без импорта пакета hub: host worker исполняется
    на устройстве отдельно от хаба, но обязан отклонять те же пути. Встроенная
    копия — только аварийный fallback: расхождение с каноном поднимает ошибку,
    а не молча продолжает работу с другой политикой.
    """
    fallback = {
        "DENY_DIRS": {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
                      "venv", "dist", "build", ".next", ".idea", ".mypy_cache",
                      ".pytest_cache", "target", "vendor", "outbox", "models",
                      ".ssh", "appdata"},
        "SECRET_SUFFIXES": (".pem", ".key", ".pfx", ".p12", ".dump", ".bak", ".kdbx"),
        "SECRET_NAMES": {"token.txt", "credentials", ".netrc", "id_rsa",
                         "id_ed25519", "auth.json"},
    }
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.abspath(os.path.join(here, "..", "..", "hub", "app",
                                             "security_policy.py"))
    if os.path.isfile(candidate):
        try:
            spec = importlib.util.spec_from_file_location("aihub_security_policy",
                                                          candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return {
                "DENY_DIRS": set(module.DENY_DIRS),
                "SECRET_SUFFIXES": tuple(module.SECRET_SUFFIXES),
                "SECRET_NAMES": set(module.SECRET_NAMES),
                "policy_digest": module.policy_digest,
                "denied_reason": module.denied_reason,
            }
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"каноническая deny-политика не загрузилась: {exc}") from None
    # Файл политики недоступен (автономная копия воркера): работаем на встроенной
    # копии. Паритет с сервером гарантируют юнит-тесты репозитория, поэтому
    # расхождение здесь — сигнал обновить копию воркера, а не молчать.
    import sys
    print("WARNING: aihub security_policy.py не найден, host worker использует "
          "встроенную копию политики; обновите копию репозитория", file=sys.stderr)
    return {"DENY_DIRS": set(fallback["DENY_DIRS"]),
            "SECRET_SUFFIXES": tuple(fallback["SECRET_SUFFIXES"]),
            "SECRET_NAMES": set(fallback["SECRET_NAMES"]),
            "policy_digest": None,
            "denied_reason": None}



_POLICY = _load_canonical_policy()
DENY_DIRS = _POLICY["DENY_DIRS"]
DENY_SUFFIX = _POLICY["SECRET_SUFFIXES"]
DENY_NAMES = _POLICY["SECRET_NAMES"]


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
    """Отказ по deny-политике. Канонический источник — security_policy хаба;
    без доступа к файлу политики — встроенная копия (паритет ловят тесты)."""
    canonical = _POLICY.get("denied_reason")
    if canonical is not None:
        return canonical(rel)
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


def _safe_regular_file(root: str, path: str) -> os.stat_result:
    """Refuse links/special files and paths that resolve outside the checkout."""
    if os.path.islink(path):
        raise OpFail("symlink_forbidden", f"symbolic link in workspace: {_rel(root, path)}")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OpFail("not_regular_file", f"not a regular file: {_rel(root, path)}")
    real_root = os.path.normcase(os.path.realpath(root))
    real_path = os.path.normcase(os.path.realpath(path))
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        raise OpFail("outside_root", f"path outside workspace: {_rel(root, path)}")
    return info


def _check_no_link_components(root: str, rel: str) -> str:
    """Re-resolve a write target and reject symlink/junction parents at effect time."""
    full = resolve(root, rel)
    current = os.path.abspath(root)
    is_junction = getattr(os.path, "isjunction", lambda _p: False)
    for part in rel.replace("\\", "/").split("/"):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.lexists(current) and (os.path.islink(current) or is_junction(current)):
            raise OpFail("symlink_forbidden", f"link in write path: {_rel(root, current)}")
    return full


def walk_files(root: str, limit: int = MAX_WALK):
    """Файлы корня без служебных каталогов. Возвращает (rel, size)."""
    count = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        for d in dirs:
            full_dir = os.path.join(base, d)
            if os.path.islink(full_dir):
                raise OpFail("symlink_forbidden", f"symbolic link directory: {_rel(root, full_dir)}")
        dirs[:] = sorted(d for d in dirs if d.lower() not in DENY_DIRS)
        for name in sorted(names):
            rel = _rel(root, os.path.join(base, name))
            if denied(rel):
                continue
            try:
                full = os.path.join(base, name)
                size = _safe_regular_file(root, full).st_size
            except OSError:
                continue
            yield rel, size
            count += 1
            if count >= limit:
                return


def tree_digest(root: str, files: list[tuple[str, int]] | None = None) -> str:
    """Strong deterministic digest: path, size and content SHA-256 for every file."""
    digest = hashlib.sha256()
    items = files if files is not None else list(walk_files(root))
    for rel, size in sorted(items):
        full = os.path.join(root, *rel.split("/"))
        before = _safe_regular_file(root, full)
        mark = sha256_file(full)
        after = _safe_regular_file(root, full)
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise OpFail("tree_changed_during_hash", f"file changed while hashing: {rel}")
        if after.st_size != size:
            raise OpFail("tree_changed_during_hash", f"file size changed while hashing: {rel}")
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
        "security_policy": (_POLICY.get("policy_digest") or {"deny_dirs": sorted(DENY_DIRS)}),
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
    source_before = tree_digest(root)
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
        _safe_regular_file(root, src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError:
            continue
        copied += 1
        total += size
        files.append((rel, size))
    digest = tree_digest(target, files)
    source_after = tree_digest(root)
    if source_after != source_before:
        shutil.rmtree(target, ignore_errors=True)
        raise OpFail("snapshot_source_changed", "workspace changed while snapshot was copied")
    meta = {
        "id": snap_id,
        "root": os.path.abspath(root),
        "path": target,
        "files": copied,
        "bytes": total,
        "tree_digest": digest,
        "source_tree_digest": source_after,
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


@contextlib.contextmanager
def _writer_lock(root: str, timeout: float = 90):
    """Interprocess lock shared by workspace workers on this host."""
    lock_dir = os.path.join(effect_state_root(), "locks")
    os.makedirs(lock_dir, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(os.path.realpath(root)).encode("utf-8")).hexdigest()[:24]
    path = os.path.join(lock_dir, key + ".lock")
    fh = open(path, "a+b")
    acquired = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OpFail("writer_busy", "another workspace writer holds this checkout")
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
        else:
            fh.close()


def _write_json_durable(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # POSIX directory fsync makes the rename durable across host power loss.
    # Windows does not expose a portable directory fsync through Python.
    if os.name != "nt":
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def effect_state_root() -> str:
    """Stable per-user durable area, independent of AIHUB_WORKSPACE_DIR overrides."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(os.path.abspath(base), "aihub", "workspace-effects")
    os.makedirs(path, exist_ok=True)
    return path


def _effect_path(root: str, job_id: str, hub_identity: str = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id or ""):
        raise OpFail("bad_job_id", "apply_patch requires a valid job id")
    root_key = hashlib.sha256(os.path.normcase(os.path.realpath(root)).encode("utf-8")).hexdigest()[:24]
    namespace = hashlib.sha256(hub_identity.encode("utf-8")).hexdigest()[:24]
    folder = os.path.join(effect_state_root(), "effects", root_key, namespace)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, job_id + ".json")


def pending_effect_payloads(hub_identity: str = "") -> list[dict]:
    """Return durable effects that need Hub acknowledgement after a worker restart."""
    base = os.path.join(effect_state_root(), "effects")
    if not os.path.isdir(base):
        return []
    payloads = []
    for root_key in sorted(os.listdir(base)):
        root_folder = os.path.join(base, root_key)
        if not os.path.isdir(root_folder):
            continue
        for namespace in sorted(os.listdir(root_folder)):
            folder = os.path.join(root_folder, namespace)
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        journal = json.load(fh)
                except (OSError, ValueError):
                    continue
                if journal.get("reported") or journal.get("state") not in ("applying", "uncertain", "applied"):
                    continue
                if journal.get("hub_identity") != hub_identity:
                    continue
                job_id, attempt_id = journal.get("job_id"), journal.get("attempt_id")
                if not job_id or not attempt_id:
                    continue
                if journal.get("state") == "applied" and isinstance(journal.get("result"), dict):
                    ok, output, error, uncertain = True, json.dumps(journal["result"], ensure_ascii=False), None, False
                else:
                    detail = {k: journal.get(k) for k in (
                        "schema", "state", "job_id", "attempt_id", "resource_generation",
                        "candidate_digest", "base_tree_digest", "preimage_digest", "write_set", "applied")}
                    output = json.dumps({"error": {"code": "effect_uncertain",
                                                     "message": "host restarted with unresolved filesystem effect"},
                                         "op": "apply_patch", "job_id": job_id,
                                         "attempt_id": attempt_id, "effect_journal": detail},
                                        ensure_ascii=False)
                    ok, error, uncertain = False, "effect_uncertain: host restarted with unresolved filesystem effect", True
                payloads.append({"job_id": job_id, "attempt_id": attempt_id, "ok": ok,
                                 "output": output, "error": error, "usage": {"workspace_op": "apply_patch"},
                                 "uncertain": uncertain})
    return payloads


def mark_effect_reported(job_id: str, attempt_id: str, hub_identity: str = "") -> bool:
    """Mark a local journal only after the Hub confirmed durable receipt."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id or "") or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", attempt_id or ""):
        return False
    base = os.path.join(effect_state_root(), "effects")
    if not os.path.isdir(base):
        return False
    for root_key in os.listdir(base):
        folder = os.path.join(base, root_key)
        try:
            namespaces = os.listdir(folder)
        except OSError:
            continue
        for namespace in namespaces:
            path = os.path.join(folder, namespace, job_id + ".json")
            try:
                with open(path, encoding="utf-8") as fh:
                    journal = json.load(fh)
            except (OSError, ValueError):
                continue
            if (journal.get("job_id") != job_id or journal.get("attempt_id") != attempt_id
                    or journal.get("hub_identity") != hub_identity):
                continue
            if journal.get("reported"):
                return True
            journal["reported"] = True
            journal["reported_at"] = time.time()
            _write_json_durable(path, journal)
            return True
    return False


def op_apply_patch(root: str, args: dict, job_id: str = "", attempt_id: str = "",
                   resource_generation: int | None = None,
                   fence_check=None, candidate_digest: str | None = None,
                   hub_identity: str = "") -> dict:
    """Fenced write with a durable intent journal; partial outcomes never replay."""
    if not job_id or not attempt_id or not callable(fence_check):
        raise OpFail("fence_unavailable", "apply_patch requires job, attempt and active lease verifier")
    if not os.path.isdir(root):
        raise OpFail("path_unavailable", f"blocked: path unavailable {root}")
    journal_path = _effect_path(root, job_id, hub_identity)
    args_digest = hashlib.sha256(json.dumps(
        args, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    candidate_digest = candidate_digest or args_digest
    with _writer_lock(root):
        root_effect_dir = os.path.dirname(os.path.dirname(journal_path))
        root_canonical = os.path.normcase(os.path.realpath(root))
        for namespace in os.listdir(root_effect_dir):
            namespace_dir = os.path.join(root_effect_dir, namespace)
            if not os.path.isdir(namespace_dir):
                continue
            for name in os.listdir(namespace_dir):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(namespace_dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        existing = json.load(fh)
                except (OSError, ValueError):
                    raise OpFail("checkout_requires_reconciliation", "unreadable effect journal blocks writes") from None
                if (os.path.normcase(os.path.realpath(existing.get("root") or "")) == root_canonical
                        and existing.get("state") in ("applying", "uncertain")):
                    raise OpFail("checkout_requires_reconciliation",
                                 "unresolved prior filesystem effect blocks all writes to this checkout")
        if os.path.exists(journal_path):
            with open(journal_path, encoding="utf-8") as fh:
                prior = json.load(fh)
            if (prior.get("job_id") == job_id and prior.get("attempt_id") == attempt_id
                    and prior.get("candidate_digest") == candidate_digest
                    and prior.get("state") == "applied" and isinstance(prior.get("result"), dict)):
                prior["result"]["replayed"] = True
                return prior["result"]
            raise OpFail("effect_uncertain", "durable operation journal exists; manual reconciliation required")

        base = args.get("base_tree_digest")
        current = tree_digest(root)
        if base and current != base:
            raise OpFail("tree_changed", "workspace tree changed since inspect")
        changes = args.get("changes") or []
        if not changes or len(changes) > MAX_PATCH_FILES:
            raise OpFail("bad_arg", f"changes: 1..{MAX_PATCH_FILES}")
        backup_dir = os.path.join(state_root(), "backups",
                                  f"{time.strftime('%Y%m%d-%H%M%S')}-{job_id[:8]}")
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
                if sha256_file(full) != preimage:
                    raise OpFail("preimage_mismatch", f"{rel}: файл изменён относительно preimage")
            elif exists:
                raise OpFail("preimage_missing", f"{rel}: файл уже существует, нужен preimage_sha256")
            if content_b64 is not None:
                try:
                    raw = base64.b64decode(content_b64, validate=True)
                except (ValueError, TypeError):
                    raise OpFail("bad_arg", f"{rel}: invalid base64 content") from None
            else:
                raw = None
            plan.append({"path": rel, "full": full, "exists": exists,
                         "preimage_sha256": preimage,
                         "action": "delete" if raw is None else ("update" if exists else "create"),
                         "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                         "content": raw})
        if not fence_check():
            raise OpFail("lease_lost", "workspace lease is no longer current")
        journal = {
            "schema": 1, "state": "applying", "job_id": job_id,
            "root": root_canonical,
            "attempt_id": attempt_id, "resource_generation": resource_generation,
            "hub_identity": hub_identity,
            "candidate_digest": candidate_digest, "base_tree_digest": base,
            "preimage_digest": hashlib.sha256(json.dumps(
                [{"path": x["path"], "sha256": x["preimage_sha256"]} for x in plan],
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "write_set": [{k: x[k] for k in ("path", "action", "preimage_sha256", "sha256")} for x in plan],
            "applied": [], "started_at": time.time(),
        }
        _write_json_durable(journal_path, journal)
        applied = []
        try:
            for item in plan:
                if not fence_check():
                    raise OpFail("lease_lost", "workspace lease is no longer current")
                full = _check_no_link_components(root, item["path"])
                if os.path.normcase(full) != os.path.normcase(item["full"]):
                    raise OpFail("path_changed", "write path changed after validation")
                if item["exists"]:
                    _safe_regular_file(root, full)
                    if sha256_file(full) != item["preimage_sha256"]:
                        raise OpFail("preimage_mismatch", f"{item['path']}: changed immediately before write")
                elif os.path.lexists(full):
                    raise OpFail("preimage_mismatch", f"{item['path']}: appeared immediately before write")
                if item["exists"]:
                    dst = os.path.join(backup_dir, *item["path"].split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(full, dst)
                if item["action"] == "delete":
                    os.remove(full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    tmp = full + f".aihub-{uuid.uuid4().hex[:8]}.tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(item["content"])
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, full)
                done = {k: item[k] for k in ("path", "action", "preimage_sha256", "sha256")}
                applied.append(done)
                journal["applied"] = list(applied)
                _write_json_durable(journal_path, journal)
            result = {
                "root": os.path.abspath(root), "operation": "apply_patch", "applied": applied,
                "base_tree_digest": base, "tree_digest": tree_digest(root),
                "backup_dir": backup_dir if os.path.isdir(backup_dir) else None,
                "job_id": job_id, "attempt_id": attempt_id,
                "candidate_digest": candidate_digest,
                "preimage_digest": journal["preimage_digest"],
                "resource_generation": resource_generation,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            journal.update({"state": "applied", "result": result, "finished_at": time.time()})
            _write_json_durable(journal_path, journal)
            return result
        except Exception as exc:
            journal.update({"state": "uncertain", "error_type": type(exc).__name__,
                            "applied": list(applied), "finished_at": time.time()})
            _write_json_durable(journal_path, journal)
            raise OpFail("effect_uncertain", "patch effect is partial or uncertain; manual reconciliation required") from None


def op_run_checks(root: str, args: dict) -> dict:
    """Run checks only through an explicitly configured isolated executor."""
    snap_id = args.get("snapshot_id") or ""
    workdir = _snapshot_dir(snap_id)
    plan = (args.get("plan") or {}).get("checks") or []
    if not plan:
        raise OpFail("no_check_plan", "план проверок пуст")
    runner_argv = isolated_runner_argv()
    if not runner_argv:
        raise OpFail("runner_unavailable", "isolated runner is not configured")
    results = []
    ok = True
    verified_isolation: dict | None = None
    for item in plan:
        name = item.get("name")
        cmd = item.get("cmd") or []
        timeout = int(item.get("timeout_s") or 120)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise OpFail("bad_check_plan", f"{name}: cmd должен быть списком строк")
        before = tree_digest(workdir)
        request_id = uuid.uuid4().hex
        request = {
            "protocol": 1,
            "request_id": request_id,
            "snapshot": {"path": os.path.realpath(workdir), "read_only": True,
                         "tree_digest": before},
            "command": cmd,
            "timeout_s": timeout,
            "limits": {"network": "none", "rootfs_read_only": True,
                       "cap_drop": ["ALL"], "no_new_privileges": True,
                       "memory_mb": 512, "cpus": 1, "pids": 64,
                       "tmpfs_mb": 64, "stdout_bytes": 65536,
                       "stderr_bytes": 65536},
        }
        started = time.time()
        try:
            proc = subprocess.run(
                runner_argv, cwd=state_root(), capture_output=True,
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                env=isolated_runner_env(), timeout=timeout + 60, shell=False)
            if proc.returncode != 0:
                raise OpFail("runner_failed", f"isolated runner exited {proc.returncode}")
            response = json.loads((proc.stdout or b"").decode("utf-8"))
            required_isolation = {
                "network": "none", "rootfs_read_only": True,
                "cap_drop_all": True, "no_new_privileges": True,
                "resource_limits": True, "snapshot_read_only": True,
                "nonroot": True, "ephemeral_workspace": True,
            }
            isolation = response.get("isolation") or {}
            if (response.get("protocol") != 1 or response.get("request_id") != request_id
                    or any(isolation.get(k) != v for k, v in required_isolation.items())
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(isolation.get("image") or ""))
                    or response.get("termination_confirmed") is not True
                    or response.get("snapshot_tree_digest_before") != before
                    or response.get("snapshot_tree_digest_after") != before):
                raise OpFail("runner_unverified", "isolated runner response failed verification")
            code = int(response["exit_code"])
            out = str(response.get("stdout") or "")[:65536]
            err = str(response.get("stderr") or "")[:65536]
            after = tree_digest(workdir)
            if after != before:
                raise OpFail("snapshot_changed", "isolated runner modified read-only snapshot")
            verified_isolation = dict(isolation)
        except subprocess.TimeoutExpired:
            code, out, err = -1, "", f"isolated runner timeout after {timeout}s"
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise OpFail("runner_unverified", f"isolated runner unavailable/invalid: {type(exc).__name__}") from None
        passed = code == 0
        ok = ok and passed
        results.append({"name": name, "cmd": cmd, "exit_code": code, "passed": passed,
                        "elapsed_s": round(time.time() - started, 2),
                        "stdout_tail": out[-4000:], "stderr_tail": err[-2000:]})
    receipt = {"snapshot_id": snap_id, "workdir": workdir, "ok": ok, "checks": results,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    # Изоляция, подтверждённая сверкой ответа runner'а: хаб принимает receipt
    # только с этим блоком (flow_gates.validate_check_receipt).
    if verified_isolation is not None:
        receipt["isolation"] = verified_isolation
    return receipt


def isolated_runner_argv() -> list[str] | None:
    """Admin-only JSON argv for the restricted runner; never uses a shell."""
    raw = os.environ.get("AIHUB_ISOLATED_RUNNER_ARGV", "")
    if not raw:
        return None
    try:
        argv = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(x, str) and x for x in argv)
            or not os.path.isabs(argv[0]) or not os.path.isfile(argv[0])):
        return None
    return argv


def isolated_runner_env() -> dict:
    """Minimal environment for the trusted runner executable."""
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


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
    ops = list(OPS)
    if not isolated_runner_argv() and "run_checks" in ops:
        ops.remove("run_checks")
    return {"workspace_ops": ops, "workspace_root": state_root(),
            "workspace_protocol": 3, "workspace_fencing": 1,
            "workspace_effect_journal": 1,
            "isolated_runner_protocol": 1 if isolated_runner_argv() else None,
            "host": platform.node(), "kinds": ["agent", "workspace"]}


def run_job(job: dict, fence_check=None, hub_identity: str = "") -> dict:
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
        if op == "apply_patch" and (not callable(fence_check) or not fence_check()):
            raise OpFail("lease_lost", "workspace lease is not current")
        if op == "apply_patch":
            result = handler(root, args, job_id, attempt_id,
                             (job.get("resource") or {}).get("generation"), fence_check,
                             envelope.get("digest"), hub_identity)
        else:
            result = handler(root, args, job_id) if op in TAKES_JOB else handler(root, args)
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": True,
                   "output": json.dumps(result, ensure_ascii=False),
                   "usage": {"workspace_op": op}}
    except OpFail as exc:
        detail = {"error": exc.as_dict(), "op": op, "root": root,
                  "job_id": job_id, "attempt_id": attempt_id}
        if exc.code == "effect_uncertain":
            try:
                with open(_effect_path(root, job_id, hub_identity), encoding="utf-8") as fh:
                    journal = json.load(fh)
                detail["effect_journal"] = {k: journal.get(k) for k in (
                    "schema", "state", "job_id", "attempt_id", "resource_generation",
                    "candidate_digest", "base_tree_digest", "preimage_digest", "write_set", "applied")}
            except (OSError, ValueError, OpFail):
                detail["effect_journal"] = {"state": "unavailable"}
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                   "error": f"{exc.code}: {exc}",
                   "output": json.dumps(detail, ensure_ascii=False),
                   "usage": {"workspace_op": op},
                   "uncertain": exc.code == "effect_uncertain"}
    except Exception as exc:  # noqa: BLE001
        payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "usage": {"workspace_op": op}}
    payload["elapsed_total"] = round(time.time() - started, 2)
    return payload
