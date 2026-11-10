"""CORE-01: версия протокола моста и интервал подтверждения аренды.

Без БД и без сети: проверяется решение «пускать ли клиента к аренде» и
арифметика интервала heartbeat относительно длительности аренды.
"""
import unittest

from app import protocol


class VersionGate(unittest.TestCase):
    def test_supported_version_passes(self):
        self.assertIsNone(protocol.client_problem("2"))
        self.assertIsNone(protocol.client_problem(2))
        self.assertIsNone(protocol.client_problem(" 2 "))

    def test_missing_version_is_legacy_and_refused(self):
        for value in (None, "", "   "):
            self.assertIsNotNone(protocol.client_problem(value), value)

    def test_old_and_garbage_versions_are_refused(self):
        for value in ("1", 1, "0", "3", "abc", True, -2, 2.0, []):
            self.assertIsNotNone(protocol.client_problem(value), repr(value))

    def test_version_parsing_does_not_accept_booleans(self):
        self.assertEqual(protocol.version_of(" 2 "), 2)
        self.assertIsNone(protocol.version_of(True))
        self.assertIsNone(protocol.version_of(False))
        self.assertIsNone(protocol.version_of(""))


class HeartbeatInterval(unittest.TestCase):
    def test_interval_is_a_third_of_the_lease(self):
        self.assertEqual(protocol.heartbeat_interval(300), 60)
        self.assertEqual(protocol.heartbeat_interval(90), 30)
        self.assertEqual(protocol.heartbeat_interval(60), 20)

    def test_short_test_lease_gets_interval_before_expiry(self):
        # Приемка CORE-01: короткая тестовая аренда не истекает раньше первого
        # корректного heartbeat при живом исполнителе.
        for lease in (1, 2, 3, 4, 5, 9):
            interval = protocol.heartbeat_interval(lease)
            self.assertGreaterEqual(interval, 1, lease)
            self.assertLessEqual(interval, lease, lease)
        self.assertEqual(protocol.heartbeat_interval(3), 1)
        self.assertEqual(protocol.heartbeat_interval(2), 1)

    def test_interval_never_exceeds_a_minute(self):
        self.assertEqual(protocol.heartbeat_interval(7200), 60)

    def test_lease_risk_flags_interval_not_shorter_than_lease(self):
        self.assertIsNone(protocol.lease_risk(60, 300))
        self.assertIsNone(protocol.lease_risk(100, 300))
        self.assertIsNotNone(protocol.lease_risk(150, 300))
        self.assertIsNotNone(protocol.lease_risk(300, 300))
        self.assertIsNotNone(protocol.lease_risk(60, 3))
        self.assertIsNotNone(protocol.lease_risk(None, 300))
        self.assertIsNotNone(protocol.lease_risk(0, 300))
        self.assertIsNotNone(protocol.lease_risk("x", 300))


if __name__ == "__main__":
    unittest.main(verbosity=2)
