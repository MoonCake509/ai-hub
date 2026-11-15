"""CORE-02b — управляемые операции рабочего пространства на устройстве воркера.

Сервер не предполагает, что видит диски ноутбука: карту и версии проекта он
получает проверяемыми операциями host worker. Набор операций фиксирован, а не
"пришли произвольный Python". Аргументы проверяются по схеме; путь канонизируется
на языке той ОС, где корень существует (Windows — ntpath, иначе posixpath), и
обязан лежать внутри корня проекта этого host. Выход через "..", абсолютные
подстановки и служебные/секретные файлы отвергаются до постановки задания.

Модифицирующие операции несут idempotency key и digest входов; результат каждой
операции сохраняется как durable receipt, поэтому повтор с тем же ключом
возвращает прежний результат, а не применяет патч второй раз.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import ntpath
import os
import posixpath
import re
import time

from . import db, workers, security_policy

OPS = {
    "init_project": "рабочая папка проекта: шаблон, git init и первый коммит",
    "archive_project": "перенос рабочей папки проекта в архив рядом с корнем проектов",
    "write_reports": "живые отчёты проекта в ops/: журнал и черновики PR",
    "create_backup": "точка восстановления: zip коммита и строка в ops/BACKUPS.md",
    "refresh_index": "карта проекта ops/INDEX.md по зафиксированному коммиту",
    "search_git": "поиск по именам и строкам коммита с версией файла",
    "read_at": "фрагмент файла из коммита с проверкой версии",
    "describe_files": "описания файлов локальной моделью в ops/DESCRIPTIONS.md",
    "inspect": "канонический корень, git HEAD/status digest, возможности host",
    "snapshot": "управляемая копия и зафиксированное базовое состояние",
    "list_files": "ограниченный список путей с размерами и хешами",
    "read_files": "только запрошенные разрешённые версии/диапазоны",
    "apply_patch": "проверка write_set/preimages, применение, receipt и tree digest",
    "run_checks": "утверждённый check plan в тестовой среде",
    "package": "проверенный review bundle и manifest",
    "task_start": "отдельная рабочая копия задачи от зафиксированного коммита",
    "verify_task": "сверка рабочей папки и ветки задания перед записью",
    "task_merge": "слияние проверенного коммита в основную ветку через --ff-only",
    "setup_env": "команды подготовки среды в рабочей копии задачи",
    "archive_artifacts": "журнал зарегистрированных артефактов запуска",
    "publish_branch": "публикация ветки в настроенный remote",
    "semantic_index": "векторы фрагментов коммита локальными моделями",
    "verify_evidence": "проверка фрагментов семантического контекста по blob'ам коммита",
    "semantic_embed": "вектор запроса локальной моделью",
}

# Операции, которые меняют рабочее дерево пользователя: только они берут
# писательскую аренду checkout. inspect/list/read/snapshot/package пишут лишь в
# управляемую область воркера и не должны блокировать кодеров.
WRITES_ROOT = {"apply_patch"}
# Операции, меняющие управляемое состояние: им нужен idempotency key по умолчанию.
MUTATING = {"snapshot", "apply_patch", "package", "init_project", "archive_project", "write_reports",
            "create_backup", "refresh_index", "task_start", "task_merge", "setup_env",
            "archive_artifacts", "publish_branch"}

MAX_LIST = 200
MAX_READ_FILES = 8
MAX_READ_BYTES = 64 * 1024
MAX_PATCH_FILES = 50
MAX_PATCH_BYTES = 1024 * 1024
MAX_CHECKS = 10

# Deny-политика — единый источник с host worker: hub/app/security_policy.py.
DENY_DIRS = security_policy.DENY_DIRS
DENY_SUFFIX = security_policy.SECRET_SUFFIXES
DENY_NAMES = security_policy.SECRET_NAMES


class OpError(ValueError):
    """Отказ по схеме операции: код машинный, текст — человеку."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


def _is_windows(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path or ""))


def _mod(path: str):
    return ntpath if _is_windows(path) else posixpath


