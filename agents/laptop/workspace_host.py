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
import errno
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

OPS = ["init_project", "write_reports", "create_backup", "refresh_index", "task_start", "verify_task", "task_merge", "setup_env",
       "archive_artifacts", "publish_branch",
       "search_git", "read_at", "describe_files", "inspect", "snapshot", "list_files", "read_files",
       "apply_patch", "run_checks", "package", "semantic_index", "semantic_embed",
       "verify_evidence"]

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
        "SECRET_PATTERNS": (
            ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12",
            "id_rsa*", "id_ed25519*", "*credentials*", "*secret*",
            ".npmrc", ".pypirc", ".netrc", ".git-credentials",
            "*.kdbx", "*.ovpn", "auth.json", "cookies*.txt",
        ),
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
                "policy_digest": module.policy_digest(),
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
            "SECRET_PATTERNS": tuple(fallback["SECRET_PATTERNS"]),
            "policy_digest": {
                "deny_dirs": sorted(fallback["DENY_DIRS"]),
                "secret_names": sorted(fallback["SECRET_NAMES"]),
                "secret_suffixes": sorted(fallback["SECRET_SUFFIXES"]),
                "secret_patterns": sorted(fallback["SECRET_PATTERNS"]),
            },
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
    low_rel = (rel or "").lower()
    for pattern in _POLICY.get("SECRET_PATTERNS", ()):
        if fnmatch.fnmatch(name, pattern.lower()):
            return "секрет (политика путей)"
        if "*" in pattern and fnmatch.fnmatch(low_rel, pattern.lower()):
            return "секрет (политика путей)"
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


INIT_MAX_FILES = 40
INIT_MAX_BYTES = 512 * 1024


def _init_rel(rel: str) -> str:
    text = str(rel or "").replace("\\", "/").strip()
    parts = [part for part in text.split("/") if part]
    if not text or text.startswith("/") or ".." in parts or ":" in parts[0]:
        raise OpFail("bad_path", f"invalid template path: {rel!r}")
    return "/".join(parts)


def op_init_project(root: str, args: dict) -> dict:
    """Рабочая папка проекта: шаблон, git init и первый коммит.

    Повтор на готовой папке ничего не перезаписывает: файлы пользователя
    остаются как есть, второй коммит не создаётся.
    """
    template = args.get("template_files") or {}
    if not isinstance(template, dict) or len(template) > INIT_MAX_FILES:
        raise OpFail("bad_request", "invalid template file set")
    branch = str(args.get("branch") or "main").strip()[:100] or "main"
    author_name = str(args.get("author_name") or "AI Hub").strip()[:100]
    author_email = str(args.get("author_email") or "aihub@local").strip()[:200]
    template_version = str(args.get("template_version") or "1")[:40]
    repo_subdir = str(args.get("repo_subdir") or "repo").strip()
    if repo_subdir in {".", ""}:
        repo_subdir = ""
    elif "/" in repo_subdir or "\\" in repo_subdir or ".." in repo_subdir or ":" in repo_subdir:
        raise OpFail("bad_request", "repo_subdir must be a single directory name")

    created_dir = not os.path.isdir(root)
    if created_dir:
        os.makedirs(root, exist_ok=True)

    written, skipped, total = [], [], 0
    for rel, content in template.items():
        if not isinstance(content, str):
            raise OpFail("bad_request", "template content must be a string")
        total += len(content.encode("utf-8"))
        if total > INIT_MAX_BYTES:
            raise OpFail("bad_request", "template is too large")
        rel_path = _init_rel(rel)
        target = os.path.join(root, *rel_path.split("/"))
        if os.path.exists(target):
            skipped.append(rel_path)
            continue
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        written.append(rel_path)

    # Канонический Git-репозиторий живёт в подкаталоге: рядом с ним лежат ops/.
    git_root = os.path.join(root, repo_subdir) if repo_subdir else root
    os.makedirs(git_root, exist_ok=True)
    if not os.path.isdir(os.path.join(git_root, ".git")):
        code, out = _git(git_root, "init", "-q")
        if code != 0:
            raise OpFail("git_failed", f"git init failed: {out}")
        _git(git_root, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

    code, _ = _git(git_root, "rev-parse", "--verify", "HEAD")
    committed = code == 0
    if not committed:
        _git(git_root, "add", "-A")
        code, out = _git(git_root, "-c", f"user.name={author_name}",
                         "-c", f"user.email={author_email}",
                         "commit", "-q", "-m", "chore: initial commit", "--allow-empty")
        if code != 0:
            raise OpFail("git_failed", f"git commit failed: {out}")

    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD after init")
    code, branch_now = _git(git_root, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "root": root,
        "git_root": git_root,
        "branch": branch_now if code == 0 and branch_now else branch,
        "head_sha": head,
        "template_version": template_version,
        "created_dir": created_dir,
        "written": written,
        "skipped": skipped,
        "committed": not committed,
    }


REPORTS_MAX_FILES = 40
REPORTS_MAX_BYTES = 512 * 1024


def op_write_reports(root: str, args: dict) -> dict:
    """Записать живые отчёты проекта (ops/): журнал и черновики PR.

    Пишутся только пути внутри ops/; одинаковое содержимое не перезаписывается,
    поэтому повтор события не меняет файл и не плодит версии.
    """
    reports = args.get("reports")
    if not isinstance(reports, list) or not reports:
        raise OpFail("bad_request", "reports must be a non-empty list")
    if len(reports) > REPORTS_MAX_FILES:
        raise OpFail("bad_request", "too many reports")
    written, skipped, total = [], [], 0
    for item in reports:
        if not isinstance(item, dict):
            raise OpFail("bad_request", "report item must be an object")
        rel = _init_rel(str(item.get("path") or ""))
        if not rel.startswith("ops/"):
            raise OpFail("denied_path", f"reports are allowed only under ops/: {rel}")
        content = item.get("content")
        if not isinstance(content, str):
            raise OpFail("bad_request", "report content must be a string")
        total += len(content.encode("utf-8"))
        if total > REPORTS_MAX_BYTES:
            raise OpFail("bad_request", "reports are too large")
        target = os.path.join(root, *rel.split("/"))
        if os.path.isfile(target):
            try:
                with open(target, encoding="utf-8") as fh:
                    if fh.read() == content:
                        skipped.append(rel)
                        continue
            except OSError:
                pass
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, target)
        written.append(rel)
    return {"root": root, "written": written, "skipped": skipped}


