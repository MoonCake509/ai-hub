"""CORE-02: ключи ресурсов канонизируются, alias не обходит лимит.

Без БД: проверяется только построение ключа — то, что делает «один CDP через
два alias» одним ресурсом и не даёт обойти занятость сменой регистра или
формы записи пути.
"""
import unittest

from app import resources


class CheckoutKeys(unittest.TestCase):
    def test_same_path_in_different_spellings_is_one_resource(self):
        win = "R:" + chr(92) + "Proj"
        variants = [win, "R:/Proj", win.lower(), win + chr(92), "  " + win.upper() + "  "]
        keys = {resources.key_for("checkout", "server", item) for item in variants}
        self.assertEqual(len(keys), 1, keys)

    def test_different_hosts_are_different_resources(self):
        a = resources.key_for("checkout", "server", "R:/Proj")
        b = resources.key_for("checkout", "laptop", "R:/Proj")
        self.assertNotEqual(a, b)

    def test_different_paths_are_different_resources(self):
        a = resources.key_for("checkout", "server", "R:/Proj")
        b = resources.key_for("checkout", "server", "R:/Proj2")
        self.assertNotEqual(a, b)

    def test_parent_reference_is_rejected(self):
        for bad in ("R:/Proj/../Other", "R:/Proj/../Other2", ".."):
            with self.assertRaises(ValueError):
                resources.key_for("checkout", "server", bad)

    def test_empty_path_is_rejected(self):
        with self.assertRaises(ValueError):
            resources.key_for("checkout", "server", "   ")


class CdpKeys(unittest.TestCase):
    def test_localhost_and_loopback_are_one_browser(self):
        a = resources.key_for("cdp", "laptop", "localhost:9333")
        b = resources.key_for("cdp", "laptop", "127.0.0.1:9333")
        c = resources.key_for("cdp", "laptop", "LOCALHOST:9333")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_two_ports_on_one_host_are_two_instances(self):
        a = resources.key_for("cdp", "laptop", "127.0.0.1:9333")
        b = resources.key_for("cdp", "laptop", "127.0.0.1:9444")
        self.assertNotEqual(a, b)

    def test_port_is_required(self):
        with self.assertRaises(ValueError):
            resources.key_for("cdp", "laptop", "127.0.0.1")

    def test_two_aliases_of_one_endpoint_share_a_resource(self):
        # alias — имя агента; ключ строится из адреса, поэтому два агента
        # на одном browser делят одну аренду.
        qwen = resources.key_for("cdp", "laptop", "127.0.0.1:9333")
        other = resources.key_for("cdp", "laptop", "localhost:9333")
        self.assertEqual(qwen, other)


class KeyShape(unittest.TestCase):
    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            resources.key_for("gpu", "server", "x")

    def test_host_is_required(self):
        with self.assertRaises(ValueError):
            resources.key_for("cli", "", "codex")

    def test_key_keeps_kind_and_host(self):
        key = resources.key_for("cli", "Laptop", "Codex")
        self.assertEqual(key, "cli:laptop:codex")


if __name__ == "__main__":
    unittest.main(verbosity=2)
