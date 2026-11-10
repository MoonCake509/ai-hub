"""DIAG-01: модель наблюдений — юнит-проверки чистой логики.

Запуск внутри образа хаба (там есть зависимости и код приложения):

    docker cp tests/test_diag01_observations.py aihub-hub-test:/tmp/t.py
    docker exec aihub-hub-test python /tmp/t.py

БД не требуется: проверяются вычисление состояния на чтение, сводка и
фильтрация секретов. Живой API проверяет tests/test_diag01_api.py.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")

from app import observations as obs  # noqa: E402

NOW = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc)


class ResolveState(unittest.TestCase):
    def test_fresh_keeps_state(self):
        self.assertEqual(obs.resolve_state("ok", NOW - timedelta(seconds=30), 180, NOW), "ok")

    def test_expired_ttl_becomes_stale(self):
        self.assertEqual(obs.resolve_state("ok", NOW - timedelta(seconds=181), 180, NOW), "stale")

    def test_boundary_is_not_stale(self):
        self.assertEqual(obs.resolve_state("ok", NOW - timedelta(seconds=180), 180, NOW), "ok")

    def test_missing_observation_is_unknown(self):
        self.assertEqual(obs.resolve_state(None, None, 180, NOW), "unknown")

    def test_no_ttl_never_expires(self):
        self.assertEqual(obs.resolve_state("ok", NOW - timedelta(days=400), None, NOW), "ok")


class Summarize(unittest.TestCase):
    def _set(self, **states):
        base = {kind: {"state": "ok", "reason": None} for kind in obs.KINDS}
        for kind, state in states.items():
            base[kind] = {"state": state, "reason": kind + " причина"}
        return base

    def test_all_ok(self):
        self.assertEqual(obs.summarize(self._set())[0], "ok")

    def test_unknown_availability_is_unknown(self):
        state, reason = obs.summarize(self._set(availability="unknown"))
        self.assertEqual(state, "unknown")
        self.assertIn("доступности", reason)

    def test_stale_availability_degrades(self):
        self.assertEqual(obs.summarize(self._set(availability="stale"))[0], "degraded")

    def test_failed_configured_wins(self):
        self.assertEqual(obs.summarize(self._set(configured="failed"))[0], "failed")

    def test_unknown_quality_does_not_degrade(self):
        self.assertEqual(obs.summarize(self._set(quality="unknown"))[0], "ok")


class ObservationShape(unittest.TestCase):
    def test_reason_required_outside_ok(self):
        item = obs.observation("availability", "unknown", None, 180, "нет наблюдений")
        self.assertTrue(item["reason"])

    def test_secrets_filtered(self):
        item = obs.observation("availability", "ok", NOW, 180, "heartbeat",
                               detail={"version": "0.3.0", "api_token": "secret", "nested": {"password": "x", "ok": 1}})
        self.assertNotIn("api_token", item["detail"])
        self.assertEqual(item["detail"]["nested"], {"ok": 1})

    def test_age_and_iso(self):
        item = obs.observation("quality", "ok", NOW - timedelta(minutes=5), 86400, "runs")
        self.assertEqual(item["checked_at"], "2026-11-10T01:55:00+00:00")


class Cards(unittest.TestCase):
    GOOD = {"id": "a", "name": "A", "transport": "cli", "host": "laptop",
            "model_selection": True, "profiles": [{"id": "p", "models": ["m"]}], "configured": True}

    def test_good_card_configured(self):
        ok, reason, detail = obs.configured_facts(self.GOOD)
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(detail["models"], 1)

    def test_missing_field_fails(self):
        ok, reason, _ = obs.configured_facts({**self.GOOD, "transport": ""})
        self.assertFalse(ok)
        self.assertIn("transport", reason)

    def test_legacy_status_verified_is_configured(self):
        card = {k: v for k, v in self.GOOD.items() if k != "configured"}
        card["status"] = "verified"
        self.assertTrue(obs.configured_facts(card)[0])

    def test_model_selection_without_models_degrades(self):
        card = {**self.GOOD, "profiles": [{"id": "p", "models": []}]}
        state, reason, _ = obs.compatibility_facts(card, "0.3.0")
        self.assertEqual(state, "degraded")
        self.assertIn("моделей", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
