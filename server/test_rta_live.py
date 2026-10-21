"""Tests for the RTA heard by the server: the spectrum maths with exact tones, then ffmpeg on a generated signal standing
in for Dante Virtual Soundcard (channel 1: 1 kHz at -20 dBFS, the room mic; channel 2: 250 Hz, the desk reference).
Run: python3 -m unittest -v test_rta_live.py   (the ffmpeg ones need ffmpeg)"""

import json
import math
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import logging

import c3life_server as s
import rta_inputs as ri
import rta_live as rl

FFMPEG = shutil.which("ffmpeg")
SIGNAL = ["-re", "-f", "lavfi", "-i", "aevalsrc=0.1*sin(1000*2*PI*t)|0.3*sin(250*2*PI*t)|0|0.05*sin(5000*2*PI*t):s=48000:c=quad"]


def tone(f, amp=1.0, n=rl.N, rate=rl.RATE):
    return [amp * math.sin(2 * math.pi * f * i / rate) for i in range(n)]


def wait(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class Maths(unittest.TestCase):
    sp = rl.Spectrum()

    def band_of(self, f):
        return round(12 * math.log2(f / 20))

    def test_a_full_scale_sine_is_0_dbfs_in_its_band(self):
        b = self.sp.to_bands(self.sp.powers(tone(1000))[0])
        self.assertAlmostEqual(rl.db(sum(b)), 0.0, delta=0.2)
        self.assertIn(b.index(max(b)), (self.band_of(1000) - 1, self.band_of(1000), self.band_of(1000) + 1))

    def test_minus_20_dbfs(self):
        b = self.sp.to_bands(self.sp.powers(tone(1000, 0.1))[0])
        self.assertAlmostEqual(rl.db(sum(b)), -20.0, delta=0.2)

    def test_two_channels_at_once_stay_apart(self):
        px, py = self.sp.powers(tone(1000, 0.5), tone(100, 0.5))
        bx, by = self.sp.to_bands(px), self.sp.to_bands(py)
        self.assertEqual(abs(bx.index(max(bx)) - self.band_of(1000)) <= 1, True)
        self.assertEqual(abs(by.index(max(by)) - self.band_of(100)) <= 1, True)
        self.assertLess(rl.db(by[self.band_of(1000)]), -80)  # the 100 Hz channel has nothing at 1 kHz
        self.assertAlmostEqual(rl.db(sum(by)), rl.db(0.25), delta=0.3)

    def test_the_low_end_finds_the_right_band(self):
        b = self.sp.to_bands(self.sp.powers(tone(50))[0])
        self.assertLessEqual(abs(b.index(max(b)) - self.band_of(50)), 2)

    def test_a_weighting(self):
        self.assertAlmostEqual(rl.a_weight(1000), 0.0, delta=0.1)
        self.assertAlmostEqual(rl.a_weight(100), -19.1, delta=0.2)
        self.assertAlmostEqual(rl.a_weight(10000), -2.5, delta=0.2)

    def test_fast_enough(self):
        t = time.time()
        for _ in range(5):
            self.sp.to_bands(self.sp.powers(tone(1000), tone(200))[0])
        self.assertLess((time.time() - t) / 5, 0.15)  # each two-channel spectrum; five a second


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inputs = ri.RtaInputs(self.tmp)
        self.cfg = {"listen_input": SIGNAL}
        self.a = rl.Analyser(self.tmp, self.cfg, logging.getLogger("test"), self.inputs)

    def tearDown(self):
        self.a.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hears_the_mic_and_the_reference(self):
        self.a.ask()
        self.assertTrue(wait(lambda: self.a.state()["status"] == "live" and "ref" in self.a.state()))
        time.sleep(1.2)
        st = self.a.state()
        mic, ref = st["mic"], st["ref"]
        self.assertEqual(len(mic), 120)
        self.assertLessEqual(abs(mic.index(max(mic)) - round(12 * math.log2(1000 / 20))), 1)
        self.assertLessEqual(abs(ref.index(max(ref)) - round(12 * math.log2(250 / 20))), 1)
        # -20 dBFS, and not calibrated yet: 0 dBFS = 120 dB SPL
        self.assertAlmostEqual(st["dbz"], 100.0, delta=0.6)
        self.assertAlmostEqual(st["dba"], 100.0, delta=0.6)
        self.assertFalse(st["calibrated"])
        self.assertEqual(st["source"], "DVS 1")
        self.assertIsNotNone(st["leq"]["1m"])
        self.assertAlmostEqual(st["leq"]["1m"], 100.0, delta=1.0)

    def test_calibrate_with_a_94_db_calibrator(self):
        ok, msg = self.a.calibrate(94)
        self.assertFalse(ok)  # not listening yet
        self.a.ask()
        self.assertTrue(wait(lambda: self.a.state()["status"] == "live"))
        time.sleep(1.0)
        ok, msg = self.a.calibrate(94)
        self.assertTrue(ok, msg)
        time.sleep(0.6)
        st = self.a.state()
        self.assertTrue(st["calibrated"])
        self.assertAlmostEqual(st["dbz"], 94.0, delta=0.6)
        with open(os.path.join(self.tmp, "rta_live.json")) as f:
            self.assertEqual(json.load(f)["how"], "94 dB calibrator")
        self.assertAlmostEqual(rl.Analyser(self.tmp, self.cfg, logging.getLogger("test"), self.inputs).cal["offset"], st["cal"]["offset"])
        self.a.uncalibrate()
        time.sleep(0.4)
        self.assertFalse(self.a.state()["calibrated"])
        self.assertAlmostEqual(self.a.state()["dbz"], 100.0, delta=0.6)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rta_live.json")))

    def test_stereo_mic_and_subs(self):
        d = self.inputs.get()
        d["mic"] = {"from": "LCL/8", "via": "dvs", "ch": 1, "stereo": True, "from2": "LCL/7", "ch2": 4}
        d["ref"] = {"from": "MAIN/1", "via": "dvs", "ch": 2, "subs": {"on": True, "from": "MAIN/2", "ch": 3, "from2": "MAIN/2/R", "ch2": 5}}
        self.inputs.save(d)
        self.a.ask()
        self.assertTrue(wait(lambda: self.a.state()["status"] == "live" and "ref" in self.a.state()))
        time.sleep(1.0)
        mic = self.a.state()["mic"]
        # the mean of left (1 kHz) and right (5 kHz): both there
        for f in (1000, 5000):
            self.assertGreater(mic[round(12 * math.log2(f / 20))], 80)
        self.assertEqual(self.a.state()["source"], "DVS 1 and 4")

    def test_silence_says_so(self):
        d = self.inputs.get()
        d["mic"] = {"from": "LCL/8", "via": "dvs", "ch": 3}
        self.inputs.save(d)
        self.a.ask()
        self.assertTrue(wait(lambda: self.a.state()["status"] == "silent"))
        self.assertIn("No sound on DVS 3", self.a.state()["reason"])

    def test_changing_the_channel_listens_again(self):
        self.a.ask()
        self.assertTrue(wait(lambda: self.a.state()["status"] == "live"))
        d = self.inputs.get()
        d["mic"] = {"from": "LCL/8", "via": "dvs", "ch": 3}
        self.inputs.save(d)
        self.assertTrue(wait(lambda: self.a.state()["status"] == "silent"))

    def test_stops_when_nobody_looks(self):
        old = rl.IDLE
        rl.IDLE = 1
        try:
            self.a.ask()
            self.assertTrue(wait(lambda: self.a.state()["status"] == "live"))
            self.assertTrue(wait(lambda: self.a.state()["status"] == "off", 6))
        finally:
            rl.IDLE = old


class NoFfmpeg(unittest.TestCase):
    def test_says_ffmpeg_is_missing(self):
        tmp = tempfile.mkdtemp()
        try:
            a = rl.Analyser(tmp, {"ffmpeg": os.path.join(tmp, "no-ffmpeg")}, logging.getLogger("test"), ri.RtaInputs(tmp))
            a.ask()
            self.assertTrue(wait(lambda: a.state()["status"] == "error", 4))
            self.assertIn("ffmpeg isn't installed", a.state()["reason"])
            a.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", wing_ip="", tio_ip="", listen_input=SIGNAL)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("FOH sound", "Viewer", "Keys (musician)"):
            body = {"name": n, "pin": "" if n == "Viewer" else "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="FOH sound"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_may_look_and_calibrate(self):
        self.assertEqual(self.call("/api/rta/live", who="Keys (musician)")[0], 403)  # musicians: no RTA
        st, j = self.call("/api/rta/live", who="Viewer")
        self.assertEqual(st, 200)
        self.assertTrue(wait(lambda: self.call("/api/rta/live", who="Viewer")[1]["status"] == "live"))
        time.sleep(1.0)
        self.assertEqual(self.call("/api/rta/calibrate", "POST", {"spl": 94}, who="Viewer")[0], 403)  # RTA settings: View
        self.assertEqual(self.call("/api/rta/calibrate", "POST", {"spl": 300})[0], 400)
        st, j = self.call("/api/rta/calibrate", "POST", {"spl": 85, "weight": "A"})
        self.assertEqual((st, j["ok"], j["calibrated"]), (200, True, True))
        self.assertAlmostEqual(j["dba"], 85.0, delta=0.8)


if __name__ == "__main__":
    unittest.main()
