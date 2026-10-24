"""Tests for the daily RTA and activity reports (daily.py, /api/daily...).
Run: python3 -m unittest -v test_daily.py"""

import logging
import os
import shutil
import tempfile
import time
import unittest

import daily as dl


class RTA:
    def __init__(self):
        self.asked, self.dba = 0, 90.0

    def ask(self):
        self.asked += 1

    def state(self):
        return {"dba": self.dba, "dbz": self.dba + 6, "leq": {"1m": self.dba}, "calibrated": True, "mic": [50.0] * 120}


class Reports:
    def __init__(self):
        self.sent = []

    def add(self, b, who, files=None, kind="issue"):
        self.sent.append((b["subject"], b["text"], [f[0] for f in files or []], kind))


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = {"t": time.mktime(time.strptime("2026-10-25 10:30:00", "%Y-%m-%d %H:%M:%S"))}
        self.rta, self.rep = RTA(), Reports()
        self.log = logging.getLogger("daily-test")
        self.log.setLevel(logging.INFO)
        self.h = dl.ActivityLog(os.path.join(self.tmp, "activity"))  # only test 1 adds it: it stamps lines with the real clock
        self.d = dl.Daily(self.tmp, {}, self.log, rta=self.rta, reports=self.rep, now=lambda: self.t["t"])

    def tearDown(self):
        self.log.removeHandler(self.h)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def step(self, n, secs=10):
        for _ in range(n):
            self.t["t"] += secs
            self.d.tick()

    def test_logs_the_room_and_emails_both_reports(self):
        self.step(30)
        self.rta.dba = 101.0
        self.step(12)
        self.log.addHandler(self.h)
        self.log.info("macros: FOH ran Worship start")
        self.log.error("wing: no answer from 192.168.10.20")
        self.log.removeHandler(self.h)
        day = time.strftime("%Y-%m-%d")  # the activity log uses the real day
        self.assertGreater(self.rta.asked, 40)  # the RTA kept running while logging
        err, sent = self.d.send("2026-10-25", "FOH")
        self.assertIsNone(err)
        subj, text, files, kind = self.rep.sent[0]
        self.assertEqual((subj, kind, files), ("RTA report 2026-10-25", "daily", ["rta-2026-10-25.csv", "rta-2026-10-25-spectrum.csv"]))
        self.assertIn("highest 101.0 dB(A)", text)
        self.assertIn("over 100 dB(A): 2 min", text)
        self.assertIn("Service 10:00-12:00", text)
        err, _ = self.d.send(day, "FOH")
        act = [x for x in self.rep.sent if x[0].startswith("Activity")][0]
        errors = act[1].split("Everything, in order:")[0]
        self.assertIn("ERRORS", errors)
        self.assertIn("wing: no answer", errors)
        self.assertNotIn("Worship start", errors)  # not an error: only in the full list
        self.assertEqual(act[2], ["activity-%s.log" % day])
        self.assertEqual({x["day"] for x in self.d.view()["days"]}, {day, "2026-10-25"})

    def test_only_between_the_times_and_settings_checked(self):
        self.assertIsNone(self.d.set({"rta_mode": "times", "start": "11:00", "end": "12:00"}, "Admin"))
        self.step(5)
        self.assertEqual(self.rta.asked, 0)  # 10:30: not yet
        self.t["t"] += 1800
        self.step(1)
        self.assertEqual(self.rta.asked, 1)
        for bad in ({"start": "25:00"}, {"rta_mode": "sometimes"}, {"nope": 1}):
            self.assertEqual(self.d.set(bad, "Admin"), "Bad request")
        self.assertIn("Nothing was logged", self.d.send("2020-01-01", "FOH")[0])

    def test_a_day_not_sent_goes_the_next_morning(self):
        self.step(3)
        self.t["t"] += 86400
        self.d.catch_up()
        self.assertEqual(self.rep.sent[0][0], "RTA report 2026-10-25")
        self.assertIn("2026-10-25", self.d.settings["sent"])
        n = len(self.rep.sent)
        self.d.catch_up()
        self.assertEqual(len(self.rep.sent), n)  # once


if __name__ == "__main__":
    unittest.main()
