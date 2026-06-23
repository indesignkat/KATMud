"""Unit tests for the webstate/webcmd file helpers added to katmud_lib.paths
for the phone/web dashboard (docs/superpowers/specs/2026-06-21-web-dashboard-design.md).

Isolated from the real install dir by monkeypatching paths.WEBSTATE_DIR to a
temp directory for the duration of each test.
"""
import os
import tempfile
import unittest

from katmud_lib import paths


class WebstatePathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = paths.WEBSTATE_DIR
        paths.WEBSTATE_DIR = self._tmp.name

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_webstate_file_is_under_webstate_dir_with_json_ext(self):
        p = paths.webstate_file("3s-din")
        self.assertEqual(os.path.dirname(p), self._tmp.name)
        self.assertTrue(os.path.basename(p).endswith(".json"))

    def test_webcmd_file_is_under_webstate_dir_with_cmd_ext(self):
        p = paths.webcmd_file("3s-din")
        self.assertEqual(os.path.dirname(p), self._tmp.name)
        self.assertTrue(os.path.basename(p).endswith(".cmd"))

    def test_webstate_and_webcmd_files_differ_for_same_profile(self):
        self.assertNotEqual(paths.webstate_file("3s-din"),
                            paths.webcmd_file("3s-din"))

    def test_different_profile_ids_get_different_files(self):
        self.assertNotEqual(paths.webcmd_file("3s-din"),
                            paths.webcmd_file("3k-din"))


class WebcmdClaimTest(unittest.TestCase):
    """write_webcmd (hub-side) / claim_webcmd (character-side) - the
    single-writer-per-file design from the plan that replaces a shared
    pending_command field (which had a lost-update race: the character
    rewrites its snapshot every second, so a plain read-modify-write on
    a shared file could drop a command written concurrently by the hub)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = paths.WEBSTATE_DIR
        paths.WEBSTATE_DIR = self._tmp.name

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_claim_with_no_pending_command_returns_none(self):
        self.assertIsNone(paths.claim_webcmd("3s-din"))

    def test_claim_returns_text_written_by_hub(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        self.assertEqual(paths.claim_webcmd("3s-din"), "Hunt orly")

    def test_claim_deletes_the_file_so_it_cannot_be_claimed_twice(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        paths.claim_webcmd("3s-din")
        self.assertIsNone(paths.claim_webcmd("3s-din"))

    def test_claim_is_scoped_to_its_own_profile_id(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        self.assertIsNone(paths.claim_webcmd("3k-din"))
        self.assertEqual(paths.claim_webcmd("3s-din"), "Hunt orly")


class EnsureDirsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = paths.WEBSTATE_DIR
        paths.WEBSTATE_DIR = os.path.join(self._tmp.name, "webstate")

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_ensure_dirs_creates_webstate_dir(self):
        self.assertFalse(os.path.isdir(paths.WEBSTATE_DIR))
        paths.ensure_dirs()
        self.assertTrue(os.path.isdir(paths.WEBSTATE_DIR))


if __name__ == "__main__":
    unittest.main()
