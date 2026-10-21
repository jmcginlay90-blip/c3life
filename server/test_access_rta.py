"""Tests for the RTA settings access level, and for access levels saved before it existed.
Run: python3 -m unittest -v test_access_rta.py"""

import json
import os
import shutil
import tempfile
import unittest

import access as am


class RtaSettingsArea(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_is_an_area_after_the_others(self):
        self.assertEqual(am.AREAS[-3:], ["RTA settings", "Tap EQ", "Music"])
        self.assertEqual(am.AREAS[am.LISTEN], "Listen")
        a = am.Access(self.tmp)
        self.assertEqual([a.level(r, "RTA settings") for r in ("Admin", "FOH sound", "ProPresenter", "Viewer", "Musician", "Listener")], [2, 2, 1, 1, 0, 0])

    def test_saved_before_it_existed_keeps_every_level(self):
        old = am.AREAS[:-3]  # saved before RTA settings, Tap EQ and Music
        roles = [dict(r, p=r["p"][:-3]) for r in am.DEFAULT_ROLES]
        next(r for r in roles if r["name"] == "Viewer")["p"][old.index("RTA")] = 0  # someone changed one
        with open(os.path.join(self.tmp, "access.json"), "w") as f:
            json.dump({"areas": old, "roles": roles}, f)
        a = am.Access(self.tmp)
        self.assertEqual(a.level("Viewer", "RTA"), 0)  # their change is kept
        self.assertEqual(a.level("FOH sound", "RTA settings"), 2)  # and the new area gets its usual level
        self.assertEqual(a.level("Foyer team", "RTA settings"), 0)
        self.assertEqual(a.level("Listener", "Listen"), 1)
        self.assertEqual([a.level(r, "Tap EQ") for r in ("FOH sound", "Musician", "Viewer", "Lighting")], [2, 1, 0, 0])
        self.assertEqual([a.level(r, "Music") for r in ("Admin", "FOH sound", "ProPresenter", "Lighting", "Viewer", "Foyer team", "Listener")], [2, 2, 2, 1, 1, 0, 0])

    def test_listen_control_is_still_admin_only(self):
        a = am.Access(self.tmp)
        roles = [dict(r, p=list(r["p"])) for r in am.DEFAULT_ROLES]
        next(r for r in roles if r["name"] == "FOH sound")["p"][am.LISTEN] = 2
        self.assertEqual(a.save(roles), "")
        self.assertEqual(a.level("FOH sound", "Listen"), 1)
        self.assertEqual(a.level("FOH sound", "RTA settings"), 2)  # the last area isn't mistaken for Listen


if __name__ == "__main__":
    unittest.main()
