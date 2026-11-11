"""CTX-02: сборщик контекста под задачу и краткая память.

Задача на один модуль не должна тянуть весь проект: собирается минимальный
набор файлов (точка входа, её прямые импорты, совпадения по цели), а
обязательные контракты входят целиком. Если контракт не влезает в бюджет —
это ошибка, а не тихое обрезание.
"""
from __future__ import annotations

import hashlib
import json
import re

TOKENS_PER_CHAR = 0.25  # оценка: ~4 символа на токен
CONTRACT_HINTS = ("openapi", "schema.sql", "contract", ".proto", "types.ts",
                  "api.yaml", "api.json", "migrations/")
MAX_IMPORT_DEPTH = 1

# Версия формата digest: смена контракта меняет все digest, старые значения
# нельзя сравнивать с новыми. Digest включает SHA-256 каждого фактически
# передаваемого файла: замена содержимого без смены размера меняет digest.
FORMAT_VERSION = 2


def content_digest(items: list[dict]) -> str:
    """Канонический digest выбранных файлов: путь|размер|sha256 + версия формата."""
    lines = [f"aihub-context-v{FORMAT_VERSION}"]
    for item in sorted(items, key=lambda x: x["path"]):
        lines.append(f"{item['path']}|{int(item['size'])}|{item.get('sha256') or '-'}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class ContextError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def estimate_tokens(value) -> int:
    """Оценка токенов по размеру в символах (для текста) или байтах (для файла)."""
    if isinstance(value, str):
        chars = len(value)
    else:
        chars = int(value or 0)
    return max(1, int(round(chars * TOKENS_PER_CHAR)))


FROM_IMPORT = re.compile(r"^\s*from\s+([\.\w]+)\s+import\s+(.+)$", re.M)
SIMPLE_IMPORT = re.compile(r"^\s*import\s+([\w\.]+)", re.M)
JS_IMPORT = re.compile(r"""(?:from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\))""")


def parse_imports(path: str, text: str) -> list[str]:
    """Модули, которые файл подключает. Для `from . import x` даёт `.x`."""
    found: list[str] = []
    body = text or ""
    if path.endswith(".py"):
        for module, names in FROM_IMPORT.findall(body):
            for name in re.split(r"\s*,\s*", names.strip().strip("()")):
                name = name.split(" as ")[0].strip()
                if not name or name == "*":
                    continue
                found.append(f".{name}" if module == "." else (f"{module}.{name}" if module else name))
        for module in SIMPLE_IMPORT.findall(body):
            found.append(module)
    elif path.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")):
        for a, b in JS_IMPORT.findall(body):
            found.append(a or b)
    return [x for x in found if x]


def resolve_module(module: str, from_path: str, paths: list[str]) -> str | None:
    """Файл проекта для импорта: относительный — от каталога файла, иначе по хвосту имени."""
    if not module:
        return None
    available = {p.replace("\\", "/"): p for p in paths}
    if module.startswith("./"):
        module = module[1:]
    if module.startswith("."):
        depth = len(module) - len(module.lstrip("."))
        rest = module[depth:].replace(".", "/")
        base = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
        parts = [x for x in base.split("/") if x][:len(base.split("/")) - max(0, depth - 1) or None]
        stem = "/".join([x for x in parts + ([rest] if rest else []) if x])
        for suffix in (".py", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts"):
            candidate = stem + suffix
            if candidate in available:
                return available[candidate]
        return None
    tail = module.replace(".", "/")
    for candidate, original in available.items():
        if candidate.endswith(tail + ".py") or candidate.endswith(tail + ".js")                 or candidate.endswith(tail + ".ts") or candidate.endswith(tail):
            return original
        if candidate.rsplit("/", 1)[-1].split(".")[0] == module.split(".")[-1]:
            return original
    return None


def is_contract(path: str) -> bool:
    low = path.lower()
    return any(hint in low for hint in CONTRACT_HINTS)

def _score(path: str, goal_words: list[str], entry: str, imports: set[str]) -> int:
    score = 0
    if path == entry:
        score += 100
    if path in imports:
        score += 50
    low = path.lower()
    for word in goal_words:
        if word and word in low:
            score += 10
    if is_contract(path):
        score += 5
    return score


def build(entries: list[dict], contents: dict[str, str], goal: str, entry: str,
          contracts: list[str], budget_tokens: int) -> dict:
    """Собирает вход под бюджет. Контракты обязательны и никогда не обрезаются."""
    if budget_tokens <= 0:
        raise ContextError("bad_budget", "бюджет должен быть положительным")
    by_path = {e["path"]: e for e in entries}
    if entry and entry not in by_path:
        raise ContextError("unknown_entry", f"точка входа не найдена в проекте: {entry}")

    required = [c for c in contracts if c]
    missing = [c for c in required if c not in by_path]
    if missing:
        raise ContextError("missing_contract", f"контракты не найдены: {', '.join(missing)}",
                           {"missing": missing})

    sizes = {path: int(e.get("size") or 0) for path, e in by_path.items()}
    # SHA обязателен для выбранных файлов: digest пакета защищает содержимое,
    # а не метаданные. Если хост не прислал sha, считаем из переданного
    # содержимого; выбранный файл без sha и без содержимого — дыра в сверке.
    hashes: dict[str, str | None] = {}
    for path, e in by_path.items():
        sha = e.get("sha256") or None
        if not sha:
            text = (contents or {}).get(path)
            if text is not None:
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        hashes[path] = sha
    tokens = {path: estimate_tokens(sizes[path]) for path in by_path}

    required_tokens = sum(tokens[c] for c in required)
    if required_tokens > budget_tokens:
        raise ContextError(
            "contract_exceeds_budget",
            f"обязательные контракты требуют {required_tokens} токенов при бюджете {budget_tokens}",
            {"required_tokens": required_tokens, "budget_tokens": budget_tokens,
             "contracts": required})

    imports: set[str] = set()
    for path, text in (contents or {}).items():
        for module in parse_imports(path, text):
            resolved = resolve_module(module, path, list(by_path))
            if resolved:
                imports.add(resolved)

    goal_words = [w for w in re.findall(r"[\w\-]{4,}", (goal or "").lower())]
    ranked = sorted(by_path, key=lambda p: (-_score(p, goal_words, entry, imports), tokens[p], p))

    items = []
    used = required_tokens
    chosen = set(required)
    for path in ranked:
        if path in chosen:
            items.append({"path": path, "size": sizes[path], "sha256": hashes[path], "tokens": tokens[path],
                          "selected": True, "reason": "обязательный контракт" if path in required
                          else ("точка входа" if path == entry else
                                ("прямой импорт" if path in imports else "по цели задачи"))})
            continue
        if used + tokens[path] > budget_tokens:
            items.append({"path": path, "size": sizes[path], "sha256": hashes[path], "tokens": tokens[path],
                          "selected": False,
                          "reason": f"не влезло в бюджет {budget_tokens} токенов"})
            continue
        used += tokens[path]
        chosen.add(path)
        reason = "точка входа" if path == entry else ("прямой импорт" if path in imports else "по цели задачи")
        items.append({"path": path, "size": sizes[path], "sha256": hashes[path], "tokens": tokens[path],
                      "selected": True, "reason": reason})

    total_tokens = sum(tokens.values()) or 1
    selected = [i for i in items if i["selected"]]
    for item in selected:
        if not item.get("sha256"):
            raise ContextError("no_hash", f"нет sha256 для файла {item['path']}",
                               {"path": item["path"]})
    digest = content_digest(selected)
    return {
        "items": items,
        "selected": [i["path"] for i in selected],
        "used_tokens": used,
        "budget_tokens": budget_tokens,
        "total_project_tokens": sum(tokens.values()),
        "saved_tokens": max(0, sum(tokens.values()) - used),
        "saved_ratio": round(1 - (used / total_tokens), 3) if total_tokens else 0.0,
        "digest": digest,
        "format_version": FORMAT_VERSION,
        "contracts": required,
        "imports": sorted(imports),
    }
