"""Unit tests for katmud_lib.necro.format_status_bar - the compact
Status[w/p/v/r] Cr[...] string shown on the necro status bar (replacing
the connection text), kept as a pure function over self.vars-shaped dicts
so it's testable without spinning up the Tkinter client.
"""
import unittest

from katmud_lib import necro


class FormatStatusBarTest(unittest.TestCase):
    def test_full_precap(self):
        v = {"worth": 125, "protection": True, "veil": True, "reset": 96,
             "circle": "64%"}
        self.assertEqual(necro.format_status_bar(v),
                          "Status[w125%|pON|vON|r96%] Cr[64%]")

    def test_postcap_uses_tier_pct(self):
        v = {"worth": 125, "protection": False, "veil": True, "reset": 0,
             "circle": "you are miles from advancement.",
             "tier": {"idx": 0, "total": 15, "pct": 0}}
        self.assertEqual(necro.format_status_bar(v),
                          "Status[w125%|pOFF|vON|r0%] Cr[0%]")

    def test_partial_data(self):
        self.assertEqual(necro.format_status_bar({"worth": 125}),
                          "Status[w125%]")

    def test_no_data(self):
        self.assertEqual(necro.format_status_bar({}), "")

    def test_circle_only(self):
        self.assertEqual(necro.format_status_bar({"circle": "64%"}),
                          "Cr[64%]")


if __name__ == "__main__":
    unittest.main()