def _backup_table_row(cells: list[str]) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def op_create_backup(root: str, args: dict) -> dict:
    """Точка восстановления: zip зафиксированного коммита + строка в ops/BACKUPS.md.

    Источник — `git archive HEAD`, поэтому точка соответствует конкретному SHA,
    а не текущему содержимому каталога. Повтор с тем же ключом возвращает уже
    созданный файл и не добавляет вторую строку в журнал.
    """
    import hashlib
    import zipfile
    from datetime import datetime, timezone

    git_root = os.path.join(root, "repo")
    if not os.path.isdir(os.path.join(git_root, ".git")):
        git_root = root
    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD for backup")
    head = head.strip()
    label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(args.get("label") or "backup"))[:48]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Имя без времени: одна точка на коммит, повтор не плодит копии.
    rel = f"ops/backups/{label}-{head[:8]}.zip"
    target = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.isfile(target):
        archive = subprocess.run(["git", "-C", git_root, "archive", "--format=zip",
                                  f"--output={target}", "HEAD"],
                                 capture_output=True, text=True, timeout=120)
        if archive.returncode != 0:
            raise OpFail("git_failed", f"git archive failed: {archive.stderr.strip()[:300]}")
    digest = hashlib.sha256(open(target, "rb").read()).hexdigest()
    size = os.path.getsize(target)
    restore = f"git -C \"{git_root}\" checkout {head[:12]}"
    journal = os.path.join(root, "ops", "BACKUPS.md")
    row = _backup_table_row([label, stamp, head[:12], str(args.get("host") or "laptop"),
                             f"`{rel}`", f"`{digest[:16]}`", f"`{restore}`"])
    added = False
    if os.path.isfile(journal):
        with open(journal, encoding="utf-8") as fh:
            text = fh.read()
        if rel not in text:
            if not text.endswith("\n"):
                text += "\n"
            text += row + "\n"
            with open(journal, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            added = True
    return {"root": root, "path": rel, "sha256": digest, "size": size, "head_sha": head,
            "journal_row_added": added, "restore": restore}


INDEX_MAX_FILES = 2000


SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
                   "id_ed25519*", "credentials*", "secrets*", "*token*", "*.kdbx",
                   ".npmrc", ".netrc", ".git-credentials", "auth.json", "*.p8", "*.jks")


def _is_secret_path(rel: str) -> bool:
    """Секретные пути не попадают в карту: индекс цитирует имена, но не секреты.

    Примеры настроек (`.env.example`, `.sample`, `.template`) — часть шаблона
    проекта и обязаны быть в карте: в них нет значений.
    """
    name = rel.rsplit("/", 1)[-1].lower()
    if name in {".env.example", ".env.sample", ".env.template"}:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


def _git_blob(git_root: str, path: str) -> bytes | None:
    code, out = _git(git_root, "show", f"HEAD:{path}")
    return out.encode("utf-8") if code == 0 else None


def _repo_dir(root: str) -> str:
    candidate = os.path.join(root, "repo")
    # В рабочей копии задачи `.git` — файл-указатель на общий репозиторий.
    return candidate if os.path.exists(os.path.join(candidate, ".git")) else root


