"""Unit tests for katmud_lib.hub.resolve_bind_host - picks the Tailscale
IPv4 address to bind to (per the spec: bind the Tailscale interface, not
0.0.0.0, since POST /api/command can run MUD commands and release the
deadman gate - 0.0.0.0 would expose that to the whole LAN, not just
Tailscale devices). Falls back to localhost-only if `tailscale` isn't
installed or fails, rather than crashing the hub.

The real `tailscale ip -4` call is injected as `runner` so this is
testable without Tailscale installed.
"""
import subprocess
import unittest

from katmud_lib import hub


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class ResolveBindHostTest(unittest.TestCase):
    def test_returns_tailscale_ip_on_success(self):
        runner = lambda *a, **k: _FakeCompleted(0, "100.64.1.2\n")
        self.assertEqual(hub.resolve_bind_host(runner=runner),
                         "100.64.1.2")

    def test_falls_back_when_tailscale_not_installed(self):
        def runner(*a, **k):
            raise FileNotFoundError("no tailscale binary")
        self.assertEqual(hub.resolve_bind_host(runner=runner),
                         "127.0.0.1")

    def test_falls_back_on_nonzero_exit(self):
        runner = lambda *a, **k: _FakeCompleted(1, "")
        self.assertEqual(hub.resolve_bind_host(runner=runner),
                         "127.0.0.1")

    def test_falls_back_on_empty_stdout(self):
        runner = lambda *a, **k: _FakeCompleted(0, "\n")
        self.assertEqual(hub.resolve_bind_host(runner=runner),
                         "127.0.0.1")

    def test_takes_first_line_only(self):
        runner = lambda *a, **k: _FakeCompleted(
            0, "100.64.1.2\nfd7a:115c:a1e0::1\n")
        self.assertEqual(hub.resolve_bind_host(runner=runner),
                         "100.64.1.2")


if __name__ == "__main__":
    unittest.main()
