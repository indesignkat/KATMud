"""Unit tests for MudClient._poll_webcmd / _write_webstate - the per-tick
dashboard wiring (spec: docs/superpowers/specs/2026-06-21-web-dashboard-design.md).

MudClient.__init__ opens a real MUD connection, so it can't be instantiated
in a unit test. Both methods are called unbound against a MagicMock
standing in for self - this still exercises the real method bodies (the
exact code tick() runs), just without a live socket/Tk window.

Written after the implementation, like test_client_ntfy.py - same reason
(no existing harness for this class) and same caveat (these confirm the
wiring behaves as intended, not a red-green proof).
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from katmud_lib import paths
from katmud_lib.client import MudClient


class PollWebcmdTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = paths.WEBSTATE_DIR
        self._orig_crash_log = paths.CRASH_LOG
        paths.WEBSTATE_DIR = self._tmp.name
        # _log_webcmd_failure appends to paths.CRASH_LOG - redirect it so a
        # test exercising that path doesn't write into the real install's
        # logs/crash.log.
        paths.CRASH_LOG = os.path.join(self._tmp.name, "crash.log")

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_dir
        paths.CRASH_LOG = self._orig_crash_log
        self._tmp.cleanup()

    def _fake_self(self, deadman_tripped=False):
        fake = MagicMock()
        fake.profile_id = "3s-din"
        fake.deadman_tripped = deadman_tripped
        return fake

    def test_no_pending_command_does_not_call_process_input(self):
        fake = self._fake_self()
        MudClient._poll_webcmd(fake)
        fake.process_input.assert_not_called()

    def test_pending_command_calls_process_input_with_manual_true(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        fake = self._fake_self()
        MudClient._poll_webcmd(fake)
        fake.process_input.assert_called_once_with("Hunt orly", manual=True)

    def test_pending_command_resets_last_manual(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        fake = self._fake_self()
        before = time.time()
        MudClient._poll_webcmd(fake)
        self.assertGreaterEqual(fake.last_manual, before)

    def test_pending_command_releases_tripped_deadman(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        fake = self._fake_self(deadman_tripped=True)
        MudClient._poll_webcmd(fake)
        self.assertFalse(fake.deadman_tripped)
        fake.write_local.assert_called_once()

    def test_pending_command_does_not_touch_deadman_if_not_tripped(self):
        paths.write_webcmd("3s-din", "Hunt orly")
        fake = self._fake_self(deadman_tripped=False)
        MudClient._poll_webcmd(fake)
        fake.write_local.assert_not_called()

    def test_process_input_exception_does_not_propagate(self):
        """The load-bearing invariant: tick() is `_poll_webcmd()` followed
        by more tick logic, ending in root.after(1000, self.tick) - if
        process_input throws and that propagates out of _poll_webcmd, the
        next tick() never gets scheduled and the whole heartbeat (deadman,
        reminders, status bar, webstate) dies silently. A bad dashboard
        command must not be able to do that.

        `self` here is a MagicMock standing in for a real MudClient
        instance, so `self._log_webcmd_failure(text)` inside _poll_webcmd
        resolves to a mock stub, not the real method body - this test
        only proves _poll_webcmd delegates to it instead of letting the
        exception propagate. _log_webcmd_failure's own behavior (logging
        + write_local) is covered separately below."""
        paths.write_webcmd("3s-din", "garbage;;;")
        fake = self._fake_self()
        fake.process_input.side_effect = ValueError("boom")
        MudClient._poll_webcmd(fake)  # must not raise
        fake._log_webcmd_failure.assert_called_once_with("garbage;;;")

    def test_log_webcmd_failure_writes_crash_log_and_notifies_user(self):
        fake = self._fake_self()
        try:
            raise ValueError("boom")
        except ValueError:
            MudClient._log_webcmd_failure(fake, "garbage;;;")
        fake.write_local.assert_called_once()
        with open(paths.CRASH_LOG, encoding="utf-8") as f:
            contents = f.read()
        self.assertIn("3s-din", contents)
        self.assertIn("garbage;;;", contents)
        self.assertIn("ValueError", contents)


class WriteWebstateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = paths.WEBSTATE_DIR
        paths.WEBSTATE_DIR = self._tmp.name

    def tearDown(self):
        paths.WEBSTATE_DIR = self._orig_dir
        self._tmp.cleanup()

    def _fake_self(self, **overrides):
        fake = MagicMock()
        fake.profile_id = "3s-din"
        fake.room = "Town Square"
        fake.last_sent = 990.0
        fake.last_manual = 1000.0
        fake.deadman_tripped = False
        fake.deadman_minutes = 0
        fake.vitals = {}
        fake.viking_state = {}
        fake._web_output = [{"t": 1.0, "line": "A goblin attacks!"}]
        fake._web_chats = [{"t": 1.0, "line": "Normal nods to you."}]
        fake._web_tells = [{"t": 1.0, "line": "Wiz tells you: hi",
                            "incoming": True}]
        for k, v in overrides.items():
            setattr(fake, k, v)
        return fake

    def test_writes_readable_snapshot_with_expected_fields(self):
        fake = self._fake_self()
        MudClient._write_webstate(fake, 1000.0)
        data, err = paths.load_json(paths.webstate_file("3s-din"))
        self.assertIsNone(err)
        self.assertEqual(data["room"], "Town Square")
        self.assertEqual(data["updated"], 1000.0)
        self.assertEqual(data["idle_seconds"], 10.0)
        self.assertFalse(data["deadman_tripped"])
        self.assertEqual(data["output"],
                         [{"t": 1.0, "line": "A goblin attacks!"}])
        self.assertEqual(data["tells"][0]["incoming"], True)

    def test_deadman_seconds_left_is_none_when_disabled(self):
        fake = self._fake_self(deadman_minutes=0)
        MudClient._write_webstate(fake, 1000.0)
        data, _ = paths.load_json(paths.webstate_file("3s-din"))
        self.assertIsNone(data["deadman_seconds_left"])

    def test_deadman_seconds_left_counts_down_from_the_limit(self):
        # 15m deadman, last manual input 100s ago -> 900 - 100 = 800 left.
        fake = self._fake_self(deadman_minutes=15, last_manual=900.0)
        MudClient._write_webstate(fake, 1000.0)
        data, _ = paths.load_json(paths.webstate_file("3s-din"))
        self.assertEqual(data["deadman_seconds_left"], 800.0)

    def test_vitals_snapshot_is_included_verbatim(self):
        fake = self._fake_self(vitals={"hp": 320, "hpmax": 400,
                                       "enemy": "a goblin",
                                       "enemycond": 64})
        MudClient._write_webstate(fake, 1000.0)
        data, _ = paths.load_json(paths.webstate_file("3s-din"))
        self.assertEqual(data["vitals"], {"hp": 320, "hpmax": 400,
                                          "enemy": "a goblin",
                                          "enemycond": 64})

    def test_daler_is_pulled_from_viking_state_when_present(self):
        fake = self._fake_self(viking_state={"DALER": 5383})
        MudClient._write_webstate(fake, 1000.0)
        data, _ = paths.load_json(paths.webstate_file("3s-din"))
        self.assertEqual(data["daler"], 5383)

    def test_daler_is_none_for_non_viking_characters(self):
        fake = self._fake_self(viking_state={})
        MudClient._write_webstate(fake, 1000.0)
        data, _ = paths.load_json(paths.webstate_file("3s-din"))
        self.assertIsNone(data["daler"])


if __name__ == "__main__":
    unittest.main()