def canon(path: str) -> str:
    """Канонический абсолютный путь без завершающего разделителя."""
    text = (path or "").strip().strip('"')
    if not text:
        raise OpError("bad_path", "путь не задан")
    if "\x00" in text:
        raise OpError("bad_path", "NUL в пути")
    mod = _mod(text)
    norm = mod.normpath(text)
    if norm == ".." or norm.startswith(".." + "/") or norm.startswith(".." + chr(92)):
        raise OpError("traversal", "путь выходит за текущий каталог")
    if mod is ntpath:
        if not re.match(r"^[A-Za-z]:\\", norm):
            raise OpError("bad_path", "ожидался абсолютный путь с буквой диска")
    elif not norm.startswith("/"):
        raise OpError("bad_path", "ожидался абсолютный POSIX-путь")
    return norm


def ckey(path: str) -> str:
    """Ключ сравнения: на Windows регистр и разделители не значимы."""
    mod = _mod(path)
    return mod.normcase(mod.normpath(path)).replace("\\", "/").rstrip("/")


def is_within(root: str, path: str) -> bool:
    r, p = ckey(root), ckey(path)
    return p == r or p.startswith(r + "/")


def resolve(root: str, rel: str) -> str:
    """Путь относительно корня; отказ на абсолютный путь, ".." и выход наружу."""
    text = (rel or "").strip().replace("\\", "/")
    if not text or text in (".", "/"):
        raise OpError("bad_path", "относительный путь не задан")
    if text.startswith("/") or _is_windows(text) or re.match(r"^[A-Za-z]:", text):
        raise OpError("absolute_path", "ожидался путь относительно корня")
    parts = [p for p in text.split("/") if p]
    if any(p == ".." for p in parts):
        raise OpError("traversal", "`..` в относительном пути запрещён")
    full = _mod(root).normpath(_mod(root).join(root, *parts))
    if not is_within(root, full):
        raise OpError("outside_root", "путь выходит за корень проекта")
    return full


def denied_reason(rel: str) -> str | None:
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


