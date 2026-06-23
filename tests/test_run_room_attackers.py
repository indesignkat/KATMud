"""Unit tests for Run's room-attacker detection - the fix for Run walking
off mid-fight (e.g. the zelligars bot ignoring 'Stone Golem attacking
you!.'): _run_scan_mob only matches a whitelisted mob's bare name, so a
room-entry line where the mob already has the mud's '<Name> attacking
you!.' (optionally '<Name> [cond] attacking you!.') suffix never matches,
the room reads as clear, and Run advances before self.in_combat (the FFF
K/enemy field) has caught up. _run_room_attackers() catches that text
directly so Run holds movement for one settle interval instead.

MudClient.__init__ opens a real connection, so (like test_client_webstate)
these call the unbound methods against a MagicMock standing in for self.
"""
import time
import unittest
from unittest.mock import MagicMock

from katmud_lib.client import MudClient


class RunRoomAttackersTest(unittest.TestCase):
    def _fake_self(self, lines):
        fake = MagicMock()
        fake._run_lines = lines
        return fake

    def test_bare_attacking_line_matches(self):
        fake = self._fake_self(["Flesh Golem attacking you!."])
        self.assertEqual(MudClient._run_room_attackers(fake), ["Flesh Golem"])

    def test_condition_bracket_is_stripped(self):
        fake = self._fake_self(["Steel Golem [scratched] attacking you!."])
        self.assertEqual(MudClient._run_room_attackers(fake), ["Steel Golem"])

    def test_multiple_attackers_and_unrelated_lines(self):
        fake = self._fake_self([
            "Zelligar's Castle (e,n,se)",
            "Stone Golem attacking you!.",
            "Steel Golem [scratched] attacking you!.",
            "A magical sword.",
        ])
        self.assertEqual(MudClient._run_room_attackers(fake),
                         ["Stone Golem", "Steel Golem"])

    def test_no_attackers_returns_empty(self):
        fake = self._fake_self(["A preserved, melted corpse of Straw golem."])
        self.assertEqual(MudClient._run_room_attackers(fake), [])


class RunTickHoldsForAttackingRoomTextTest(unittest.TestCase):
    def _fake_self(self, lines, room_enter_t, engage_grace=4.0):
        fake = MagicMock()
        fake._run_job = None
        fake._run_on = True
        fake.conn.alive = True
        fake.deadman_tripped = False
        fake.in_combat = False
        fake._run_killing = False
        fake._run_combat_end = 0.0
        fake._post_kill_holding.return_value = False
        fake._run_postcombat_s.return_value = 2.0
        fake._run_acted = False
        fake._run_lines = lines
        fake._run_room_enter_t = room_enter_t
        fake._run_engage_grace_s.return_value = engage_grace
        fake._run_settle_s.return_value = 1.5
        fake._run_move_s.return_value = 1.3
        fake._run_room_blocked.return_value = None
        fake._run_scan_mob.return_value = (None, None)
        fake._run_idx = 0
        fake._run_path = ["n"]
        fake._run_loop = False
        # Bind the real helper under test so _run_tick's call to it runs
        # the actual logic, not a MagicMock stub.
        fake._run_room_attackers = MudClient._run_room_attackers.__get__(fake)
        return fake

    def test_attacking_line_holds_movement_within_grace(self):
        fake = self._fake_self(
            ["Stone Golem attacking you!."], room_enter_t=time.time())
        MudClient._run_tick(fake)
        fake._run_send_step.assert_not_called()
        self.assertEqual(fake._run_idx, 0)
        fake._run_reschedule.assert_called_once_with(1.5)

    def test_attacking_line_past_grace_window_proceeds(self):
        fake = self._fake_self(
            ["Stone Golem attacking you!."],
            room_enter_t=time.time() - 10, engage_grace=4.0)
        MudClient._run_tick(fake)
        fake._run_send_step.assert_called_once_with("n")
        self.assertEqual(fake._run_idx, 1)

    def test_no_attacking_line_proceeds_normally(self):
        fake = self._fake_self(["Zelligar's Castle (e,n,se)"],
                               room_enter_t=time.time())
        MudClient._run_tick(fake)
        fake._run_send_step.assert_called_once_with("n")
        self.assertEqual(fake._run_idx, 1)


if __name__ == "__main__":
    unittest.main()
