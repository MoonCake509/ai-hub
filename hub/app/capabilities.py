"""ADAPTER-01: проверяемая матрица возможностей агента/профиля/модели.

Каталог — это заявка, а не доказательство. Возможность считается доступной
только если она явно объявлена (true) с указанием источника: каталог,
отдельный fixture или проба. Всё, что не объявлено, считается недоступным
с причиной, а не «наверное работает»: workflow не должен уметь незаметно
выбрать неподдержанный режим.
"""
from __future__ import annotations

CAPABILITIES = ("text", "patch", "workdir", "commands", "usage", "streaming",
                "stop", "output_control")

# Человеческие названия для API и интерфейса.
LABELS = {
    "text": "текстовый ответ",
    "patch": "правка файлов",
    "workdir": "работа в каталоге проекта",
    "commands": "запуск команд",
    "usage": "учёт токенов",
    "streaming": "потоковый вывод",
    "stop": "подтверждённая остановка",
    "output_control": "ограничение вывода",
}

DEFAULT_REASON = {
    "text": "канал не подтвердил текстовый ответ",
    "patch": "канал не подтвердил правку файлов",
    "workdir": "канал не подтвердил работу в каталоге проекта",
    "commands": "канал не подтвердил запуск команд",
    "usage": "канал не отдаёт usage",
    "streaming": "потоковый вывод не подтверждён",
    "stop": "подтверждённая остановка не реализована",
    "output_control": "ограничение вывода не подтверждено",
}


