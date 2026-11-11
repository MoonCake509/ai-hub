"""Каноническая deny-политика путей — единый источник для хаба и host worker.

Хаб (hub/app/*) и исполнитель на устройстве (agents/laptop/workspace_host.py)
обязаны отклонять одни и те же пути: секреты, служебные каталоги и дампы не
покидают хост и не попадают в пакеты/снимки ни при каких условиях. Любая правка
этого модуля меняет поведение обеих сторон; расхождение ловится юнит-тестом
паритета (tests/test_security_policy.py).
"""
from __future__ import annotations

import fnmatch
import re

# Имена файлов, которые никогда не читаются и не пакуются.
SECRET_NAMES = {
    "token.txt", "credentials", ".netrc", "id_rsa", "id_ed25519", "auth.json",
}
# Суффиксы секретов и дампов.
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".dump", ".bak", ".kdbx")
# Шаблоны fnmatch: секреты с учётом подкаталогов и маски.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12",
    "id_rsa*", "id_ed25519*",
    "*credentials*", "*secret*",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "*.kdbx", "*.ovpn", "auth.json", "cookies*.txt",
)
# Каталоги, полностью исключённые из выборки, чтения и снимков.
DENY_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", "dist", "build", ".next", ".idea",
    ".mypy_cache", ".pytest_cache", "target", "vendor",
    "outbox", "models", ".ssh", "appdata",
}


def denied_reason(rel: str) -> str | None:
    """Причина отказа для относительного пути проекта, None если путь разрешён."""
    parts = [p.lower() for p in re.split(r"[\\/]+", rel or "") if p]
    if not parts:
        return "пустой путь"
    for part in parts[:-1]:
        if part in DENY_DIRS:
            return f"служебный каталог {part}"
    name = parts[-1]
    if name == ".env" or name.startswith(".env."):
        return "файл окружения"
    if name in SECRET_NAMES or name.startswith("id_rsa"):
        return "секрет"
    if name.endswith(SECRET_SUFFIXES):
        return "секрет/дамп"
    low_rel = rel.lower()
    for pattern in SECRET_PATTERNS:
        if fnmatch.fnmatch(name, pattern.lower()):
            return "секрет (политика путей)"
        if "*" in pattern and fnmatch.fnmatch(low_rel, pattern.lower()):
            return "секрет (политика путей)"
    return None


def is_secret(rel: str) -> bool:
    """True, если путь по политике не должен покидать хост (секреты, не каталоги).

    Каталоги обрабатываются отдельно (in_skipped_dir / denied_reason): выборка
    пакетов считает их своей статистикой, а чтение отказывает по denied_reason.
    """
    name = rel.rsplit("/", 1)[-1]
    low_rel, low_name = rel.lower(), name.lower()
    if low_name == ".env" or low_name.startswith(".env."):
        return True
    if low_name in SECRET_NAMES or low_name.startswith("id_rsa"):
        return True
    if low_name.endswith(SECRET_SUFFIXES):
        return True
    for pattern in SECRET_PATTERNS:
        if fnmatch.fnmatch(low_name, pattern.lower()):
            return True
        if "*" in pattern and fnmatch.fnmatch(low_rel, pattern.lower()):
            return True
    return False


def in_skipped_dir(rel: str) -> bool:
    return any(part in DENY_DIRS for part in rel.split("/")[:-1])


def policy_digest() -> dict:
    """Снимок политики для capabilities: хаб сверяет host и наоборот."""
    return {
        "deny_dirs": sorted(DENY_DIRS),
        "secret_names": sorted(SECRET_NAMES),
        "secret_suffixes": sorted(SECRET_SUFFIXES),
        "secret_patterns": sorted(SECRET_PATTERNS),
    }
