"""Unit tests for katmud_lib.hub's build_state aggregation - the GET
/api/state logic from docs/superpowers/specs/2026-06-21-web-dashboard-design.md,
kept as a pure function (no HTTP, no Tkinter) so it's testable without
spinning up a server or a character process.
"""
import os
import tempfile
import unittest

from katmud_lib import hub, paths


class BuildStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_webstate_dir = paths.WEBSTATE_DIR
        self._orig_reminders_file = paths.REMINDERS_FILE
        paths.WEBSTATE_DIR = os.path.join(self._tmp.name, "webstate")
        paths.REMINDERS_FILE = os.path.join(self._tmp.name, "reminders.json")
        os.makedirs(paths.WEBSTATE_DIR)

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_webstate_dir
        paths.REMINDERS_FILE = self._orig_reminders_file
        self._tmp.cleanup()

    def _write_webstate(self, profile_id, **fields):
        paths.save_json(paths.webstate_file(profile_id), fields)

    def test_no_webstate_files_means_no_characters(self):
        state = hub.build_state(now=1000.0)
        self.assertEqual(state["characters"], [])

    def test_recent_character_is_reported_online(self):
        self._write_webstate("3s-din", updated=999.0, room="Square")
        state = hub.build_state(now=1000.0)
        self.assertEqual(len(state["characters"]), 1)
        char = state["characters"][0]
        self.assertEqual(char["profile_id"], "3s-din")
        self.assertEqual(char["room"], "Square")
        self.assertFalse(char["offline"])

    def test_character_older_than_ten_seconds_is_offline(self):
        self._write_webstate("3s-din", updated=985.0, room="Square")
        state = hub.build_state(now=1000.0)
        self.assertTrue(state["characters"][0]["offline"])

    def test_character_exactly_ten_seconds_old_is_still_online(self):
        self._write_webstate("3s-din", updated=990.0, room="Square")
        state = hub.build_state(now=1000.0)
        self.assertFalse(state["characters"][0]["offline"])

    def test_multiple_characters_all_reported(self):
        self._write_webstate("3s-din", updated=999.0)
        self._write_webstate("3k-din", updated=999.0)
        state = hub.build_state(now=1000.0)
        ids = sorted(c["profile_id"] for c in state["characters"])
        self.assertEqual(ids, ["3k-din", "3s-din"])

    def test_reminders_file_merged_into_state(self):
        paths.save_json(paths.REMINDERS_FILE,
                        {"reminders": [{"id": 1, "text": "stretch"}],
                         "next_id": 2})
        state = hub.build_state(now=1000.0)
        self.assertEqual(state["reminders"], [{"id": 1, "text": "stretch"}])

    def test_missing_reminders_file_yields_empty_list(self):
        state = hub.build_state(now=1000.0)
        self.assertEqual(state["reminders"], [])


if __name__ == "__main__":
    unittest.main()
