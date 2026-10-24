"""Tests for MD talk (mdtalk.py, /api/md...): going green from the Wing's channel, the hold, the words, the log.
Run: python3 -m unittest -v test_mdtalk.py"""

import logging
import shutil
import tempfile
import unittest

import mdtalk as mt


class Desk:
    KINDS = {"ch": 40}

    def __init__(self):
        self.lv, self.mute = None, 1

    def input_level(self, n):
        return self.lv if n == 12 else -90

    def state(self):
        return {"strips": {"ch": {"12": {"name": "MD Talkback", "mute": self.mute}}}}


class Listen:
    def __init__(self):
        self.lines, self.asked = [], []

    def config(self):
        return {"sources": [{"id": "md", "name": "MD mic", "captions": True}]}

    def captions(self, src, since):
        self.asked.append(since)
        return {"lines": [x for x in self.lines if x["id"] > since]}


class PP:
    def __init__(self):
        self.sent = []

    def _req(self, path, method="GET", body=None):
        self.sent.append((path, body))


class MD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = {"t": 1000.0}
        self.desk, self.listen, self.pp = Desk(), Listen(), PP()
        self.m = mt.MDTalk(self.tmp, {}, logging.getLogger("test"), self.desk, listen=self.listen, pp=self.pp, now=lambda: self.t["t"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def step(self, secs, n=1):
        for _ in range(n):
            self.t["t"] += secs
            self.m.tick()

    def test_speaking_over_the_threshold_with_a_hold_and_the_words(self):
        self.assertIsNone(self.m.set({"source": "md", "stage": True}, "FOH"))
        self.step(0.2)
        self.assertFalse(self.m.live()["talking"])  # no meters: never green
        self.desk.lv = -20
        self.step(0.2)
        self.assertTrue(self.m.live()["talking"])
        self.assertEqual(self.pp.sent[-1], ("/v1/stage/message", "MD is talking"))
        self.listen.lines.append({"id": 1, "text": "go round the chorus again"})
        self.desk.lv = -50
        self.step(0.2, 5)
        self.assertTrue(self.m.live()["talking"])  # held between words
        self.step(0.2, 6)
        self.assertFalse(self.m.live()["talking"])
        self.assertEqual(self.pp.sent[-1], ("/v1/stage/message", ""))
        ln = self.m.view()["lines"][0]
        self.assertEqual((ln["ch"], ln["text"], ln["words"]), ("Ch 12 · MD Talkback", "go round the chorus again", True))

    def test_unmuted_mode_test_and_keeping_the_log(self):
        self.assertIsNone(self.m.set({"mode": 0, "keep": "report"}, "FOH"))
        self.desk.mute = 0
        self.step(0.2)
        self.assertTrue(self.m.live()["talking"])
        self.desk.mute = 1
        self.step(0.2, 10)
        self.assertEqual(len(self.m.view()["lines"]), 1)
        again = mt.MDTalk(self.tmp, {}, logging.getLogger("test"), self.desk)
        self.assertEqual(len(again.view()["lines"]), 1)  # kept for the report
        self.m.test("FOH")
        self.assertEqual((self.m.live()["talking"], self.m.live()["test"]), (True, True))
        self.m.clear("FOH")
        self.assertEqual(self.m.view()["lines"], [])
        for bad in ({"ch": 99}, {"mode": 2}, {"thresh": 5}, {"keep": "forever"}, {"nope": 1}):
            self.assertEqual(self.m.set(bad, "FOH"), "Bad request")


if __name__ == "__main__":
    unittest.main()
