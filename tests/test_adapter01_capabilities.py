"""ADAPTER-01: матрица возможностей — юнит без сети и без запуска агентов."""
import os
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import capabilities  # noqa: E402

with open(os.path.join(ROOT, "config", "agents.yaml"), encoding="utf-8") as fh:
    CATALOG = yaml.safe_load(fh)
AGENTS = {item["id"]: item for item in CATALOG["agents"]}


class CatalogDeclarationTest(unittest.TestCase):
    def test_every_agent_declares_all_capabilities(self):
        for agent_id, agent in AGENTS.items():
            declared = agent.get("capabilities") or {}
            self.assertEqual(set(declared), set(capabilities.CAPABILITIES),
                             f"{agent_id} declares {sorted(declared)}")

    def test_supported_capability_has_evidence(self):
        for agent_id, agent in AGENTS.items():
            for name, value in (agent.get("capabilities") or {}).items():
                if isinstance(value, dict) and value.get("supported"):
                    self.assertTrue(value.get("evidence"),
                                    f"{agent_id}.{name} supported without evidence")

    def test_unsupported_capability_has_reason(self):
        for agent_id, agent in AGENTS.items():
            for name, value in (agent.get("capabilities") or {}).items():
                if isinstance(value, dict) and not value.get("supported"):
                    self.assertTrue(value.get("reason"),
                                    f"{agent_id}.{name} disabled without reason")


class ResolveTest(unittest.TestCase):
    def test_undeclared_capability_is_off_with_reason(self):
        agent = {"id": "probe", "profiles": [{"id": "p", "models": ["m"]}]}
        matrix = capabilities.resolve(agent, "p", "m")
        for name in capabilities.CAPABILITIES:
            self.assertFalse(matrix["capabilities"][name]["supported"])
            self.assertTrue(matrix["capabilities"][name]["reason"])
            self.assertEqual(matrix["capabilities"][name]["evidence"], "unknown")

    def test_codex_workdir_on_streaming_off(self):
        matrix = capabilities.resolve(AGENTS["codex"], "openai", "gpt-6.1-sol")
        self.assertTrue(matrix["capabilities"]["workdir"]["supported"])
        self.assertEqual(matrix["capabilities"]["workdir"]["evidence"],
                         "fixture:core02b-live")
        self.assertFalse(matrix["capabilities"]["streaming"]["supported"])
        self.assertIn("поток", matrix["capabilities"]["streaming"]["reason"].lower())

    def test_model_level_override_wins(self):
        agent = {
            "id": "a",
            "capabilities": {"text": True},
            "profiles": [{
                "id": "p", "models": ["base", "special"],
                "model_capabilities": {"special": {"capabilities": {"text": False}}},
            }],
        }
        self.assertTrue(capabilities.resolve(agent, "p", "base")["capabilities"]["text"]["supported"])
        self.assertFalse(capabilities.resolve(agent, "p", "special")["capabilities"]["text"]["supported"])

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(capabilities.CapabilityError) as ctx:
            capabilities.resolve(AGENTS["codex"], "openai", "gpt-9-imaginary")
        self.assertEqual(ctx.exception.code, "unknown_model")
        self.assertIn("gpt-6.1-sol", ctx.exception.details["supported_models"])

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(capabilities.CapabilityError) as ctx:
            capabilities.resolve(AGENTS["codex"], "nope", None)
        self.assertEqual(ctx.exception.code, "unknown_profile")

    def test_empty_model_does_not_invent_one(self):
        matrix = capabilities.resolve(AGENTS["openclaw-laptop"], "router-cheap", None)
        self.assertIsNone(matrix["model"])
        self.assertEqual(matrix["default_model"], "deepseek-v4.1-flash-1017")

    def test_ocr_declares_image_input(self):
        matrix = capabilities.resolve(AGENTS["xiaomi-ocr-server"], "local", "xiaomi-ocr-q8")
        self.assertIn("image", matrix["modalities"]["input"])
        self.assertIn("image", matrix["required_modalities"])


class PreflightTest(unittest.TestCase):
    def test_rejects_unsupported_capability_with_reason(self):
        with self.assertRaises(capabilities.CapabilityError) as ctx:
            capabilities.preflight(AGENTS["llm-server"], "local", None, ["workdir"])
        self.assertEqual(ctx.exception.code, "capability_unsupported")
        self.assertIn("workdir", ctx.exception.details["missing"])
        self.assertTrue(ctx.exception.details["reasons"]["workdir"])

    def test_rejects_unknown_capability_name(self):
        with self.assertRaises(capabilities.CapabilityError) as ctx:
            capabilities.preflight(AGENTS["codex"], "openai", None, ["telepathy"])
        self.assertEqual(ctx.exception.code, "unknown_capability")

    def test_accepts_supported_combination(self):
        matrix = capabilities.preflight(AGENTS["codex"], "openai", "gpt-6.1-sol",
                                        ["text", "workdir", "commands"])
        self.assertEqual(matrix["agent_id"], "codex")
        self.assertIn("workdir", matrix["supported"])

    def test_openclaw_rejects_workdir(self):
        with self.assertRaises(capabilities.CapabilityError) as ctx:
            capabilities.preflight(AGENTS["openclaw-laptop"], "router-cheap", None, ["workdir"])
        self.assertEqual(ctx.exception.code, "capability_unsupported")

    def test_empty_needs_always_pass(self):
        matrix = capabilities.preflight(AGENTS["qwen-code"], "qwen-sub", None, [])
        self.assertTrue(matrix["capabilities"]["text"]["supported"])


class DescribeTest(unittest.TestCase):
    def test_describe_lists_every_model(self):
        described = capabilities.describe(AGENTS["codex"])
        profile = described["profiles"][0]
        declared = len(AGENTS["codex"]["profiles"][0]["models"])
        self.assertEqual(len(profile["models"]), declared)
        for entry in profile["models"]:
            self.assertIn("capabilities", entry)
            self.assertTrue(entry["model"])

    def test_describe_marks_disabled_model_selection(self):
        described = capabilities.describe(AGENTS["openclaw-laptop"])
        self.assertFalse(described["model_selection"])
        self.assertTrue(described["model_selection_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)