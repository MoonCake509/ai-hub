"""ADAPTER-01: выбор модели применяется к одному запуску, а не к конфигу клиента.

Тест выполняется на Windows-хосте: bridge.py — часть воркера ноутбука.
Реальные CLI не запускаются: подменяется subprocess.run, проверяется argv.
"""
import hashlib
import importlib.util
import os
import sys
import threading
import unittest
from types import SimpleNamespace

BRIDGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "agents", "laptop", "bridge.py")

CONFIG_FILES = [
    os.path.expanduser("~/.codex/config.toml"),
    os.path.expanduser("~/.codex/auth.json"),
    os.path.expanduser("~/AppData/Local/hermes/config.yaml"),
]


def load_bridge():
    spec = importlib.util.spec_from_file_location("aihub_bridge_adapter01", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "nt", "bridge is Windows-only")
class PerRunSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()
        cls.bridge.resolve_codex_exe = lambda: r"C:\fake\codex.exe"
        cls.bridge.HERMES_EXE = r"C:\fake\hermes.exe"
        cls.calls = []
        cls._real_run = cls.bridge.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            cls.calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout=b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
                                   stderr=b"")

        cls.bridge.subprocess.run = fake_run

    @classmethod
    def tearDownClass(cls):
        cls.bridge.subprocess.run = cls._real_run

    def setUp(self):
        self.__class__.calls.clear()

    def _digest(self):
        parts = []
        for path in CONFIG_FILES:
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    parts.append(path + ":" + hashlib.sha256(fh.read()).hexdigest())
        return "|".join(parts)

    def test_codex_model_is_a_flag_of_this_run(self):
        self.bridge.run_codex("hi", 5, requested_model="gpt-6.1-sol", requested_profile="openai")
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn("exec", argv)

    def test_hermes_model_and_provider_are_flags_of_this_run(self):
        self.bridge.run_hermes("hi", 5, requested_model="gpt-6.1-sol", requested_profile="router-cheap")
        argv = self.calls[0]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn("--provider", argv)

    def test_clients_config_is_not_rewritten(self):
        before = self._digest()
        self.bridge.run_codex("hi", 5, requested_model="gpt-6-luna", requested_profile="openai")
        self.bridge.run_hermes("hi", 5, requested_model="gpt-6.1-sol", requested_profile="router-cheap")
        self.assertEqual(before, self._digest(),
                         "выбор модели не должен менять глобальный конфиг клиента")

    def test_parallel_profiles_do_not_overwrite_each_other(self):
        results = {}

        def worker(model):
            self.bridge.run_codex(f"hi {model}", 5, requested_model=model, requested_profile="openai")
            results[model] = self.calls[-1]

        threads = [threading.Thread(target=worker, args=(m,)) for m in ("gpt-6.1-sol", "gpt-6-luna")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["gpt-6.1-sol"][results["gpt-6.1-sol"].index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(results["gpt-6-luna"][results["gpt-6-luna"].index("-m") + 1], "gpt-6-luna")

    def test_unknown_model_is_rejected_by_bridge(self):
        with self.assertRaises(ValueError):
            self.bridge.run_hermes("hi", 5, requested_model="not-a-model", requested_profile="router-cheap")

    def test_openclaw_refuses_selection_instead_of_ignoring_it(self):
        result = self.bridge.run_openclaw("hi", 5, requested_model="gpt-6.1-sol", requested_profile="router-cheap")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("disabled", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)