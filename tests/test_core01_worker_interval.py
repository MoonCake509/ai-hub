"""CORE-01: воркеры подтверждают аренду до её истечения.

Модули воркеров загружаются из репозитория как есть: проверяется реальная
функция интервала, а не её копия в тесте.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path):
    spec = importlib.util.spec_from_file_location("core01_" + path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PATHS = {
    "server": ROOT / "agents" / "server" / "worker_v2.py",
    "laptop": ROOT / "agents" / "laptop" / "worker_v2.py",
}
WORKERS = {name: load(path) for name, path in PATHS.items()}


class WorkerHeartbeat(unittest.TestCase):
    def test_default_lease_uses_hub_interval(self):
        for name, mod in WORKERS.items():
            with self.subTest(worker=name):
                self.assertEqual(mod.attempt_heartbeat_interval(300, 60), 60)
                self.assertEqual(mod.attempt_heartbeat_interval(300, None), 60)

    def test_short_test_lease_heartbeats_before_expiry(self):
        for name, mod in WORKERS.items():
            with self.subTest(worker=name):
                self.assertEqual(mod.attempt_heartbeat_interval(3, None), 1)
                # Хаб может прислать длинный интервал, но воркер не имеет права
                # ждать дольше самой аренды: иначе первое подтверждение — после
                # истечения, и живая попытка становится unknown.
                self.assertEqual(mod.attempt_heartbeat_interval(3, 60), 1)
                self.assertLess(mod.attempt_heartbeat_interval(3, 60), 3)

    def test_interval_never_exceeds_a_minute(self):
        for name, mod in WORKERS.items():
            with self.subTest(worker=name):
                self.assertEqual(mod.attempt_heartbeat_interval(3600, 600), 60)
                self.assertEqual(mod.attempt_heartbeat_interval(3600, None), 60)

    def test_poll_declares_protocol_and_heartbeat_interval(self):
        for name, path in PATHS.items():
            with self.subTest(worker=name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("protocol={PROTOCOL}", text)
                self.assertIn("hb_interval_s", text)
                self.assertIn("attempt_heartbeat_interval", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
