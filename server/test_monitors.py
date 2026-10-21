"""Tests for monitor mixes on the Wing: whose mix is whose, sends, the lock, and the Pre / pan link check.
Run: python3 -m unittest -v test_monitors.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import monitors as mm
from test_wing import FakeWing, wait


class Unit(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(mm.valid(mm.DEFAULT))
        self.assertIn("buses 1 to 16", mm.valid({"buses": [17], "people": {}, "locked": False}))
        self.assertIn("must be one of the monitor mixes", mm.valid({"buses": [1], "people": {"A": {"own": 2}}, "locked": False}))

    def test_who_may_change(self):
        m = mm.Monitors(tempfile.mkdtemp())
        self.assertEqual(m.mine("Keys (musician)"), [4, 1, 2])
        self.assertIsNone(m.may_change("Keys (musician)", 0, 4))
        self.assertIn("isn't one of your mixes", m.may_change("Somebody", 0, 4))
        self.assertIn("isn't a monitor mix", m.may_change("Keys (musician)", 0, 5))
        m.set_locked(True)
        self.assertIn("locked", m.may_change("Keys (musician)", 0, 4))
        self.assertIsNone(m.may_change("FOH sound", 2, 4))  # FOH can, even locked


class Api(unittest.TestCase):
    def setUp(self):
        self.f = FakeWing()
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="",
                   wing_ip="127.0.0.1", wing_osc_port=self.f.osc_port, wing_native_port=self.f.native_port)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "Keys (musician)", "Viewer"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "" if n == "Viewer" else "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]
        self.call("/api/wing/settings", "PUT", {"mode": "control"})
        self.assertTrue(wait(lambda: self.call("/api/wing")[1].get("status") == "connected"))

    def tearDown(self):
        self.httpd.wing.close()
        self.httpd.tio.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="Admin"):
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

    def test_musician_sees_only_their_mixes(self):
        code, d = self.call("/api/monitors", who="Keys (musician)")
        self.assertEqual((code, d["mine"], "people" in d), (200, [4, 1, 2], False))
        self.assertEqual(self.call("/api/wing/sends?bus=5", who="Keys (musician)")[0], 403)
        self.assertTrue(wait(lambda: self.call("/api/wing/sends?bus=4", who="Keys (musician)")[1]["sends"].get("1", {}).get("lvl") == -6.0))
        self.assertEqual(self.call("/api/monitors", who="Viewer")[0], 403)

    def test_musician_changes_their_own_mix_on_the_wing(self):
        self.call("/api/wing/sends?bus=4")
        self.assertEqual(self.call("/api/wing/send", "POST", {"bus": 4, "n": 2, "field": "lvl", "value": -3.0}, who="Keys (musician)")[0], 200)
        self.assertEqual(self.call("/api/wing/send", "POST", {"bus": 4, "n": 2, "field": "pan", "value": 30}, who="Keys (musician)")[0], 200)
        self.assertEqual(self.call("/api/wing/send", "POST", {"bus": 4, "n": 0, "field": "lvl", "value": -2.0}, who="Keys (musician)")[0], 200)  # their own volume
        self.assertTrue(wait(lambda: self.f.vals["/ch/2/send/4/lvl"] == -3.0 and self.f.vals["/ch/2/send/4/pan"] == 30.0 and self.f.vals["/bus/4/fdr"] == -2.0))
        code, d = self.call("/api/wing/send", "POST", {"bus": 5, "n": 2, "field": "lvl", "value": 0.0}, who="Keys (musician)")
        self.assertEqual(code, 403)  # bus 5 is the stream, not a monitor mix
        code, d = self.call("/api/wing/send", "POST", {"bus": 2, "n": 2, "field": "lvl", "value": 0.0}, who="FOH sound")
        self.assertEqual(code, 200)

    def test_a_musician_puts_the_rta_on_their_own_mix(self):
        # the Wing's RTA on a channel: a musician doesn't see it
        self.assertEqual(self.call("/api/wing/rta", "POST", {"n": 3, "tap": "POSTEQ"})[0], 200)
        self.assertTrue(wait(lambda: self.call("/api/wing/rta")[1].get("kind") == "ch"))
        code, r = self.call("/api/wing/rta", who="Keys (musician)")
        self.assertEqual((code, r["mine"], r["rta"]), (200, False, []))
        # their own mix (bus 4): yes, and then they see it; someone else's (bus 5): no
        self.assertEqual(self.call("/api/wing/rta", "POST", {"kind": "bus", "n": 5, "tap": "POSTEQ"}, who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/wing/rta", "POST", {"n": 3, "tap": "POSTEQ"}, who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/wing/rta", "POST", {"kind": "bus", "n": 4, "tap": "POSTEQ"}, who="Keys (musician)")[0], 200)
        self.assertTrue(wait(lambda: self.call("/api/wing/rta", who="Keys (musician)")[1].get("mine") and len(self.call("/api/wing/rta", who="Keys (musician)")[1]["rta"]) == 120))
        self.assertEqual(self.f.vals["/cfg/rta/rtasrc"], 51)
        # locked: they can't move it
        d = self.call("/api/monitors")[1]
        d["locked"] = True
        self.call("/api/monitors", "PUT", d)
        self.assertEqual(self.call("/api/wing/rta", "POST", {"kind": "bus", "n": 4, "tap": "PREEQ"}, who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/wing/rta", who="Viewer")[0], 403)

    def test_the_lock(self):
        d = self.call("/api/monitors")[1]
        d["locked"] = True
        self.assertEqual(self.call("/api/monitors", "PUT", d, who="FOH sound")[0], 200)
        code, r = self.call("/api/wing/send", "POST", {"bus": 4, "n": 2, "field": "lvl", "value": -3.0}, who="Keys (musician)")
        self.assertEqual(code, 403)
        self.assertIn("locked", r["error"])
        self.assertEqual(self.call("/api/monitors", "PUT", d, who="Keys (musician)")[0], 403)

    def test_check_and_fix(self):
        self.call("/api/monitors/check")
        self.assertTrue(wait(lambda: all(c["read"] == 40 for c in self.call("/api/monitors/check")[1]["buses"]) and {c["bus"]: c for c in self.call("/api/monitors/check")[1]["buses"]}[4]["linked"] == [1]))
        by = {c["bus"]: c for c in self.call("/api/monitors/check")[1]["buses"]}
        self.assertEqual((by[1]["post"], by[4]["linked"], by[2]["post"]), ([9, 10], [1], []))
        self.assertEqual(self.call("/api/monitors/fix", "POST", {}, who="Viewer")[0], 403)
        code, d = self.call("/api/monitors/fix", "POST", {}, who="FOH sound")
        self.assertEqual((code, d["changed"]), (200, 3))
        self.assertTrue(wait(lambda: self.f.vals["/ch/9/send/1/mode"] == "PRE" and self.f.vals["/ch/1/send/4/plink"] == 0))
        self.assertTrue(wait(lambda: all(not c["post"] and not c["linked"] for c in self.call("/api/monitors/check")[1]["buses"])))


if __name__ == "__main__":
    unittest.main()
