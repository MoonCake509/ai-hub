"""FLOW-P1: программные ворота процесса — проверки, которые модель не может подделать.

Три вороты между шагами пяти ролей:
  * gate_candidate  — кодер закончил, хаб сам снял digest дерева кандидата;
  * gate_checks     — тестировщик не объявляет тесты пройденными: хаб сам запускает
                      утверждённые проверки проекта в изолированном runner и принимает
                      только receipt с ненулевым набором тестов, exit code 0 у каждого
                      и snapshot_id того же кандидата;
  * gate_review     — ревьюер обязан сослаться на digest того же кандидата, по которому
                      есть принятый receipt проверок.

Receipt приходит от host worker (op run_checks): хост уже сверил изоляцию runner'а,
неизменность снимка и завершение процессов. Здесь — ворота хаба поверх него.
"""
from __future__ import annotations

# Поля изоляции, которые host worker обязан подтвердить перед тем, как вернуть
# receipt; хаб не доверяет receipt без них (host мог ответить произвольным JSON).
REQUIRED_ISOLATION = {
    "network": "none",
    "rootfs_read_only": True,
    "cap_drop_all": True,
    "no_new_privileges": True,
    "resource_limits": True,
    "snapshot_read_only": True,
    "nonroot": True,
    "ephemeral_workspace": True,
}


class GateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def validate_check_receipt(receipt: dict | None, *, snapshot_id: str,
                           isolation: dict | None = None) -> dict:
    """Принимает receipt настоящего runner'а или бросает GateError.

    Модель не участвует: receipt рождается из управляемой операции run_checks.
    """
    if not isinstance(receipt, dict):
        raise GateError("no_receipt", "нет receipt изолированного runner'а")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise GateError("no_candidate", "нет snapshot_id кандидата для сверки")
    if receipt.get("snapshot_id") != snapshot_id:
        raise GateError("candidate_mismatch",
                        f"проверки выполнены над другим снимком: {receipt.get('snapshot_id')!r}")
    checks = receipt.get("checks")
    if not isinstance(checks, list):
        raise GateError("bad_receipt", "receipt: checks должен быть списком")
    if not checks:
        raise GateError("no_checks", "receipt без единой проверки не принимается")
    for item in checks:
        if not isinstance(item, dict):
            raise GateError("bad_receipt", "receipt: проверка должна быть объектом")
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise GateError("bad_receipt", "receipt: у проверки нет имени")
        code = item.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise GateError("bad_receipt", f"receipt: {item.get('name')}: exit_code не целое")
        if code != 0 or item.get("passed") is not True:
            raise GateError("check_failed",
                            f"проверка {item.get('name')} не пройдена (exit code {code})")
        cmd = item.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise GateError("bad_receipt", f"receipt: {item.get('name')}: cmd должен быть списком строк")
    # Изоляция подтверждена host'ом в ответе runner'а; для старых receipt без
    # блока isolation требуем внешнее подтверждение (передаётся аргументом).
    confirmed = isolation if isolation is not None else receipt.get("isolation")
    if not isinstance(confirmed, dict):
        raise GateError("isolation_unverified", "изоляция runner'а не подтверждена")
    for key, want in REQUIRED_ISOLATION.items():
        if confirmed.get(key) != want:
            raise GateError("isolation_unverified",
                            f"изоляция runner'а: {key} != {want!r}")
    return receipt


def gate_candidate(run: dict) -> str:
    """Digest кандидата запуска; без него шаги test/review не выдаются."""
    digest = (run or {}).get("candidate_digest")
    if not digest:
        raise GateError("no_candidate", "кандидат ещё не зафиксирован снимком")
    return digest


def _norm_digest(value) -> str:
    """Digest в ответе агента: допускаем префикс алгоритма и регистр.

    Сравнивать `sha256:<hex>` с `<hex>` как разные значения — значит отбраковывать
    верный отчёт тестировщика только за форму записи.
    """
    text = str(value or "").strip().lower()
    return text[7:] if text.startswith("sha256:") else text


def gate_tester_output(parsed: dict, *, candidate_digest: str) -> None:
    """Ворота тестировщика: его JSON обязан ссылаться на тот же кандидат."""
    if not isinstance(parsed, dict):
        raise GateError("bad_output", "выход тестировщика не является объектом")
    claimed = parsed.get("candidate_digest")
    if _norm_digest(claimed) != _norm_digest(candidate_digest):
        raise GateError("candidate_mismatch",
                        f"тестировщик ссылается на кандидата {claimed!r}, ожидался {candidate_digest!r}")


def gate_reviewer_output(parsed: dict, *, candidate_digest: str) -> None:
    """Ворота ревьюера: ревью без того же кандидата и принятых проверок не считается."""
    if not isinstance(parsed, dict):
        raise GateError("bad_output", "выход ревьюера не является объектом")
    claimed = parsed.get("candidate_digest")
    if _norm_digest(claimed) != _norm_digest(candidate_digest):
        raise GateError("candidate_mismatch",
                        f"ревью относится к кандидату {claimed!r}, ожидался {candidate_digest!r}")
    if parsed.get("verdict") != "approve":
        raise GateError("changes_requested", "ревью просит изменения")