def op_task_start(root: str, args: dict) -> dict:
    """Отдельная рабочая копия задачи: worktree от зафиксированного SHA.

    Копия живёт в `<проект>/runs/<run>`: внутри `repo/` — рабочий каталог ветки
    задачи, `ops/` — копия журналов на момент старта. Основная копия не меняется.
    """
    import shutil

    run_id = str(args.get("run_id") or "")
    branch = str(args.get("branch") or "")
    base_sha = str(args.get("base_sha") or "")
    if not run_id or not branch:
        raise OpFail("bad_arg", "run_id и branch обязательны")
    repo = _repo_dir(root)
    task_root = os.path.join(root, "runs", run_id[:8])
    task_repo = os.path.join(task_root, "repo")
    if os.path.isdir(task_repo):
        code, head = _git(task_repo, "rev-parse", "HEAD")
        if code != 0:
            raise OpFail("git_failed", f"копия задачи без HEAD: {task_repo}")
        _, current = _git(task_repo, "rev-parse", "--abbrev-ref", "HEAD")
        current, head = current.strip(), head.strip()
        if current != branch:
            raise OpFail("task_conflict",
                         f"копия {task_root} на ветке {current}, ожидалась {branch}")
        if base_sha and head != base_sha:
            raise OpFail("task_conflict",
                         f"копия {task_root} на {head[:12]}, база задания {base_sha[:12]}")
        return {"task_root": task_root, "branch": current,
                "base_sha": base_sha, "head_sha": head, "created": False}
    if not base_sha:
        code, head = _git(repo, "rev-parse", "HEAD")
        if code != 0:
            raise OpFail("git_failed", "cannot read HEAD for task start")
        base_sha = head.strip()
    os.makedirs(os.path.join(root, "runs"), exist_ok=True)
    code, out = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    if code == 0:
        # Существующая ветка задания переиспользуется только от той же базы:
        # иначе копия унаследует чужие коммиты прошлого прогона.
        _, ref_head = _git(repo, "rev-parse", f"refs/heads/{branch}")
        if base_sha and ref_head.strip() != base_sha:
            raise OpFail("task_conflict",
                         f"ветка {branch} уже на {ref_head.strip()[:12]}, база задания {base_sha[:12]}")
        code, out = _git(repo, "worktree", "add", task_repo, branch)
    else:
        code, out = _git(repo, "worktree", "add", "-b", branch, task_repo, base_sha)
    if code != 0:
        raise OpFail("git_failed", f"git worktree add failed: {out[:300]}")
    src_ops = os.path.join(root, "ops")
    if os.path.isdir(src_ops):
        shutil.copytree(src_ops, os.path.join(task_root, "ops"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("backups", "runs"))
    code, head = _git(task_repo, "rev-parse", "HEAD")
    if code != 0 or not head.strip():
        raise OpFail("git_failed", f"копия задачи без HEAD: {task_repo}")
    head = head.strip()
    if base_sha and head != base_sha:
        raise OpFail("task_conflict",
                     f"копия начата с {head[:12]}, база задания {base_sha[:12]}")
    return {"task_root": task_root, "branch": branch, "base_sha": base_sha,
            "head_sha": head, "created": True}


def op_verify_task(root: str, args: dict) -> dict:
    """Сверка рабочей папки и ветки задания перед записью.

    Несовпадение ветки или коммита — отказ до изменения файлов.
    """
    repo = _repo_dir(root)
    want_branch = str(args.get("branch") or "")
    want_head = str(args.get("head_sha") or "")
    code, head = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        raise OpFail("git_failed", f"не Git-репозиторий: {root}")
    _, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip()
    head = head.strip()
    if want_branch and branch != want_branch:
        raise OpFail("task_mismatch", f"ветка задания {want_branch}, в копии {branch}")
    if want_head and head != want_head:
        raise OpFail("task_mismatch", f"коммит задания {want_head[:12]}, в копии {head[:12]}")
    return {"root": repo, "branch": branch, "head_sha": head,
            "dirty": bool((_git(repo, "status", "--porcelain")[1] or "").strip())}


def op_task_merge(root: str, args: dict) -> dict:
    """Слияние проверенного коммита в основную ветку только через --ff-only.

    Под блокировкой проекта: если основная ветка сдвинулась, слияние не делается,
    ветка задачи сохраняется, а в ответе — причина и текущие коммиты.
    """
    import time as _time

    repo = _repo_dir(root)
    branch = str(args.get("branch") or "")
    sha = str(args.get("sha") or "")
    target = str(args.get("target") or "main")
    base = str(args.get("base_sha") or "")
    if not branch or not sha:
        raise OpFail("bad_arg", "branch и sha обязательны")
    # Слияние выполняется в основной рабочей копии: целевая ветка может быть
    # занята ею, а не копией задачи.
    code, listed = _git(repo, "worktree", "list", "--porcelain")
    if code == 0 and listed.strip():
        primary = listed.splitlines()[0].replace("worktree ", "").strip()
        if primary:
            repo = primary
    code, git_dir = _git(repo, "rev-parse", "--git-dir")
    if code != 0:
        raise OpFail("git_failed", f"не Git-репозиторий: {root}")
    git_dir = git_dir.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo, git_dir)
    lock_path = os.path.join(git_dir, "aihub-merge.lock")
    locked = False
    for _ in range(50):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            locked = True
            break
        except FileExistsError:
            _time.sleep(0.2)
    if not locked:
        raise OpFail("locked", "проект занят слиянием")
    try:
        code, out = _git(repo, "checkout", target)
        if code != 0:
            raise OpFail("git_failed", f"checkout {target}: {out[:200]}")
        code, target_head = _git(repo, "rev-parse", "HEAD")
        target_head = target_head.strip()
        # Правило проекта: если основная ветка сдвинулась с базы запуска,
        # слияние не выполняется — ветка задачи сохраняется для повторной проверки.
        if base and target_head != base:
            return {"merged": False, "reason": "base_moved", "target": target,
                    "target_head": target_head, "task_sha": sha,
                    "message": "основная ветка уже сдвинулась до слияния: "
                               "обновите базу и повторите проверки",
                    "task_branch": branch}
        code, merged = _git(repo, "merge", "--ff-only", sha)
        if code != 0:
            return {"merged": False, "reason": "base_moved", "target": target,
                    "target_head": target_head.strip(), "task_sha": sha,
                    "message": "основная ветка сдвинулась: обновите базу и повторите проверки",
                    "task_branch": branch}
        code, head = _git(repo, "rev-parse", "HEAD")
        head = head.strip()
        if head != sha:
            return {"merged": False, "reason": "sha_mismatch", "target_head": head,
                    "task_sha": sha, "task_branch": branch}
        return {"merged": True, "sha": head, "target": target, "task_branch": branch,
                "previous_head": target_head}
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def op_setup_env(root: str, args: dict) -> dict:
    """Команды подготовки среды проекта — в рабочей копии задачи, до кодера."""
    repo = _repo_dir(root)
    cmd = args.get("cmd") or []
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        raise OpFail("bad_arg", "cmd: нужен непустой список строк")
    timeout_s = max(1, min(600, int(args.get("timeout_s") or 180)))
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                              timeout=timeout_s, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpFail("setup_failed", f"подготовка среды не выполнена: {exc}") from exc
    out, err = (proc.stdout or "")[-4000:], (proc.stderr or "")[-2000:]
    if proc.returncode != 0:
        raise OpFail("setup_failed",
                     f"подготовка среды вернула {proc.returncode}: {(err or out)[:300]}")
    return {"ok": True, "exit_code": proc.returncode, "stdout_tail": out, "stderr_tail": err,
            "cmd": cmd, "cwd": repo}


