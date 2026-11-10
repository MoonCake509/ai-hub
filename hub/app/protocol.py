"""CORE-01 — версия протокола моста, аренда и интервал подтверждения.

Один источник правды для хаба и воркеров: какие версии протокола хаб
обслуживает, какой минимальный протокол у задания, как часто исполнитель
обязан подтверждать аренду при её заданной длительности.

Исходная проблема (до CORE-01): `GET /bridge/jobs` отдавал задание любому
клиенту без версии, поэтому старый poller v1 (без outbox-ack и без heartbeat
контракта) мог арендовать задание v2 и «потерять» его при падении. Вторая
проблема: воркер считал интервал heartbeat как `max(20, lease // 3)`, и при
короткой тестовой аренде (3 с) первое подтверждение приходило на 20-й секунде —
то есть уже после истечения аренды при живом исполнителе.
"""
from __future__ import annotations

PROTOCOL = 2                 # текущая версия протокола моста
SUPPORTED = (2,)             # версии, которые принимает хаб
JOB_PROTOCOL = 2             # минимальный протокол для новых заданий
MAX_HEARTBEAT_S = 60         # верхняя граница интервала подтверждения


def version_of(value) -> int | None:
    """Разобрать версию клиента: положительное целое или строка с числом."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def client_problem(value) -> str | None:
    """Причина отказа клиенту или None, если версия поддерживается.

    Отсутствие версии трактуется как legacy-клиент v1: он не умеет ни
    durable-ack, ни heartbeat-контракт, поэтому арендовать задания v2 ему
    нельзя. Отказ явный (HTTP 426), а не тихое «заданий нет»: иначе старый
    воркер бесконечно опрашивал бы хаб и выглядел бы живым.
    """
    supported = ", ".join(str(item) for item in SUPPORTED)
    version = version_of(value)
    if version is None:
        return "protocol is required; supported: " + supported
    if version not in SUPPORTED:
        return "protocol " + str(version) + " is not supported; supported: " + supported
    return None


def heartbeat_interval(lease_seconds: int) -> int:
    """Интервал подтверждения аренды: треть аренды, но не больше минуты.

    При коротком тестовом lease (3 с) интервал равен 1 с, поэтому первый
    heartbeat приходит до истечения аренды, пока исполнитель жив.
    """
    lease = max(1, int(lease_seconds or 0))
    return max(1, min(MAX_HEARTBEAT_S, lease // 3))


def lease_risk(interval_s, lease_seconds: int) -> str | None:
    """Проверка интервала heartbeat относительно lease: причина риска или None.

    Воркер сообщает, как часто он собирается подтверждать аренду. Если этот
    интервал не короче самой аренды, попытка гарантированно станет unknown
    между двумя подтверждениями; если он больше половины аренды, то достаточно
    одного пропуска. Оба случая — не ошибка исполнения, а неверная настройка,
    и она должна быть видна в диагностике, а не выглядеть как потеря связи.
    """
    lease = max(1, int(lease_seconds or 0))
    if interval_s is None:
        return "интервал подтверждения аренды не сообщён"
    try:
        interval = int(interval_s)
    except (TypeError, ValueError):
        return "интервал подтверждения аренды не число"
    if interval <= 0:
        return "интервал подтверждения аренды должен быть положительным"
    if interval >= lease:
        return ("интервал подтверждения " + str(interval) + " с не короче аренды "
                + str(lease) + " с: попытка истечёт между подтверждениями")
    if interval * 2 >= lease:
        return ("интервал подтверждения " + str(interval) + " с больше половины аренды "
                + str(lease) + " с: один пропуск истекает аренду")
    return None
