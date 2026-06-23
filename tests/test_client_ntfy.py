"""Unit test for MudClient._post_ntfy - the incoming-tell push notification
(spec: docs/superpowers/specs/2026-06-21-web-dashboard-design.md). Tested as
a static method, no Tkinter instance needed, by monkeypatching
urllib.request.urlopen so no real network call happens.

Note: written after the implementation (not strict red-green) because
MudClient lives in a single large Tkinter-coupled module with no existing
test harness; this fills the gap rather than skipping verification
entirely. The earlier paths.py/hub.py pieces (the parts the project's own
design spec calls out as worth automating) were done test-first.
"""
import unittest
from unittest.mock import patch

from katmud_lib.client import MudClient


class _FakeResponse:
    def close(self):
        pass


class PostNtfyTest(unittest.TestCase):
    def test_posts_to_ntfy_topic_url_with_text_body(self):
        with patch("urllib.request.urlopen",
                  return_value=_FakeResponse()) as mock_urlopen:
            MudClient._post_ntfy("mysecrettopic", "Wiz tells you: bot check")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/mysecrettopic")
        self.assertEqual(req.data, b"Wiz tells you: bot check")
        self.assertEqual(req.get_method(), "POST")

    def test_swallows_urlopen_exceptions(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            MudClient._post_ntfy("mysecrettopic", "hi")  # must not raise


if __name__ == "__main__":
    unittest.main()
