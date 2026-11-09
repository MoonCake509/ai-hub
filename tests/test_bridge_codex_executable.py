"""Codex bridge executable discovery follows the current installed CLI."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "agents" / "laptop" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_bridge_executable_test", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class CodexExecutableResolutionTests(unittest.TestCase):
    def test_explicit_environment_override_is_authoritative(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / "OpenAI" / "Codex" / "bin" / "installed" / "codex.exe"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"cli")
            override = str(root / "custom" / "codex.exe")
            with patch.dict(os.environ, {"LOCALAPPDATA": temp, "CODEX_EXE": override}):
                self.assertEqual(bridge.resolve_codex_exe(), override)

    def test_resolves_newest_existing_cli_under_trusted_install_root(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_root = Path(temp) / "OpenAI" / "Codex" / "bin"
            older = bin_root / "older" / "codex.exe"
            newest = bin_root / "newest" / "codex.exe"
            missing = bin_root / "incomplete-update"
            older.parent.mkdir(parents=True)
            newest.parent.mkdir(parents=True)
            missing.mkdir(parents=True)
            older.write_bytes(b"old")
            newest.write_bytes(b"new")
            os.utime(older, (100, 100))
            os.utime(newest, (200, 200))
            with patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=True):
                self.assertEqual(bridge.resolve_codex_exe(), str(newest))

    def test_each_codex_run_resolves_the_current_executable(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with patch.object(bridge, "resolve_codex_exe", side_effect=["codex-v1.exe", "codex-v2.exe"]), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            bridge.run_codex("prompt one", 30)
            bridge.run_codex("prompt two", 30)
        self.assertEqual(run.call_args_list[0].args[0][0], "codex-v1.exe")
        self.assertEqual(run.call_args_list[1].args[0][0], "codex-v2.exe")

    def test_agent_health_path_uses_fresh_resolution(self):
        with patch.object(bridge, "resolve_codex_exe", return_value="current-codex.exe"):
            path = bridge._agent_path("codex", bridge.AGENTS["codex"])
        self.assertEqual(path, "current-codex.exe")
        self.assertEqual(bridge.AGENTS["codex"]["path"], "current-codex.exe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