def _sha(obj) -> str:
    body = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _int(value, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise OpError("bad_arg", f"{name}: ожидалось целое") from None
    if number < low or number > high:
        raise OpError("bad_arg", f"{name}: допустимо {low}..{high}")
    return number


def _str(value, name: str, limit: int = 128) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise OpError("bad_arg", f"{name}: ожидалась строка")
    if len(value) > limit:
        raise OpError("bad_arg", f"{name}: длиннее {limit}")
    return value.strip()


def validate(op: str, args: dict | None) -> dict:
    """Проверка аргументов по схеме операции. Возвращает нормализованный dict."""
    if op not in OPS:
        raise OpError("unknown_op", f"неизвестная операция: {op}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise OpError("bad_arg", "args должен быть объектом")
    unknown = set(args) - {"root", "label", "glob", "limit", "offset", "files",
                           "changes", "checks", "snapshot_id", "include",
                           "base_tree_digest", "name", "branch", "author_name",
                           "author_email", "template_version", "template_files",
                           "repo_subdir", "reports", "label", "host",
                           "q", "path", "sha256", "length",
                           "run_id", "head_sha", "base_sha", "sha", "target", "cmd", "timeout_s",
                           "artifacts", "remote", "text", "kind", "max_chunks", "project_id", "upload",
                           "items"}
    if unknown:
        raise OpError("bad_arg", "лишние поля: " + ", ".join(sorted(unknown)))
    root = canon(_str(args.get("root"), "root", 400))
    clean: dict = {"root": root}

    if op == "refresh_index":
        return clean

    if op == "search_git":
        query = _str(args.get("q"), "q", 200)
        if not query.strip():
            raise OpError("bad_arg", "q обязателен")
        clean["q"] = query
        clean["limit"] = _int(args.get("limit", 50), 1, 100, "limit")
        return clean

    if op == "describe_files":
        clean["limit"] = _int(args.get("limit", 20), 1, 50, "limit")
        return clean

    if op == "semantic_index":
        clean["sha"] = _str(args.get("sha"), "sha", 64)
        clean["max_chunks"] = _int(args.get("max_chunks", 240), 1, 500, "max_chunks")
        clean["project_id"] = _str(args.get("project_id"), "project_id", 64)
        clean["upload"] = bool(args.get("upload"))
        return clean

    if op == "verify_evidence":
        clean["sha"] = _str(args.get("sha"), "sha", 64)
        if not clean["sha"]:
            raise OpError("bad_arg", "sha обязателен")
        raw = args.get("items")
        if not isinstance(raw, list) or not raw:
            raise OpError("bad_arg", "items обязателен")
        if len(raw) > 20:
            raise OpError("bad_arg", "items: не больше 20")
        clean["items"] = [{
            "path": _str(item.get("path"), "path", 400),
            "start_line": _int(item.get("start_line"), 1, 2_000_000, "start_line"),
            "end_line": _int(item.get("end_line"), 1, 2_000_000, "end_line"),
            "kind": (_str(item.get("kind"), "kind", 12) or "code"),
        } for item in raw if isinstance(item, dict)]
        if not clean["items"]:
            raise OpError("bad_arg", "items пуст")
        return clean

    if op == "semantic_embed":
        clean["text"] = _str(args.get("text"), "text", 4000)
        clean["kind"] = _str(args.get("kind"), "kind", 8) or "docs"
        if not clean["text"].strip():
            raise OpError("bad_arg", "text обязателен")
        if clean["kind"] not in ("code", "docs"):
            raise OpError("bad_arg", "kind: code или docs")
        return clean

    if op == "read_at":
        rel = _str(args.get("path"), "path", 400)
        reason = denied_reason(rel)
        if reason:
            raise OpError("denied_path", f"{rel}: {reason}")
        clean["path"] = rel
        clean["sha256"] = _str(args.get("sha256"), "sha256", 64)
        clean["length"] = _int(args.get("length", 64 * 1024), 1, MAX_READ_BYTES, "length")
        return clean

    if op == "task_start":
        clean["run_id"] = _str(args.get("run_id"), "run_id", 64)
        clean["branch"] = _str(args.get("branch"), "branch", 120)
        clean["base_sha"] = _str(args.get("base_sha"), "base_sha", 64)
        if not clean["run_id"] or not clean["branch"]:
            raise OpError("bad_arg", "run_id и branch обязательны")
        return clean

    if op == "verify_task":
        clean["branch"] = _str(args.get("branch"), "branch", 120)
        clean["head_sha"] = _str(args.get("head_sha"), "head_sha", 64)
        return clean

    if op == "task_merge":
        clean["branch"] = _str(args.get("branch"), "branch", 120)
        clean["sha"] = _str(args.get("sha"), "sha", 64)
        clean["base_sha"] = _str(args.get("base_sha"), "base_sha", 64)
        clean["target"] = _str(args.get("target"), "target", 120) or "main"
        if not clean["branch"] or not clean["sha"]:
            raise OpError("bad_arg", "branch и sha обязательны")
        return clean

    if op == "setup_env":
        cmd = args.get("cmd")
        if not isinstance(cmd, list) or not cmd or len(cmd) > 20 \
                or not all(isinstance(x, str) and x.strip() for x in cmd):
            raise OpError("bad_arg", "cmd: непустой список строк")
        clean["cmd"] = cmd
        clean["timeout_s"] = _int(args.get("timeout_s", 180), 1, 600, "timeout_s")
        return clean

    if op == "archive_artifacts":
        clean["run_id"] = _str(args.get("run_id"), "run_id", 64)
        clean["sha"] = _str(args.get("sha"), "sha", 64)
        clean["branch"] = _str(args.get("branch"), "branch", 120)
        items = args.get("artifacts") or []
        if not clean["run_id"] or not isinstance(items, (list, type(None))) or len(items or []) > 50:
            raise OpError("bad_arg", "run_id обязателен, artifacts — до 50")
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise OpError("bad_arg", "artifacts[]: ожидался объект")
            path = _str(item.get("path"), "path", 400)
            if not path:
                raise OpError("bad_arg", "artifacts[].path: путь обязателен")
            rows.append({"path": path, "sha256": _str(item.get("sha256"), "sha256", 64)})
        clean["artifacts"] = rows
        return clean

    if op == "publish_branch":
        clean["branch"] = _str(args.get("branch"), "branch", 120)
        clean["remote"] = _str(args.get("remote"), "remote", 100) or "origin"
        if not clean["branch"]:
            raise OpError("bad_arg", "branch обязателен")
        return clean

    if op == "create_backup":
        clean["label"] = _str(args.get("label"), "label", 48) or "backup"
        clean["host"] = _str(args.get("host"), "host", 64) or "server"
        return clean

    if op == "write_reports":
        reports = args.get("reports")
        if not isinstance(reports, list) or not reports:
            raise OpError("bad_arg", "reports: нужен непустой список")
        if len(reports) > 40:
            raise OpError("bad_arg", "reports: не больше 40 файлов")
        items = []
        for item in reports:
            if not isinstance(item, dict):
                raise OpError("bad_arg", "reports[]: ожидался объект")
            path = _str(item.get("path"), "path", 400)
            if not path.startswith("ops/"):
                raise OpError("denied_path", f"{path}: отчёты пишутся только в ops/")
            reason = denied_reason(path)
            if reason:
                raise OpError("denied_path", f"{path}: {reason}")
            content = item.get("content")
            if not isinstance(content, str):
                raise OpError("bad_arg", "reports[].content: ожидалась строка")
            items.append({"path": path, "content": content})
        clean["reports"] = items
        return clean

    if op == "inspect":
        return clean

    if op == "init_project":
        name = _str(args.get("name"), "name", 200)
        if not name:
            raise OpError("bad_arg", "name обязателен")
        clean["name"] = name
        clean["branch"] = _str(args.get("branch"), "branch", 100) or "main"
        clean["repo_subdir"] = _str(args.get("repo_subdir"), "repo_subdir", 100) or "repo"
        clean["author_name"] = _str(args.get("author_name"), "author_name", 100) or "AI Hub"
        clean["author_email"] = _str(args.get("author_email"), "author_email", 200) or "aihub@local"
        clean["template_version"] = _str(args.get("template_version"), "template_version", 40) or "1"
        template = args.get("template_files") or {}
        if not isinstance(template, dict) or len(template) > 40:
            raise OpError("bad_arg", "template_files: объект не больше 40 файлов")
        files: dict[str, str] = {}
        total = 0
        for rel, content in template.items():
            if not isinstance(content, str):
                raise OpError("bad_arg", "template_files: содержимое должно быть строкой")
            total += len(content.encode("utf-8"))
            if total > 512 * 1024:
                raise OpError("bad_arg", "template_files: больше 512 КБ")
            files[_str(rel, "path", 400)] = content
        clean["template_files"] = files
        return clean

    if op == "archive_project":
        clean["label"] = _str(args.get("label"), "label", 64)
        return clean

    if op == "snapshot":
        clean["label"] = _str(args.get("label"), "label", 64) or "snapshot"
        return clean

    if op == "list_files":
        pattern = _str(args.get("glob"), "glob", 128) or "**/*"
        if pattern.startswith("/") or ".." in pattern:
            raise OpError("bad_arg", "glob вне корня")
        clean["glob"] = pattern
        clean["limit"] = _int(args.get("limit", 100), 1, MAX_LIST, "limit")
        clean["offset"] = _int(args.get("offset", 0), 0, 10 ** 6, "offset")
        return clean

    if op == "read_files":
        files = args.get("files")
        if not isinstance(files, list) or not files:
            raise OpError("bad_arg", "files: нужен непустой список")
        if len(files) > MAX_READ_FILES:
            raise OpError("bad_arg", f"files: не больше {MAX_READ_FILES}")
        items = []
        for item in files:
            if not isinstance(item, dict):
                raise OpError("bad_arg", "files[]: ожидался объект")
            rel = _str(item.get("path"), "path", 400)
            reason = denied_reason(rel)
            if reason:
                raise OpError("denied_path", f"{rel}: {reason}")
            items.append({
                "path": rel,
                "offset": _int(item.get("offset", 0), 0, 10 ** 8, "offset"),
                "length": _int(item.get("length", MAX_READ_BYTES), 1, MAX_READ_BYTES, "length"),
            })
        clean["files"] = items
        return clean

    if op == "apply_patch":
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            raise OpError("bad_arg", "changes: нужен непустой список")
        base = _str(args.get("base_tree_digest"), "base_tree_digest", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", base or ""):
            raise OpError("bad_arg", "apply_patch требует base_tree_digest из inspect")
        if len(changes) > MAX_PATCH_FILES:
            raise OpError("bad_arg", f"changes: не больше {MAX_PATCH_FILES}")
        total = 0
        items = []
        seen = set()
        for item in changes:
            if not isinstance(item, dict):
                raise OpError("bad_arg", "changes[]: ожидался объект")
            rel = _str(item.get("path"), "path", 400)
            if ckey(rel) in seen:
                raise OpError("bad_arg", f"{rel}: путь указан дважды")
            seen.add(ckey(rel))
            reason = denied_reason(rel)
            if reason:
                raise OpError("denied_path", f"{rel}: {reason}")
            preimage = item.get("preimage_sha256")
            if preimage is not None:
                preimage = _str(preimage, "preimage_sha256", 64).lower()
                if preimage and not re.fullmatch(r"[0-9a-f]{64}", preimage):
                    raise OpError("bad_arg", f"{rel}: preimage_sha256 не sha256")
            content = None
            if item.get("content_b64") is not None:
                raw = _str(item.get("content_b64"), "content_b64", 2 * MAX_PATCH_BYTES)
                try:
                    decoded = base64.b64decode(raw, validate=True)
                except (binascii.Error, ValueError):
                    raise OpError("bad_arg", f"{rel}: content_b64 не base64") from None
                total += len(decoded)
                content = raw
            if content is None and not preimage:
                raise OpError("bad_arg", f"{rel}: нужен content_b64 или preimage_sha256")
            items.append({"path": rel, "preimage_sha256": preimage or None,
                          "content_b64": content})
        if total > MAX_PATCH_BYTES:
            raise OpError("too_large", f"патч больше {MAX_PATCH_BYTES} байт")
        clean["base_tree_digest"] = base
        clean["changes"] = items
        return clean

    if op == "run_checks":
        clean["snapshot_id"] = _str(args.get("snapshot_id"), "snapshot_id", 120)
        if not clean["snapshot_id"]:
            raise OpError("bad_arg", "run_checks требует snapshot_id")
        checks = args.get("checks")
        if not isinstance(checks, list) or not checks:
            raise OpError("bad_arg", "checks: нужен непустой список имён")
        if len(checks) > MAX_CHECKS:
            raise OpError("bad_arg", f"checks: не больше {MAX_CHECKS}")
        clean["checks"] = [_str(name, "check", 64) for name in checks]
        return clean

    if op == "package":
        clean["snapshot_id"] = _str(args.get("snapshot_id"), "snapshot_id", 120)
        if not clean["snapshot_id"]:
            raise OpError("bad_arg", "package требует snapshot_id")
        clean["label"] = _str(args.get("label"), "label", 64) or "bundle"
        include = args.get("include")
        if include is not None:
            if not isinstance(include, list) or len(include) > 50:
                raise OpError("bad_arg", "include: список до 50 шаблонов")
            clean["include"] = [_str(x, "include", 128) for x in include]
        return clean

    raise OpError("unknown_op", f"неизвестная операция: {op}")


def allowed_roots(host_id: str, project_id: str | None = None, op: str = "") -> list[str]:
    """Разрешённые корни этого host: рабочие каталоги его проектов.

    archive_project дополнительно получает корень архива: папку проекта
    переносят внутрь <корень проектов>/Archive, и операция должна иметь
    право видеть и исходный корень, и место назначения.
    """
    roots = []
    for proj in db.list_projects():
        if proj.get("host_id") != host_id:
            continue
        if project_id and proj.get("id") != project_id:
            continue
        workdir = (proj.get("workdir") or "").strip()
        if not workdir:
            continue
        try:
            root = canon(workdir)
        except OpError:
            continue
        roots.append(root)
        if op == "archive_project":
            parent = os.path.dirname(root)
            if parent and parent != root:
                try:
                    roots.append(canon(os.path.join(parent, "Archive")))
                except OpError:
                    pass
    return roots


def registry_agents(host_id: str) -> dict:
    try:
        from .main import load_agents
        items = load_agents().get("agents", [])
    except Exception:  # noqa: BLE001
        items = []
    return {item.get("id"): item for item in items if item.get("host") == host_id}


CONFIG_FILE = os.environ.get("HUB_CONFIG", "/app/config/hub.yaml")
DEFAULT_PROJECTS_ROOT = r"R:\Projects"


def workspace_roots() -> list[str]:
    """Диски, где разрешено выбирать рабочую папку (решение владельца: R:)."""
    raw = (os.environ.get("HUB_WORKSPACE_ROOTS") or "").strip()
    parts: list[str] = []
    if raw:
        parts = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    else:
        try:
            import yaml
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            value = ((data.get("projects") or {}) if isinstance(data, dict) else {}).get("allowed_roots")
            if isinstance(value, str):
                parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
            elif isinstance(value, list):
                parts = [str(part).strip() for part in value if str(part).strip()]
        except (OSError, ValueError):
            parts = []
    return parts or [projects_root()]


def validate_workdir(path: str) -> str:
    """Рабочая папка проекта: абсолютный путь внутри разрешённого корня.

    Каталог выбирается человеком (диск + папка), но сервер не даёт выйти за
    разрешённые диски и не принимает относительные пути и «..».
    """
    value = (path or "").strip().strip('"')
    if not value:
        raise OpError("bad_arg", "рабочая папка не указана")
    if not ntpath.isabs(value):
        raise OpError("bad_arg", "рабочая папка должна быть абсолютным путём")
    parts = [part for part in value.replace("/", "\\").split("\\") if part]
    if any(part == ".." for part in parts):
        raise OpError("bad_arg", "рабочая папка не может содержать «..»")
    normalized = ntpath.normpath(value)
    for root in workspace_roots():
        base = ntpath.normpath(root)
        if normalized.lower() == base.lower() or normalized.lower().startswith(base.rstrip("\\").lower() + "\\"):
            return normalized
    raise OpError("bad_arg", "рабочая папка должна быть внутри: " + ", ".join(workspace_roots()))

def projects_root() -> str:
    """Корень рабочих папок новых проектов: переменная окружения, затем config/hub.yaml."""
    env = (os.environ.get("HUB_PROJECTS_ROOT") or "").strip()
    if env:
        return env
    try:
        import yaml
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        value = str(((data.get("projects") or {}) if isinstance(data, dict) else {}).get("root") or "").strip()
        if value:
            return value
    except (OSError, ValueError):
        pass
    return DEFAULT_PROJECTS_ROOT


def host_capabilities(host_id: str) -> dict:
    """Возможности живого воркера этого host из последнего heartbeat.

    На одном host может работать несколько воркеров: старый без проверок и
    новый с изолированным исполнителем. Возможности нельзя брать у первого
    попавшегося — иначе готовый исполнитель «не виден», и запуск отклоняется.
    Сначала выбирается воркер, подтвердивший изолированный запуск и run_checks,
    затем любой в состоянии ok.
    """
    registry = set(registry_agents(host_id))
    candidates = []
    for worker in workers.list_workers():
        served = set(worker.get("agents") or [])
        if served & registry:
            caps = dict(worker.get("capabilities") or {})
            caps["worker_id"] = worker.get("worker_id")
            caps["state"] = worker.get("state")
            candidates.append(caps)
    if not candidates:
        return {}
    for caps in candidates:
        if (caps.get("isolated_runner_protocol") == 1
                and "run_checks" in (caps.get("workspace_ops") or [])
                and caps.get("state") == "ok"):
            return caps
    for caps in candidates:
        if caps.get("state") == "ok":
            return caps
    return candidates[0]


def check_plan(project_id: str, names: list[str]) -> dict:
    """Утверждённые проверки проекта: команды берутся из настроек, не из запроса."""
    proj = db.get_project(project_id) if project_id else None
    if not proj:
        raise OpError("no_project", "run_checks требует проект")
    settings = proj.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            settings = {}
    plan = settings.get("checks") if isinstance(settings, dict) else None
    if not isinstance(plan, dict) or not plan:
        raise OpError("no_check_plan", "в настройках проекта нет checks")
    resolved = []
    for name in names:
        item = plan.get(name)
        if not isinstance(item, dict):
            raise OpError("unknown_check", f"проверка {name} не объявлена в настройках проекта")
        cmd = item.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise OpError("bad_check_plan", f"проверка {name}: cmd должен быть списком строк")
        resolved.append({"name": name, "cmd": cmd[:20],
                         "timeout_s": _int(item.get("timeout_s", 120), 1, 300, "timeout_s")})
    return {"checks": resolved}


def create_op_job(op: str, args: dict, host_id: str, project_id: str | None = None,
                  idempotency_key: str | None = None, timeout_s: int | None = None) -> dict:
    """Проверяет запрос и ставит control-задание. Возвращает задание и receipt."""
    clean = validate(op, args)
    host = _str(host_id, "host_id", 64)
    if not host:
        raise OpError("bad_host", "host_id обязателен")
    caps = host_capabilities(host)
    supported = caps.get("workspace_ops") or []
    # Единая deny-политика: воркер с устаревшим/подменённым списком исключений
    # не получает операций, пока не обновится до серверной политики.
    reported_policy = caps.get("security_policy")
    if reported_policy and reported_policy != security_policy.policy_digest():
        raise OpError("policy_mismatch",
                      "deny-политика воркера расходится с серверной; обновите host worker")
    if op == "apply_patch" and (caps.get("workspace_fencing") != 1
                                or caps.get("workspace_effect_journal") != 1):
        raise OpError("writer_unsupported", "workspace writer lacks fencing/effect journal support")
    if op == "run_checks" and op not in supported:
        raise OpError("runner_unavailable", "host has no configured isolated check runner")
    if op == "run_checks" and caps.get("isolated_runner_protocol") != 1:
        raise OpError("runner_unavailable", "host has no configured isolated check runner")
    if op not in supported:
        raise OpError("unsupported_op",
                      f"host {host} не объявил операцию {op} (capabilities: {supported or 'нет'})")
    roots = allowed_roots(host, project_id, op)
    if not roots:
        raise OpError("no_roots", f"у host {host} нет проектов с рабочим каталогом")
    if not any(is_within(root, clean["root"]) for root in roots):
        raise OpError("root_not_allowed", "корень вне разрешённых каталогов проектов этого host")
    if op == "run_checks":
        clean["plan"] = check_plan(project_id or "", clean["checks"])
    digest = _sha({"op": op, "root": ckey(clean["root"]), "args": clean,
                   "host_id": host, "project_id": project_id})
    # Idempotency key обязателен для модифицирующих операций. Для чтения ключ по
    # умолчанию не подставляется: иначе повторный read возвращал бы прежний
    # результат вместо текущего содержимого файла.
    key = (idempotency_key or "").strip()
    if not key and op in MUTATING:
        # Патч и упаковка идемпотентны по содержимому. Снимок — новая ревизия:
        # ключ уникален в пределах секунды, чтобы повтор запроса не плодил копии,
        # но новый запуск видел актуальное дерево, а не старый снимок.
        if op in {"snapshot", "refresh_index"}:
            # Снимок — новая ревизия, карта — новое состояние коммита: ключ
            # уникален в пределах секунды, иначе повторный вызов возвращал бы
            # результат, посчитанный до последнего коммита.
            key = f"ws:{host}:{op}:{digest[:16]}:{int(time.time())}"
        else:
            key = f"ws:{host}:{op}:{digest[:32]}"
    envelope = {"kind": "workspace", "op": op, "host_id": host, "digest": digest,
                "args": clean}
    snapshot = {
        "host_id": host,
        "project_host_id": host,
        "project_id": project_id,
        "project_workdir": clean["root"],
        "workdir": clean["root"],
        "workspace_op": op,
        "workspace_digest": digest,
        "op": op,
        "args": clean,
    }
    try:
        job, created = db.create_job(
            "workspace", json.dumps(envelope, ensure_ascii=False), digest,
            int(timeout_s or (1800 if op == "run_checks" else 300)),
            key or None, workspace=snapshot, kind="workspace")
    except ValueError as exc:
        if str(exc) == "idempotency_key_conflict":
            raise OpError("idempotency_conflict", "idempotency key already belongs to another target") from None
        raise
    if created and key:
        db.record_workspace_receipt(key, op, host, job["id"], digest, "pending")
    return {"job": job, "created": created, "receipt": db.workspace_receipt(key),
            "op": op, "digest": digest, "idempotency_key": key,
            "root": clean["root"], "host_id": host}


def job_result(job_id: str) -> dict | None:
    job = db.get_job(job_id)
    if not job:
        return None
    output = job.get("result_output")
    parsed = None
    if output:
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = {"raw": output[:4000]}
    return {
        "id": job["id"],
        "kind": job.get("kind"),
        "agent": job.get("agent_id"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "worker": job.get("worker"),
        "created_at": str(job.get("created_at") or ""),
        "finished_at": str(job.get("finished_at") or ""),
        "error": job.get("result_error"),
        "result": parsed,
        "receipt": db.workspace_receipt(job.get("idempotency_key") or ""),
    }
