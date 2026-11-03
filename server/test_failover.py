"""The backup Mac button: Wing Dante inputs move from the main Playback Mac to the backup one, with a 10-second undo."""

import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import failover  # noqa: E402

MAIN, BACKUP, WING = "C3-LIfe-Multitracks", "C3-Life-Backup", "wing-d-2627f4"


class FakeDante:
    def __init__(self):
        self.routes = [{"rx_device": WING, "rx_channel": "%02d" % (20 + n), "tx_device": MAIN, "tx_channel": "%02d" % n} for n in (1, 2, 3)]
        self.routes += [{"rx_device": WING, "rx_channel": "01", "tx_device": "Y001-box-1", "tx_channel": "01"},   # a mic: never touched
                        {"rx_device": "C3LIFE", "rx_channel": "40", "tx_device": MAIN, "tx_channel": "01"}]       # recording: not the Wing
        self.chans = {MAIN: {"tx": {1: "01", 2: "02", 3: "03"}, "rx": {}},
                      BACKUP: {"tx": {1: "01", 2: "02", 3: "03"}, "rx": {}},
                      WING: {"tx": {}, "rx": {21: "21", 22: "22", 23: "23"}}}
        self.calls, self.fail = [], set()

    def all_routes(self):
        return [dict(r) for r in self.routes]

    def channels(self):
        return self.chans

    def route_any(self, rx_device, rx_channel, tx_device, tx_channel):
        self.calls.append((rx_device, rx_channel, tx_device, tx_channel))
        if rx_channel in self.fail:
            return "Dante didn't take it: timeout"
        for r in self.routes:
            if r["rx_device"] == rx_device and r["rx_channel"] == rx_channel:
                r.update(tx_device=tx_device, tx_channel=tx_channel)
        return None

    def wing(self):
        return {r["rx_channel"]: (r["tx_device"], r["tx_channel"]) for r in self.routes if r["rx_device"] == WING}


class Desk:
    def __init__(self, mode="control"):
        self.settings = {"mode": mode}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dante, self.clock = FakeDante(), Clock()
        self.f = failover.Failover(self.tmp, {}, logging.getLogger("t"), self.dante, desk=Desk(), clock=self.clock)
        self.assertIsNone(self.f.save(MAIN, BACKUP, "Jonathan"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_moves_only_the_main_macs_wing_inputs(self):
        self.assertIsNone(self.f.switch("backup", "Jonathan", wait=True))
        w = self.dante.wing()
        self.assertEqual(w["21"], (BACKUP, "01"))
        self.assertEqual(w["23"], (BACKUP, "03"))
        self.assertEqual(w["01"], ("Y001-box-1", "01"))
        self.assertEqual(next(r for r in self.dante.routes if r["rx_device"] == "C3LIFE")["tx_device"], MAIN)
        v = self.f.view()
        self.assertEqual(v["on"], "backup")
        self.assertEqual(v["undo_left"], 10)
        self.assertEqual(v["last"]["done"], 3)

    def test_undo_within_10_seconds_puts_back_what_was_there(self):
        before = self.dante.wing()
        self.f.switch("backup", "Jonathan", wait=True)
        self.clock.t += 9
        self.assertEqual(self.f.view()["undo_left"], 1)
        self.assertIsNone(self.f.undo("Jonathan"))
        self.assertEqual(self.dante.wing(), before)
        self.assertEqual(self.f.view()["undo_left"], 0)
        self.assertIn("too late", self.f.undo("Jonathan"))

    def test_no_undo_after_10_seconds_but_switch_back_works(self):
        self.f.switch("backup", "Jonathan", wait=True)
        self.clock.t += 11
        self.assertEqual(self.f.view()["undo_left"], 0)
        self.assertIn("too late", self.f.undo("Jonathan"))
        self.assertIsNone(self.f.switch("main", "Jonathan", wait=True))
        self.assertEqual(self.dante.wing()["22"], (MAIN, "02"))

    def test_matches_by_name_then_by_number(self):
        self.dante.chans[BACKUP]["tx"] = {1: "Click", 2: "Guide", 3: "Tracks L"}
        self.dante.chans[MAIN]["tx"] = {1: "Click", 2: "Guide", 3: "03"}
        self.dante.routes[0]["tx_channel"], self.dante.routes[1]["tx_channel"] = "Guide", "Click"
        self.f.switch("backup", "J", wait=True)
        w = self.dante.wing()
        self.assertEqual(w["21"], (BACKUP, "Guide"))
        self.assertEqual(w["22"], (BACKUP, "Click"))
        self.assertEqual(w["23"], (BACKUP, "Tracks L"))

    def test_watch_only_changes_nothing(self):
        self.f.desk = Desk("watch")
        self.assertIn("Watch only", self.f.switch("backup", "J", wait=True))
        self.assertEqual(self.dante.calls, [])

    def test_backup_not_on_dante(self):
        del self.dante.chans[BACKUP]
        self.assertIsNone(self.f.switch("backup", "J", wait=True))
        self.assertIn("can't see", self.f.view()["last"]["error"])
        self.assertEqual(self.dante.calls, [])

    def test_one_failure_still_switches_the_rest(self):
        self.dante.fail = {"22"}
        self.f.switch("backup", "J", wait=True)
        v = self.f.view()
        self.assertEqual(v["last"]["done"], 2)
        self.assertEqual(len(v["last"]["failed"]), 1)
        self.assertEqual(self.dante.wing()["23"], (BACKUP, "03"))

    def test_needs_both_macs(self):
        f = failover.Failover(tempfile.mkdtemp(dir=self.tmp), {}, logging.getLogger("t"), self.dante, desk=Desk(), clock=self.clock)
        self.assertIn("Choose", f.save(MAIN, "", "J"))
        self.assertIn("different", f.save(MAIN, MAIN.upper(), "J"))
        f.switch("backup", "J", wait=True)
        self.assertIn("Choose the main and backup", f.view()["last"]["error"])

    def test_settings_are_kept(self):
        f = failover.Failover(self.tmp, {}, logging.getLogger("t"), self.dante)
        self.assertEqual((f.settings["main"], f.settings["backup"]), (MAIN, BACKUP))


if __name__ == "__main__":
    unittest.main()
