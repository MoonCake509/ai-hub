"""CTX-01: отбор файлов в пакет — без сети и без заданий хоста."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import packets  # noqa: E402

ENTRIES = [
    {"path": "src/app.py", "size": 100, "sha256": "a" * 64},
    {"path": "src/util.py", "size": 200, "sha256": "b" * 64},
    {"path": "tests/test_app.py", "size": 300, "sha256": "c" * 64},
    {"path": ".env", "size": 50, "sha256": "d" * 64},
    {"path": "config/secret.key", "size": 60, "sha256": "e" * 64},
    {"path": "node_modules/lib/index.js", "size": 400, "sha256": "f" * 64},
    {"path": "docs/readme.md", "size": 500, "sha256": "g" * 64},
]


class PathRulesTest(unittest.TestCase):
    def test_rejects_absolute_and_escape(self):
        for bad in ("/etc/passwd", "C:/Windows/win.ini", "../outside.py", "src/../../x.py"):
            with self.assertRaises(packets.PacketError):
                packets.normalize_path(bad)

    def test_normalizes_separators(self):
        self.assertEqual(packets.normalize_path("src\\app.py"), "src/app.py")
        self.assertEqual(packets.normalize_path("./src//app.py"), "src/app.py")

    def test_secret_detection(self):
        for path in (".env", ".env.local", "keys/id_rsa", "config/auth.json", "certs/site.pem"):
            self.assertTrue(packets.is_secret(path), path)
        self.assertFalse(packets.is_secret("src/app.py"))

    def test_service_dirs(self):
        self.assertTrue(packets.in_skipped_dir("node_modules/lib/index.js"))
        self.assertTrue(packets.in_skipped_dir(".git/config"))
        self.assertFalse(packets.in_skipped_dir("src/app.py"))


class SelectionTest(unittest.TestCase):
    def test_secrets_never_selected_even_when_requested(self):
        items, _, stats = packets.select(ENTRIES, [".env", "config/secret.key"], 10_000)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertEqual(selected, [])
        self.assertEqual(stats["secret"], 2)

    def test_budget_excludes_and_explains(self):
        items, used, stats = packets.select(ENTRIES, [], 350)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertEqual(selected, ["src/app.py", "src/util.py"])
        self.assertEqual(used, 300)
        over = [i for i in items if not i["selected"] and "бюджет" in i["reason"]]
        self.assertTrue(over)
        self.assertEqual(stats["over_budget"], len(over))

    def test_pattern_selection(self):
        items, _, _ = packets.select(ENTRIES, ["tests/*"], 10_000)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertEqual(selected, ["tests/test_app.py"])
        reason = [i["reason"] for i in items if i["path"] == "tests/test_app.py"][0]
        self.assertIn("tests/*", reason)

    def test_unrequested_files_are_listed_with_reason(self):
        items, _, _ = packets.select(ENTRIES, ["src/*"], 10_000)
        docs = [i for i in items if i["path"] == "docs/readme.md"][0]
        self.assertFalse(docs["selected"])
        self.assertEqual(docs["reason"], "не запрошен")

    def test_empty_request_takes_everything_allowed(self):
        items, used, _ = packets.select(ENTRIES, [], 10_000)
        selected = [i["path"] for i in items if i["selected"]]
        self.assertIn("src/app.py", selected)
        self.assertIn("docs/readme.md", selected)
        self.assertNotIn(".env", selected)
        self.assertNotIn("node_modules/lib/index.js", selected)
        self.assertEqual(used, 1100)


class DigestTest(unittest.TestCase):
    def test_same_selection_same_digest(self):
        first, _, _ = packets.select(ENTRIES, [], 10_000)
        second, _, _ = packets.select(list(reversed(ENTRIES)), [], 10_000)
        self.assertEqual(packets.digest(first), packets.digest(second))

    def test_changed_content_changes_digest(self):
        base, _, _ = packets.select(ENTRIES, [], 10_000)
        changed = [dict(e) for e in ENTRIES]
        changed[0]["sha256"] = "z" * 64
        after, _, _ = packets.select(changed, [], 10_000)
        self.assertNotEqual(packets.digest(base), packets.digest(after))

    def test_budget_change_changes_digest(self):
        full, _, _ = packets.select(ENTRIES, [], 10_000)
        narrow, _, _ = packets.select(ENTRIES, [], 350)
        self.assertNotEqual(packets.digest(full), packets.digest(narrow))

    def test_digest_ignores_unselected(self):
        base, _, _ = packets.select(ENTRIES, [], 10_000)
        extra = ENTRIES + [{"path": "notes.txt", "size": 5, "sha256": "n" * 64}]
        with_extra, _, _ = packets.select(extra, [], 10_000)
        self.assertNotEqual(packets.digest(base), packets.digest(with_extra))

    def test_canonical_is_stable_text(self):
        items, _, _ = packets.select(ENTRIES, ["src/*"], 10_000)
        self.assertEqual(packets.canonical(items), "src/app.py|100|" + "a" * 64 + "\n" + "src/util.py|200|" + "b" * 64)


class SafetyTest(unittest.TestCase):
    def test_safe_join_blocks_escape(self):
        root = os.path.abspath(os.sep + "tmp")
        with self.assertRaises(packets.PacketError):
            packets.safe_join(root, "../etc/passwd")
        self.assertTrue(packets.safe_join(root, "a/b.txt").startswith(root))

    def test_bad_budget(self):
        with self.assertRaises(packets.PacketError):
            packets.select(ENTRIES, [], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)