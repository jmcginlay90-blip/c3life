"""Tests for Ring-Out, the feedback watch (ringout.py), on made-up room sound: noise with a note that rings, and the
things that aren't feedback (a note that moves, a note that dies away). Then the cut on the Wing's Main 1 EQ, and undo.
Run: python3 -m unittest -v test_ringout.py"""

import json
import math
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import ringout as ro
import rta_live as rl
from test_wing import FakeWing

SP = rl.Spectrum()


def room(f=None, db=-20.0, noise=-45.0, seed=0):
    """One spectrum: noise, and a sine at f Hz (dBFS) if given."""
    rnd = random.Random(seed)
    a = 10 ** (db / 20) * math.sqrt(2) if f else 0.0
    nz = 10 ** (noise / 20)
    x = [(a * math.sin(2 * math.pi * f * i / rl.RATE) if f else 0.0) + rnd.gauss(0, nz) for i in range(rl.N)]
    return SP.powers(x)[0]


class Peaks(unittest.TestCase):
    def test_a_ringing_note_stands_out_and_its_pitch_is_found(self):
        p = room(2500.0)
        found = ro.peaks(p, SP.df, 15.0)
        top = max(found, key=lambda x: x[1])
        self.assertLess(abs(top[0] - 2500.0), 4.0)  # between bins: better than the 11.7 Hz bin spacing
        self.assertGreater(top[2], 20.0)
        self.assertEqual(ro.peaks(room(None), SP.df, 15.0), [])  # noise alone: nothing
        self.assertEqual(ro.peaks(room(50.0), SP.df, 15.0), [])  # below 80 Hz isn't looked at


class Watching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.w = ro.Watch(self.tmp, SP.df)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_rings_once_it_has_stood_long_enough(self):
        frames = [room(2500.0, seed=i) for i in range(8)]
        for p in frames[:6]:
            self.w.feed(p)
        self.assertEqual(self.w.ringing(), [])  # 1.2 s: not yet (normal needs 1.4 s)
        self.w.feed(frames[6])
        r = self.w.ringing()
        self.assertEqual(len(r), 1)
        self.assertLess(abs(r[0]["f"] - 2500.0), 5.0)
        self.assertEqual(len(self.w.view()["recent"]), 1)
        for i in range(7):  # it stops
            self.w.feed(room(None, seed=100 + i))
        self.assertEqual(self.w.ringing(), [])
        self.assertEqual(len(self.w.view()["recent"]), 1)  # still listed as rang lately

    def test_music_that_moves_or_dies_away_isnt_feedback(self):
        for i in range(12):  # a melody: a different note each time
            self.w.feed(room(400.0 * 2 ** (i / 6), seed=i))
        self.assertEqual(self.w.ringing(), [])
        for i in range(12):  # a held note dying away (6 dB a frame)
            self.w.feed(room(1000.0, db=-20.0 - 6 * i, seed=i))
        self.assertEqual(self.w.ringing(), [])

    def test_sensitivity_and_off(self):
        self.assertIsNone(self.w.settings(sens="low"))
        for i in range(9):
            self.w.feed(room(3150.0, seed=i))
        self.assertEqual(self.w.ringing(), [])  # low needs 2 s
        self.w.feed(room(3150.0, seed=9))
        self.assertEqual(len(self.w.ringing()), 1)
        self.assertIsNotNone(self.w.settings(sens="very"))
        self.w.settings(on=False)
        self.assertEqual(self.w.ringing(), [])
        self.assertEqual((ro.Watch(self.tmp, SP.df).on, ro.Watch(self.tmp, SP.df).sens), (False, "low"))  # kept


class FakeDesk:
    def __init__(self, eq):
        self.e, self.sets, self.fail = dict(eq), [], None

    def ask_eq(self, kind, n):
        pass

    def eq(self, kind, n):
        return dict(self.e)

    def set(self, kind, n, field, v):
        if self.fail:
            return self.fail
        self.sets.append((kind, n, field, v))
        self.e[field] = v
        return None


def main_eq(**kw):
    e = {"eq/on": 1, "eq/leq": "SHV", "eq/heq": "SHV", "eq/lg": 0.0, "eq/lf": 80.0, "eq/lq": 1.0, "eq/hg": 0.0, "eq/hf": 12000.0, "eq/hq": 1.0}
    for b, f in zip("123456", (100, 250, 630, 1600, 4000, 8000)):
        e.update({"eq/%sg" % b: 0.0, "eq/%sf" % b: float(f), "eq/%sq" % b: 2.0})
    e.update(kw)
    return e


