"""CTX-02: сборщик контекста — юнит без сети и хоста."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import context  # noqa: E402

ENTRIES = [
    {"path": "src/app.py", "size": 400},
    {"path": "src/util.py", "size": 300},
    {"path": "src/other.py", "size": 900},
    {"path": "docs/api.yaml", "size": 8000},
    {"path": "tests/test_app.py", "size": 200},
]
CONTENTS = {"src/app.py": "from . import util\nimport os\n"}


class EstimateTest(unittest.TestCase):
    def test_estimate_from_text_and_size(self):
        self.assertEqual(context.estimate_tokens("abcd"), 1)
        self.assertEqual(context.estimate_tokens(4000), 1000)
        self.assertGreaterEqual(context.estimate_tokens(0), 1)


class ImportsTest(unittest.TestCase):
    def test_python_imports(self):
        found = context.parse_imports("src/app.py", "from . import util\nimport json\n")
        self.assertIn(".util", found)
        self.assertIn("json", found)

    def test_js_imports(self):
        text = "import x from './util.js';\nconst y = require('lodash');\n"
        found = context.parse_imports("src/app.js", text)
        self.assertIn("./util.js", found)
        self.assertIn("lodash", found)

    def test_relative_resolution(self):
        paths = ["src/app.py", "src/util.py", "src/lib/deep.py"]
        self.assertEqual(context.resolve_module(".util", "src/app.py", paths), "src/util.py")
        self.assertEqual(context.resolve_module("./lib/deep", "src/app.py", paths), "src/lib/deep.py")
        self.assertIsNone(context.resolve_module("./missing", "src/app.py", paths))


class BuildTest(unittest.TestCase):
    def test_contract_is_always_included(self):
        built = context.build(ENTRIES, CONTENTS, "fix app", "src/app.py", ["docs/api.yaml"], 5000)
        self.assertIn("docs/api.yaml", built["selected"])
        self.assertIn("src/app.py", built["selected"])

    def test_contract_exceeds_budget_is_error(self):
        with self.assertRaises(context.ContextError) as ctx:
            context.build(ENTRIES, CONTENTS, "fix", "src/app.py", ["docs/api.yaml"], 100)
        self.assertEqual(ctx.exception.code, "contract_exceeds_budget")

    def test_missing_contract_is_error(self):
        with self.assertRaises(context.ContextError) as ctx:
            context.build(ENTRIES, CONTENTS, "fix", "src/app.py", ["docs/none.yaml"], 5000)
        self.assertEqual(ctx.exception.code, "missing_contract")

    def test_unknown_entry_is_error(self):
        with self.assertRaises(context.ContextError) as ctx:
            context.build(ENTRIES, CONTENTS, "fix", "src/nope.py", [], 5000)
        self.assertEqual(ctx.exception.code, "unknown_entry")

    def test_single_module_task_gets_small_subset(self):
        built = context.build(ENTRIES, CONTENTS, "fix app", "src/app.py", [], 300)
        self.assertIn("src/app.py", built["selected"])
        self.assertIn("src/util.py", built["selected"])
        self.assertNotIn("src/other.py", built["selected"])
        self.assertLess(built["used_tokens"], built["total_project_tokens"])
        self.assertGreater(built["saved_tokens"], 0)

    def test_every_excluded_file_has_reason(self):
        built = context.build(ENTRIES, CONTENTS, "fix app", "src/app.py", [], 300)
        for item in built["items"]:
            if not item["selected"]:
                self.assertTrue(item["reason"])

    def test_digest_is_stable_and_sensitive(self):
        first = context.build(ENTRIES, CONTENTS, "fix app", "src/app.py", [], 5000)
        second = context.build(list(reversed(ENTRIES)), CONTENTS, "fix app", "src/app.py", [], 5000)
        self.assertEqual(first["digest"], second["digest"])
        changed = [dict(e) for e in ENTRIES]
        changed[0]["size"] = 401
        third = context.build(changed, CONTENTS, "fix app", "src/app.py", [], 5000)
        self.assertNotEqual(first["digest"], third["digest"])

    def test_imports_are_prioritised(self):
        built = context.build(ENTRIES, CONTENTS, "", "src/app.py", [], 200)
        self.assertIn("src/util.py", built["selected"])
        self.assertIn("src/util.py", built["imports"])


if __name__ == "__main__":
    unittest.main(verbosity=2)