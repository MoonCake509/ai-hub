"""FLOW-P1: единая deny-политика хаба и host worker — юнит без сети.

Аудит: пакет исключает auth.json и credentials*, host snapshot обязан
отклонять те же пути. Политика — один модуль (hub/app/security_policy.py),
host worker грузит его по пути; расхождение ломает тест.
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hub"))

from app import packets, security_policy  # noqa: E402

HOST_FILE = os.path.join(ROOT, "agents", "laptop", "workspace_host.py")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ParityTest(unittest.TestCase):
    def _load_host_module(self):
        spec = importlib.util.spec_from_file_location("workspace_host_under_test",
                                                      HOST_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_host_uses_canonical_policy(self):
        host = self._load_host_module()
        self.assertEqual(set(host.DENY_DIRS), set(security_policy.DENY_DIRS))
        self.assertEqual(set(host.DENY_NAMES), set(security_policy.SECRET_NAMES))
        self.assertEqual(tuple(host.DENY_SUFFIX), tuple(security_policy.SECRET_SUFFIXES))

    def test_inspect_manifest_materializes_policy_digest(self):
        host = self._load_host_module()
        with tempfile.TemporaryDirectory(prefix="host-policy-digest-") as root:
            info = host.op_inspect(root, {})
        self.assertEqual(info["security_policy"], security_policy.policy_digest())
        self.assertIsInstance(info["security_policy"], dict)
        json.dumps(info)

    def test_host_denied_matches_server(self):
        host = self._load_host_module()
        for path in ("auth.json", "credentials.json", "config/secrets.env",
                     ".env", "keys/id_rsa", "dump.bak", "node_modules/x/index.js"):
            self.assertIsNotNone(host.denied(path), path)
            self.assertIsNotNone(security_policy.denied_reason(path), path)
        for path in ("src/app.py", "docs/readme.md", "tests/test_app.py"):
            self.assertIsNone(host.denied(path), path)
            self.assertIsNone(security_policy.denied_reason(path), path)


class SecretExclusionTest(unittest.TestCase):
    """Отрицательные fixture аудита: секреты и чужие файлы не выбираются."""

    ENTRIES = [
        {"path": "src/app.py", "size": 100, "sha256": _sha("app")},
        {"path": "auth.json", "size": 40, "sha256": _sha("AUTH-SYNTHETIC")},
        {"path": "credentials.json", "size": 60, "sha256": _sha("CRED")},
        {"path": "config/deploy.env", "size": 30, "sha256": _sha("ENV")},
        {"path": "other/foreign.txt", "size": 70, "sha256": _sha("foreign")},
    ]

    def test_auth_json_never_selected_even_when_requested(self):
        items, _, stats = packets.select(self.ENTRIES, ["auth.json"], 10_000)
        self.assertEqual([i for i in items if i["selected"]], [])
        # auth.json и config/deploy.env (.env.*) отсечены как секреты.
        self.assertEqual(stats["secret"], 2)
        self.assertEqual(stats["unmatched"], 3)

    def test_credentials_never_selected(self):
        items, _, _ = packets.select(self.ENTRIES, ["credentials.json", "src/*"], 10_000)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertIn("src/app.py", selected)
        self.assertNotIn("credentials.json", selected)

    def test_unrequested_foreign_file_not_selected(self):
        """Чужой/невыбранный файл не попадает в пакет при шаблоне src/*."""
        items, _, _ = packets.select(self.ENTRIES, ["src/*"], 10_000)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertEqual(selected, ["src/app.py"])

    def test_secret_paths_appear_with_reason(self):
        items, _, _ = packets.select(self.ENTRIES, [], 10_000)
        auth = [i for i in items if i["path"] == "auth.json"][0]
        self.assertFalse(auth["selected"])
        self.assertIn("секрет", auth["reason"])

    def test_is_secret_matches_host_denied(self):
        host_spec = importlib.util.spec_from_file_location("wh2", HOST_FILE)
        host = importlib.util.module_from_spec(host_spec)
        host_spec.loader.exec_module(host)
        for path in ("auth.json", "sub/auth.json", "credentials.json", "a/.env"):
            self.assertTrue(packets.is_secret(path), path)
            self.assertIsNotNone(host.denied(path), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