class Cutting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.w = ro.Watch(self.tmp, SP.df)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_flat_band_from_the_top_then_deeper_then_undo(self):
        d = FakeDesk(main_eq(**{"eq/6g": -2.0}))  # FOH uses band 6: leave it alone
        err, c = self.w.cut(d, 2512.3, "FOH")
        self.assertIsNone(err)
        self.assertEqual((c["band"], c["f"], c["g"], c["q"]), ("5", 2512.3, -6.0, 8.0))
        self.assertEqual([x[2] for x in d.sets], ["eq/5f", "eq/5q", "eq/5g"])  # the frequency first
        err, c2 = self.w.cut(d, 2520.0, "FOH")  # the same note again: deeper
        self.assertEqual((err, c2["band"], c2["g"]), (None, "5", -9.0))
        self.w.cut(d, 2500.0, "FOH")
        self.assertIn("already cut by 12 dB", self.w.cut(d, 2500.0, "FOH")[0])
        self.assertEqual(ro.Watch(self.tmp, SP.df).view()["cuts"][0]["g"], -12.0)  # kept after a restart
        self.assertIsNone(self.w.undo(d, c["id"], "FOH"))
        self.assertEqual((d.e["eq/5g"], d.e["eq/5f"], d.e["eq/5q"]), (0.0, 4000.0, 2.0))  # back as it was
        self.assertEqual(self.w.view()["cuts"], [])

    def test_refused(self):
        self.assertIn("turned off", self.w.cut(FakeDesk(main_eq(**{"eq/on": 0})), 2500.0, "FOH")[0])
        busy = FakeDesk(main_eq(**{"eq/%sg" % b: -1.0 for b in "123456"}))
        self.assertIn("no flat EQ band", self.w.cut(busy, 2500.0, "FOH")[0])
        self.assertEqual(busy.sets, [])
        self.assertIn("80 Hz and 12 kHz", self.w.cut(FakeDesk(main_eq()), 20.0, "FOH")[0])
        watch = FakeDesk(main_eq()); watch.fail = "The app is set to watch the Wing only."
        self.assertEqual(self.w.cut(watch, 2500.0, "FOH")[0], "The app is set to watch the Wing only.")
        self.assertEqual(self.w.view()["cuts"], [])


class Api(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWing()
        for k, v in main_eq().items():
            self.fw.vals["/main/1/" + k] = v
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="",
                   wing_ip="127.0.0.1", wing_osc_port=self.fw.osc_port, wing_native_port=self.fw.native_port)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "ProPresenter"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            self.tok[n] = self.call("/api/login", "POST", body)[1]["token"]

    def tearDown(self):
        self.httpd.tio.close(); self.httpd.wing.close()
        self.httpd.shutdown(); self.httpd.server_close()
        self.fw.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_settings_cut_and_undo_on_the_wing(self):
        code, j = self.call("/api/rta/ringout", "PUT", {"sens": "high"}, who="FOH sound")
        self.assertEqual((code, j["sens"], j["target"]), (200, "high", "Main 1"))
        self.assertEqual(self.call("/api/rta/ringout", "PUT", {"sens": "high"}, who="ProPresenter")[0], 403)
        self.assertEqual(self.call("/api/rta/ringout/cut", "POST", {"f": 2500}, who="ProPresenter")[0], 403)
        for _ in range(40):
            if self.httpd.wing.status()["status"] == "connected":
                break
            time.sleep(0.1)
        code, j = self.call("/api/rta/ringout/cut", "POST", {"f": 2500}, who="FOH sound")
        self.assertEqual(code, 409); self.assertIn("watch the Wing only", j["error"])  # Watch only: nothing
        self.httpd.wing.configure(mode="control")
        code, j = self.call("/api/rta/ringout/cut", "POST", {"f": 2500}, who="FOH sound")
        self.assertEqual(code, 200, j)
        self.assertEqual((j["cut"]["band"], j["cut"]["g"], j["cuts"][0]["f"]), ("6", -6.0, 2500.0))
        for _ in range(40):
            if ("/main/1/eq/6g", -6.0) in self.fw.sets:
                break
            time.sleep(0.05)
        self.assertIn(("/main/1/eq/6f", 2500.0), self.fw.sets)
        self.assertIn(("/main/1/eq/6g", -6.0), self.fw.sets)
        code, j = self.call("/api/rta/ringout/undo", "POST", {"id": j["cut"]["id"]}, who="FOH sound")
        self.assertEqual((code, j["cuts"]), (200, []))
        for _ in range(40):
            if self.fw.sets[-1:] and ("/main/1/eq/6q", 2.0) in self.fw.sets:
                break
            time.sleep(0.05)
        self.assertIn(("/main/1/eq/6g", 0.0), self.fw.sets)
        self.assertIn(("/main/1/eq/6f", 8000.0), self.fw.sets)
        self.assertIn("ringout", self.call("/api/rta/live", who="FOH sound")[1])


if __name__ == "__main__":
    unittest.main()
