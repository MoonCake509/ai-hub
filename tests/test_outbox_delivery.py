import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
REPO_LAPTOP = ROOT.parent / "agents" / "laptop"
ADAPTER_ROOT = REPO_LAPTOP if REPO_LAPTOP.is_dir() else ROOT.parent / "workspace-fix" / "adapter-changes"
sys.path.insert(0, str(ADAPTER_ROOT))


def load_worker(name: str, source: Path):
    holder = tempfile.TemporaryDirectory(prefix=f"{name}-worker-module-")
    target = Path(holder.name) / "worker_v2.py"
    shutil.copy2(source, target)
    spec = importlib.util.spec_from_file_location(f"{name}_isolated_worker", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, holder


LOCAL_RELEASE_LAPTOP = ROOT.parent / "workspace-fix" / "adapter-changes" / "laptop-worker_v2.py"
SERVER_SOURCE = ROOT / "server-worker_v2.py"
if LOCAL_RELEASE_LAPTOP.is_file():
    LAPTOP_SOURCE = LOCAL_RELEASE_LAPTOP
elif (ROOT / "laptop-worker_v2.py").is_file():
    LAPTOP_SOURCE = ROOT / "laptop-worker_v2.py"
else:
    LAPTOP_SOURCE = REPO_LAPTOP / "worker_v2.py"
    SERVER_SOURCE = ROOT.parent / "agents" / "server" / "worker_v2.py"
LAPTOP, _LAPTOP_MODULE_DIR = load_worker("laptop", LAPTOP_SOURCE)
SERVER, _SERVER_MODULE_DIR = load_worker("server", SERVER_SOURCE)


class OutboxDeliveryTests(unittest.TestCase):
    workers = (LAPTOP, SERVER)

    def make_outbox_file(self, folder: Path) -> Path:
        path = folder / "job__attempt.json"
        path.write_text(json.dumps({"job_id": "job", "attempt_id": "attempt", "ok": True,
                                   "output": "synthetic result"}), encoding="utf-8")
        return path

    def test_non_acknowledgements_keep_file_and_do_not_spin(self):
        cases = (
            (404, {"detail": "unknown attempt"}, False),
            (409, {"detail": "conflict"}, False),
            (200, {"ok": False, "recorded": True, "outcome": "applied"}, False),
            (200, {"ok": True, "outcome": "applied"}, False),
        )
        for worker in self.workers:
            for code, response, _ in cases:
                with self.subTest(worker=worker.__name__, code=code, response=response):
                    with tempfile.TemporaryDirectory(prefix="outbox-retain-") as temp:
                        path = self.make_outbox_file(Path(temp))
                        with patch.object(worker, "http", return_value=(code, response)) as http_call, \
                             patch.object(worker.time, "sleep"):
                            self.assertFalse(worker.deliver(str(path)))
                        self.assertTrue(path.exists())
                        http_call.assert_called_once()

    def test_invalid_json_ack_keeps_file_without_retry(self):
        for worker in self.workers:
            with self.subTest(worker=worker.__name__):
                with tempfile.TemporaryDirectory(prefix="outbox-invalid-ack-") as temp:
                    path = self.make_outbox_file(Path(temp))
                    error = json.JSONDecodeError("invalid response", "{", 1)
                    with patch.object(worker, "http", side_effect=error) as http_call, \
                         patch.object(worker.time, "sleep"):
                        self.assertFalse(worker.deliver(str(path)))
                    self.assertTrue(path.exists())
                    http_call.assert_called_once()

    def test_only_recorded_success_ack_removes_file(self):
        outcomes = ("applied", "duplicate", "late", "stale")
        for worker in self.workers:
            for outcome in outcomes:
                with self.subTest(worker=worker.__name__, outcome=outcome):
                    with tempfile.TemporaryDirectory(prefix="outbox-confirmed-") as temp:
                        path = self.make_outbox_file(Path(temp))
                        response = {"ok": True, "recorded": True, "outcome": outcome}
                        with patch.object(worker, "http", return_value=(200, response)) as http_call:
                            self.assertTrue(worker.deliver(str(path)))
                        self.assertFalse(path.exists())
                        http_call.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