def op_archive_artifacts(root: str, args: dict) -> dict:
    """Журнал зарегистрированных артефактов запуска в ops/ARTIFACTS.md.

    Переносятся только зарегистрированные артефакты; исходники и неизвестные
    файлы не трогаются. Повтор по тому же запуску заменяет его раздел.
    """
    import hashlib
    import shutil

    run_id = str(args.get("run_id") or "").strip()
    items = args.get("artifacts") or []
    if not run_id or not isinstance(items, list):
        raise OpFail("bad_arg", "run_id и artifacts обязательны")
    # Полный идентификатор и только hex: префикс из восьми символов может
    # совпасть у двух запусков, а «../» увёл бы копии за пределы ops/.
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise OpFail("bad_arg", "run_id должен быть полным hex-идентификатором (32 символа)")
    if not items:
        import glob as _glob
        items = []
        for pattern in ("ops/pr/*.md", "ops/INDEX.md", "ops/BACKUPS.md"):
            for found in sorted(_glob.glob(os.path.join(root, pattern))):
                items.append({"path": os.path.relpath(found, root).replace(os.sep, "/")})
        zips = sorted(_glob.glob(os.path.join(root, "ops/backups/*.zip")),
                      key=lambda f: os.path.getmtime(f), reverse=True)[:3]
        for found in zips:
            items.append({"path": os.path.relpath(found, root).replace(os.sep, "/")})
    target = os.path.join(root, "ops", "ARTIFACTS.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    head = "# Артефакты запусков\n\nЗарегистрированные артефакты и их версии. Исходники не переносятся: за них отвечает Git.\n"
    text = ""
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = head
    marker = f"## {run_id}"
    block = [marker, "",
             f"Коммит: `{args.get('sha') or '—'}` · ветка: `{args.get('branch') or '—'}`", "",
             "| Артефакт | Версия | Ссылка |", "| --- | --- | --- |"]
    ops_root = os.path.realpath(os.path.join(root, "ops"))
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip()
        if not path:
            continue
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        # Регистрируются только существующие файлы внутри ops/ проекта:
        # запись версии артефакта, которого нет, — ложный журнал.
        if os.path.commonpath([full, ops_root]) != ops_root:
            raise OpFail("bad_arg", f"артефакт вне ops/: {path}")
        if not os.path.isfile(full):
            raise OpFail("bad_arg", f"артефакта нет: {rel}")
        # Неизменяемая версия: изменяемые ops/pr/*.md и INDEX.md перезаписываются
        # следующим запуском, и старые разделы журнала перестают быть правдой.
        hist_rel = f"ops/artifacts/{run_id}/{rel}"
        hist_full = os.path.join(root, hist_rel)
        if not os.path.isfile(hist_full):
            os.makedirs(os.path.dirname(hist_full), exist_ok=True)
            shutil.copy2(full, hist_full)
        h = hashlib.sha256()
        with open(hist_full, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        block.append(f"| `{os.path.basename(full)}` | `{h.hexdigest()[:16]}` | `{hist_rel}` |")
    block.append("")
    if marker in text:
        head_part, _, rest = text.partition(marker)
        _, _, after = rest.partition("\n\n## ")
        tail = ("\n\n## " + after) if after else ""
        text = head_part.rstrip() + "\n\n" + "\n".join(block) + tail
    else:
        text = text.rstrip() + "\n\n" + "\n".join(block)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return {"path": "ops/ARTIFACTS.md", "run_id": run_id, "artifacts": len(items),
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}


def op_publish_branch(root: str, args: dict) -> dict:
    """Публикация ветки в настроенный remote — только если проект это разрешил."""
    repo = _repo_dir(root)
    branch = str(args.get("branch") or "")
    remote = str(args.get("remote") or "origin")
    if not branch:
        raise OpFail("bad_arg", "branch обязателен")
    code, url = _git(repo, "remote", "get-url", remote)
    if code != 0 or not url.strip():
        raise OpFail("no_remote", f"remote {remote} не настроен")
    code, out = _git(repo, "push", "-u", remote, branch)
    if code != 0:
        raise OpFail("push_failed", f"git push: {(out or '')[:300]}")
    return {"pushed": True, "remote": remote, "branch": branch, "url": url.strip(),
            "output": (out or "")[-1000:]}


def op_refresh_index(root: str, args: dict) -> dict:
    """Карта проекта по зафиксированному коммиту: путь, размер, версия и контрольная сумма.

    Источник — `git ls-tree HEAD`, содержимое берётся из самого коммита, а не из
    рабочего каталога. Секретные пути исключаются. Одинаковый коммит даёт
    одинаковый файл: повтор не меняет `ops/INDEX.md`.
    """
    import hashlib

    git_root = os.path.join(root, "repo")
    if not os.path.isdir(os.path.join(git_root, ".git")):
        git_root = root
    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD for index")
    head = head.strip()
    code, out = _git(git_root, "ls-tree", "-r", "--long", "HEAD")
    if code != 0:
        raise OpFail("git_failed", f"git ls-tree failed: {out[:200]}")
    rows, total, secrets = [], 0, []
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or not path:
            continue
        if _is_secret_path(path):
            secrets.append(path)
            continue
        blob, size = parts[2], parts[3]
        raw = _git_blob(git_root, path)
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else "—"
        rows.append(f"| `{path}` | {size} | `{blob[:12]}` | `{digest[:16]}` |")
        total += 1
        if total >= INDEX_MAX_FILES:
            break
    body = [
        "# Карта проекта",
        "",
        f"Коммит: `{head}` · файлов в карте: {total} · секретов исключено: {len(secrets)}",
        "",
        "| Путь | Байт | Версия (git) | SHA-256 (16) |",
        "| --- | --- | --- | --- |",
    ] + rows
    if secrets:
        body += ["", "Исключены как секреты: " + ", ".join(f"`{p}`" for p in secrets[:20]) + "."]
    body += ["", "Карта построена по зафиксированному коммиту; изменение файлов без коммита в ней не видно.", ""]
    content = "\n".join(body)
    target = os.path.join(root, "ops", "INDEX.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    changed = True
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            changed = fh.read() != content
    if changed:
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    return {"head_sha": head, "files": total, "secrets": secrets[:20],
            "path": "ops/INDEX.md", "changed": changed,
            "digest": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]}


def op_search_git(root: str, args: dict) -> dict:
    """Поиск по именам и строкам коммита: результат несёт путь, строку и версию файла."""
    import hashlib

    git_root = os.path.join(root, "repo")
    if not os.path.isdir(os.path.join(git_root, ".git")):
        git_root = root
    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD for search")
    head = head.strip()
    query = str(args.get("q") or "").strip()
    limit = int(args.get("limit") or 50)
    matches: list[dict] = []
    code, names = _git(git_root, "ls-tree", "-r", "--name-only", "HEAD")
    for path in (names or "").splitlines():
        if query.lower() in path.lower() and not _is_secret_path(path):
            raw = _git_blob(git_root, path)
            matches.append({"path": path, "line": None, "text": path,
                            "sha256": hashlib.sha256(raw).hexdigest()[:16] if raw else "—",
                            "kind": "name"})
        if len(matches) >= limit:
            break
    code, out = _git(git_root, "grep", "-n", "-I", "-F", "-e", query, "HEAD", "--")
    if code == 0:
        for line in out.splitlines():
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            _, path, line_no, text = parts
            if _is_secret_path(path):
                continue
            raw = _git_blob(git_root, path)
            matches.append({"path": path, "line": int(line_no), "text": text[:200],
                            "sha256": hashlib.sha256(raw).hexdigest()[:16] if raw else "—",
                            "kind": "content"})
            if len(matches) >= limit:
                break
    return {"head_sha": head, "q": query, "matches": matches[:limit]}


def op_read_at(root: str, args: dict) -> dict:
    """Фрагмент файла из коммита с проверкой версии: чужой SHA — отказ, а не догадка."""
    import hashlib

    git_root = os.path.join(root, "repo")
    if not os.path.isdir(os.path.join(git_root, ".git")):
        git_root = root
    path = str(args.get("path") or "")
    want = str(args.get("sha256") or "").lower()
    if not path or _is_secret_path(path):
        raise OpFail("denied_path", f"путь запрещён политикой: {path}")
    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD")
    raw = _git_blob(git_root, path)
    if raw is None:
        raise OpFail("not_found", f"нет файла в коммите: {path}")
    digest = hashlib.sha256(raw).hexdigest()
    if want and not digest.startswith(want):
        raise OpFail("sha_mismatch", f"{path}: sha256 {digest[:16]} != {want[:16]}")
    text = raw.decode("utf-8", "replace")
    return {"path": path, "head_sha": head.strip(), "sha256": digest,
            "size": len(raw), "text": text[:max(1, int(args.get("length") or 64 * 1024))]}


def _local_llm_url() -> str:
    return (os.environ.get("AIHUB_LOCAL_LLM") or "http://127.0.0.1:11434").rstrip("/")


def _describe_with_model(path: str, snippet: str, timeout_s: float = 20.0) -> str | None:
    """Краткое описание файла локальной моделью; недоступная модель — не ошибка."""
    import urllib.request

    prompt = ("Опиши назначение файла в одном предложении по-русски. "
              f"Путь: {path}\nСодержимое:\n{snippet[:1500]}")
    body = json.dumps({"model": os.environ.get("AIHUB_LOCAL_MODEL") or "local",
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 80, "temperature": 0.1}).encode("utf-8")
    request = urllib.request.Request(_local_llm_url() + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            data = json.load(resp)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return " ".join(text.split())[:400] or None
    except Exception:  # noqa: BLE001 — модель может быть выключена
        return None


def op_describe_files(root: str, args: dict) -> dict:
    """Описания файлов коммита локальной моделью; карта обновляется и без модели."""
    git_root = os.path.join(root, "repo")
    if not os.path.isdir(os.path.join(git_root, ".git")):
        git_root = root
    code, head = _git(git_root, "rev-parse", "HEAD")
    if code != 0 or not head:
        raise OpFail("git_failed", "cannot read HEAD for descriptions")
    head = head.strip()
    limit = min(int(args.get("limit") or 20), 50)
    code, names = _git(git_root, "ls-tree", "-r", "--name-only", "HEAD")
    rows, described, available = [], 0, True
    for path in (names or "").splitlines():
        if _is_secret_path(path) or path.endswith((".png", ".jpg", ".zip", ".pdf")):
            continue
        raw = _git_blob(git_root, path)
        if raw is None:
            continue
        try:
            snippet = raw.decode("utf-8")[:1500]
        except UnicodeDecodeError:
            continue
        text = _describe_with_model(path, snippet)
        if text is None:
            available = False
            break
        rows.append(f"| `{path}` | {text} |")
        described += 1
        if described >= limit:
            break
    if not available:
        return {"available": False, "head_sha": head, "described": 0,
                "note": "локальная модель недоступна; карта не менялась"}
    body = [f"# Описания файлов — {head[:12]}", "",
            "| Путь | Описание |", "| --- | --- |"] + rows + [""]
    content = "\n".join(body)
    target = os.path.join(root, "ops", "DESCRIPTIONS.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return {"available": True, "head_sha": head, "described": described,
            "path": "ops/DESCRIPTIONS.md", "model": _local_llm_url()}


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
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise OpFail("writer_busy", "another workspace writer holds this checkout")
                    time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
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


# ---------- семантический поиск: локальные эмбеддинги ----------
# Код и документация индексируются разными моделями (как в документе проекта):
# code_retriever_ru_en для кода, E5 для markdown/текста. Обе — локальные
# llama-server с флагом --embeddings, поэтому наружу ничего не уходит.
# Код в пилоте идёт через E5: сборка llama.cpp 12058 не грузит modernbert-модель
# code_retriever_ru_en (key not found: modern-bert.attention.layer_norm_epsilon).
# Как только движок научится, достаточно задать AIHUB_EMBED_CODE_URL на отдельный порт.
EMBED_CODE_URL = os.environ.get("AIHUB_EMBED_CODE_URL", "")
EMBED_DOCS_URL = os.environ.get("AIHUB_EMBED_DOCS_URL", "http://127.0.0.1:11442")

CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".cs",
             ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".sh", ".ps1", ".sql", ".lua",
             ".gd", ".swift", ".m", ".mm", ".scala", ".ex", ".exs", ".vue", ".svelte"}
DOC_EXTS = {".md", ".txt", ".rst", ".adoc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".json", ".env.example"}
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
             "vendor", "target", ".idea", ".next", "runs", "backups"}


def _semantic_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXTS:
        return "code"
    if ext in DOC_EXTS or os.path.basename(path).lower().startswith("readme"):
        return "docs"
    return ""


def _embed_texts(texts: list[str], kind: str, timeout: int = 180) -> list[list[float]]:
    """Векторы от локального llama-server. Батчами, чтобы не рвать контекст модели."""
    import urllib.request

    url = (EMBED_CODE_URL if kind == "code" and EMBED_CODE_URL else EMBED_DOCS_URL).rstrip("/")
    if not texts:
        return []
    out: list[list[float]] = []
    for start in range(0, len(texts), 16):
        batch = texts[start:start + 16]
        body = json.dumps({"model": "local", "input": batch}).encode("utf-8")
        req = urllib.request.Request(url + "/v1/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — сервис эмбеддингов может быть не поднят
            raise OpFail("embed_unavailable",
                         f"модель эмбеддингов {kind} недоступна ({url}): {str(exc)[:200]}") from None
        rows = data.get("data") or []
        if len(rows) != len(batch):
            raise OpFail("embed_failed", f"модель вернула {len(rows)} векторов на {len(batch)}")
        for row in rows:
            vec = row.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise OpFail("embed_failed", "пустой вектор эмбеддинга")
            out.append([round(float(x), 6) for x in vec])
    return out


def _chunk_lines(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    chunks: list[tuple[int, int, str]] = []
    step = max(1, size - overlap)
    index = 0
    while index < len(lines):
        part = lines[index:index + size]
        if not any(line.strip() for line in part):
            index += step
            continue
        body = "\n".join(part)
        chunks.append((index + 1, index + len(part), body[:4000]))
        if index + size >= len(lines):
            break
        index += step
    return chunks


def op_semantic_index(root: str, args: dict) -> dict:
    """Фрагменты коммита и их векторы. Ничего не пишет на диск: результат — в хаб."""
    repo = _repo_dir(root)
    sha = str(args.get("sha") or "").strip()
    if not sha:
        code, sha = _git(repo, "rev-parse", "HEAD")
        sha = (sha or "").strip()
        if code != 0 or not sha:
            raise OpFail("git_failed", "cannot read HEAD")
    code, listing = _git(repo, "ls-tree", "-r", "-l", sha)
    if code != 0:
        raise OpFail("git_failed", f"git ls-tree: {(listing or '')[:200]}")
    budget = int(args.get("max_chunks") or 240)
    files = 0
    chunks: list[dict] = []
    pending: dict[str, list[dict]] = {"code": [], "docs": []}
    for line in (listing or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        meta, path = parts[3], parts[4].strip()
        if not path or int(parts[3] or 0) > 400_000:
            continue
        if any(part in SKIP_DIRS for part in path.split("/")):
            continue
        kind = _semantic_kind(path)
        if not kind:
            continue
        rc, blob = _git(repo, "show", f"{sha}:{path}")
        if rc != 0 or not blob or "\x00" in blob[:4000]:
            continue
        size = 40 if kind == "code" else 60
        overlap = 8 if kind == "code" else 10
        for start, end, body in _chunk_lines(blob, size, overlap):
            if len(chunks) + sum(len(v) for v in pending.values()) >= budget:
                break
            pending[kind].append({"path": path, "start": start, "end": end,
                                  "text": f"{path}:{start}-{end}\n{body}", "kind": kind})
        files += 1
    for kind in ("code", "docs"):
        items = pending[kind]
        if not items:
            continue
        vectors = _embed_texts([item["text"] for item in items], kind)
        for item, vec in zip(items, vectors):
            item["vector"] = vec
            chunks.append(item)
    stored = 0
    if args.get("upload") and chunks:
        stored = _upload_index(str(args.get("project_id") or ""), sha, chunks)
    return {"sha": sha, "files": files, "chunks": len(chunks), "stored": stored,
            "uploaded": stored > 0,
            "code_url": EMBED_CODE_URL or EMBED_DOCS_URL, "docs_url": EMBED_DOCS_URL}


def _upload_index(project_id: str, sha: str, items: list[dict]) -> int:
    """Векторы не проходят через лимит результата задания — отправляем их в хаб.

    Результат задания обрезается примерно на 100 КБ, а индекс из сотен фрагментов
    больше. Поэтому воркер сам кладёт индекс в хаб тем же токеном.
    """
    import urllib.request

    hub = (os.environ.get("HUB_URL") or "").rstrip("/")
    token = os.environ.get("HUB_TOKEN") or ""
    if not hub or not project_id:
        raise OpFail("no_hub", "не заданы HUB_URL/project_id для загрузки индекса")
    body = json.dumps({"sha": sha, "model": f"{EMBED_CODE_URL or EMBED_DOCS_URL}|{EMBED_DOCS_URL}",
                       "items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{hub}/projects/{project_id}/semantic/ingest", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise OpFail("upload_failed", f"хаб не принял индекс: {str(exc)[:200]}") from None
    return int(data.get("stored") or 0)


def op_semantic_embed(root: str, args: dict) -> dict:
    text = str(args.get("text") or "").strip()
    kind = str(args.get("kind") or "docs")
    if not text:
        raise OpFail("bad_arg", "text обязателен")
    if kind not in ("code", "docs"):
        raise OpFail("bad_arg", "kind: code или docs")
    return {"kind": kind, "vector": _embed_texts([text[:2000]], kind)[0]}


def _git_bytes(root: str, *args: str) -> tuple[int, bytes]:
    """Сырые байты blob'а: текстовый _git обрезает пробелы и ломает номера строк."""
    try:
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=30)
        return proc.returncode, proc.stdout or b""
    except (OSError, subprocess.SubprocessError):
        return 127, b""


def op_verify_evidence(root: str, args: dict) -> dict:
    """Проверка фрагментов семантического контекста по blob'ам указанного коммита.

    Хаб хранит только пути и диапазоны; текст и blob_oid подтверждает хост, у
    которого есть репозиторий. Несовпавший фрагмент отбрасывается, а не правится.
    """
    repo = _repo_dir(root)
    sha = str(args.get("sha") or "").strip()
    if not sha:
        raise OpFail("bad_arg", "sha обязателен")
    code, listing = _git(repo, "ls-tree", "-r", sha)
    if code != 0:
        raise OpFail("git_failed", f"git ls-tree {sha}: {(listing or '')[:200]}")
    oids: dict[str, str] = {}
    for line in (listing or "").splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            oids[path.strip()] = parts[2]
    verified: list[dict] = []
    dropped: list[dict] = []
    cache: dict[str, str] = {}
    for item in (args.get("items") or [])[:20]:
        path = str(item.get("path") or "").strip().lstrip("./")
        try:
            start = int(item.get("start_line") or 0)
            end = int(item.get("end_line") or 0)
        except (TypeError, ValueError):
            dropped.append({"path": path, "reason": "bad_range"})
            continue
        oid = oids.get(path)
        if not oid:
            dropped.append({"path": path, "reason": "not_in_sha"})
            continue
        if path not in cache:
            rc, blob = _git_bytes(repo, "cat-file", "blob", oid)
            if rc != 0 or b"\x00" in blob[:4000]:
                dropped.append({"path": path, "reason": "blob_unreadable"})
                continue
            cache[path] = blob.decode("utf-8", "replace")
        lines = cache[path].splitlines()
        if start < 1 or end < start or end > len(lines):
            dropped.append({"path": path, "reason": "range_out_of_bounds"})
            continue
        text = "\n".join(lines[start - 1:end])
        if not text.strip() or len(text) > 20000:
            dropped.append({"path": path, "reason": "text_unusable"})
            continue
        verified.append({"path": path, "start_line": start, "end_line": end,
                         "kind": str(item.get("kind") or "code")[:12],
                         "blob_oid": oid, "sha": sha,
                         "text": text,
                         "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return {"sha": sha, "verified": verified, "dropped": dropped}


HANDLERS = {
    "init_project": op_init_project,
    "write_reports": op_write_reports,
    "create_backup": op_create_backup,
    "refresh_index": op_refresh_index,
    "search_git": op_search_git,
    "read_at": op_read_at,
    "describe_files": op_describe_files,
    "task_start": op_task_start,
    "verify_task": op_verify_task,
    "task_merge": op_task_merge,
    "setup_env": op_setup_env,
    "archive_artifacts": op_archive_artifacts,
    "publish_branch": op_publish_branch,
    "inspect": op_inspect,
    "snapshot": op_snapshot,
    "list_files": op_list_files,
    "read_files": op_read_files,
    "apply_patch": op_apply_patch,
    "run_checks": op_run_checks,
    "package": op_package,
    "semantic_index": op_semantic_index,
    "semantic_embed": op_semantic_embed,
    "verify_evidence": op_verify_evidence,
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