class CapabilityError(ValueError):
    """Отказ preflight: режим недоступен или запрос некорректен."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def _node(caps: dict | None) -> dict:
    """Нормализует объявление возможностей узла каталога."""
    out: dict[str, dict] = {}
    for name, value in (caps or {}).items():
        if name not in CAPABILITIES:
            continue
        if isinstance(value, dict):
            out[name] = {
                "supported": bool(value.get("supported")),
                "reason": str(value.get("reason") or ""),
                "evidence": str(value.get("evidence") or "catalog"),
            }
        else:
            out[name] = {"supported": bool(value), "reason": "", "evidence": "catalog"}
    return out


def _find(agent: dict, profile: str | None) -> dict | None:
    for item in agent.get("profiles") or []:
        if item.get("id") == profile:
            return item
    return None


def resolve(agent: dict, profile: str | None = None, model: str | None = None) -> dict:
    """Собирает матрицу: уровень агента < профиля < модели.

    Возвращает и «заявленное», и итог с причиной для каждой возможности,
    чтобы интерфейс мог показать, почему режим выключен.
    """
    agent_id = agent.get("id")
    profiles = agent.get("profiles") or []
    chosen_profile = profile or agent.get("default_profile") or (profiles[0].get("id") if profiles else None)
    spec = _find(agent, chosen_profile) if chosen_profile else None

    if profile and spec is None:
        raise CapabilityError("unknown_profile",
                              f"неизвестный профиль '{profile}' для агента '{agent_id}'",
                              {"agent_id": agent_id, "profile": profile})

    models = list((spec or {}).get("models") or [])
    if model and model not in models:
        raise CapabilityError("unknown_model",
                              f"модель '{model}' не заявлена в профиле '{chosen_profile}' агента '{agent_id}'",
                              {"agent_id": agent_id, "profile": chosen_profile, "model": model,
                               "supported_models": models})
    # Запрошенная модель и модель по умолчанию — разные вещи. Если выбор не
    # запрошен, мы не имеем права утверждать, какая модель ответила: канал может
    # не раскрывать её вовсе. Поэтому model остаётся пустым, а default_model
    # показывает, что канал применит сам.
    chosen_model = model
    default_model = models[0] if models else None

    merged: dict[str, dict] = {}
    for node in (agent, spec or {}):
        merged.update(_node(node.get("capabilities")))
    model_caps = (((spec or {}).get("model_capabilities") or {}).get(chosen_model) or {}) if chosen_model else {}
    merged.update(_node(model_caps.get("capabilities")))

    # Возможность без явного объявления — недоступна, но не молча.
    capabilities = {}
    for name in CAPABILITIES:
        item = merged.get(name)
        if item is None:
            capabilities[name] = {"supported": False, "reason": DEFAULT_REASON[name],
                                  "evidence": "unknown", "label": LABELS[name]}
            continue
        reason = item["reason"] or ("" if item["supported"] else DEFAULT_REASON[name])
        capabilities[name] = {"supported": item["supported"], "reason": reason,
                              "evidence": item["evidence"], "label": LABELS[name]}

    modalities = {
        "input": list((model_caps.get("input_modalities")
                       or (spec or {}).get("input_modalities")
                       or agent.get("input_modalities") or ["text"])),
        "output": list((model_caps.get("output_modalities")
                        or (spec or {}).get("output_modalities")
                        or agent.get("output_modalities") or ["text"])),
    }
    required = list((model_caps.get("required_modalities")
                     or (spec or {}).get("required_modalities")
                     or agent.get("required_modalities") or []))

    return {
        "agent_id": agent_id,
        "profile": chosen_profile,
        "model": chosen_model,
        "default_model": default_model,
        "capabilities": capabilities,
        "supported": [name for name, item in capabilities.items() if item["supported"]],
        "unsupported": [name for name, item in capabilities.items() if not item["supported"]],
        "modalities": modalities,
        "required_modalities": required,
        "filesystem_access": bool(model_caps.get("filesystem_access",
                                  (spec or {}).get("filesystem_access",
                                                   agent.get("filesystem_access", False)))),
        "context_mode": (model_caps.get("context_mode") or (spec or {}).get("context_mode")
                         or agent.get("context_mode") or ""),
        "model_selection": bool(agent.get("model_selection")),
    }


def preflight(agent: dict, profile: str | None, model: str | None,
              needs: list[str] | None) -> dict:
    """Проверяет запрошенный режим до постановки задания в очередь."""
    wanted = [str(x).strip() for x in (needs or []) if str(x).strip()]
    unknown = [name for name in wanted if name not in CAPABILITIES]
    if unknown:
        raise CapabilityError("unknown_capability",
                              f"неизвестные требования: {', '.join(unknown)}",
                              {"unknown": unknown, "supported": list(CAPABILITIES)})

    matrix = resolve(agent, profile, model)
    missing = [name for name in wanted if not matrix["capabilities"][name]["supported"]]
    if missing:
        details = {name: matrix["capabilities"][name]["reason"] for name in missing}
        summary = "; ".join(f"{name}: {details[name]}" for name in missing)
        raise CapabilityError("capability_unsupported",
                              f"режим недоступен для {matrix['agent_id']}"
                              f"/{matrix['profile']}/{matrix['model']}: {summary}",
                              {"missing": missing, "reasons": details})
    return matrix


def describe(agent: dict) -> dict:
    """Матрица по всем профилям и моделям агента — для интерфейса и preflight."""
    profiles = []
    for spec in agent.get("profiles") or []:
        models = list(spec.get("models") or [])
        entries = []
        for model in models or [None]:
            entry = resolve(agent, spec.get("id"), model)
            entries.append({"model": entry["model"],
                            "capabilities": entry["capabilities"],
                            "supported": entry["supported"],
                            "unsupported": entry["unsupported"]})
        profiles.append({
            "id": spec.get("id"),
            "name": spec.get("name") or spec.get("id"),
            "native_provider": spec.get("native_provider"),
            "default_model": entries[0]["model"] if entries else None,
            "models": entries,
        })
    return {
        "agent_id": agent.get("id"),
        "name": agent.get("name"),
        "host": agent.get("host"),
        "transport": agent.get("transport"),
        "model_selection": bool(agent.get("model_selection")),
        "model_selection_reason": agent.get("model_selection_reason"),
        "default_profile": agent.get("default_profile"),
        "profiles": profiles,
    }