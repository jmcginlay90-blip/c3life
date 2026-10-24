"""Tests for the warning on every screen: what's set up, and whether it's answering (health.py, GET /api/devices).
Run: python3 -m unittest -v test_health.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import health as hm


class Dev:
    NAME = "Behringer Wing"

    def __init__(self, status="connected", ip="192.168.10.20"):
        self.st, self.ip = status, ip

    def status(self):
        return {"status": self.st, "ip": self.ip, "mode": "watch"}


class Helpers:
    def __init__(self):
        self.list = [{"id": "m1", "name": "Mac mini", "address": "10.0.0.5:8099", "paired": True, "ok": True},
                     {"id": "w1", "name": "Foyer PC", "address": "10.0.0.6:8099", "paired": False, "ok": None}]
        self.checked = 0

    def view(self):
        return [dict(h) for h in self.list]

    def check(self):
        self.checked += 1
        return ["the C3 Life helper on " + h["name"] for h in self.list if h["paired"] and h["ok"] is False]


def pp_server(code=200):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = b'{"name": "ProPresenter"}'
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    h = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    return h


class Devices(unittest.TestCase):
    def test_only_whats_set_up_and_not_a_blip(self):
        clock = {"t": 1000.0}
        wing, tio, hs = Dev(), Dev("off"), Helpers()
        h = hm.Health({}, wing, tio, hs, now=lambda: clock["t"])
        v = {d["id"]: d for d in h.devices()}
        self.assertEqual(sorted(v), ["desk", "helper:m1"])  # the Tio is off, the Foyer PC has no key, no ProPresenter set
        self.assertEqual((v["desk"]["name"], v["desk"]["ok"], v["desk"]["warn"]), ("The Wing", True, False))
        wing.st = "no answer"
        self.assertFalse({d["id"]: d for d in h.devices()}["desk"]["warn"])  # quiet, but only just
        clock["t"] += hm.GRACE
        d = {d["id"]: d for d in h.devices()}["desk"]
        self.assertEqual((d["ok"], d["warn"], d["since"], d["where"]), (False, True, 1000.0, "Wing.dc.html"))
        self.assertIn("192.168.10.20", d["why"])
        wing.st = "connected"
        self.assertFalse({d["id"]: d for d in h.devices()}["desk"]["warn"])
        tio.st = "no address"
        self.assertNotIn("tio", {d["id"] for d in h.devices()})

    def test_propresenter_and_the_helpers_are_asked(self):
        hs = Helpers()
        pp = pp_server()
        try:
            h = hm.Health({"propresenter": "http://127.0.0.1:%d" % pp.server_address[1]}, None, None, hs)
            self.assertNotIn("propresenter", {d["id"] for d in h.devices()})  # not asked yet: nothing to say
            self.assertEqual(h.check(), [])
            self.assertTrue({d["id"]: d for d in h.devices()}["propresenter"]["ok"])
            hs.list[0]["ok"] = False
        finally:
            pp.shutdown(); pp.server_close()
        self.assertEqual(h.check(), ["ProPresenter", "the C3 Life helper on Mac mini"])
        v = {d["id"]: d for d in h.devices()}
        self.assertEqual((v["propresenter"]["ok"], v["helper:m1"]["ok"], v["helper:m1"]["name"]), (False, False, "The C3 Life helper on Mac mini"))
        self.assertEqual(hs.checked, 2)


class Lightkey(unittest.TestCase):
    def test_lightkey_counts_as_there_if_the_last_scan_saw_it(self):
        L = type("L", (), {"settings": {"ip": "10.0.0.32"}, "seen": None})()
        h = hm.Health({}, None, None, None, lights=L, now=lambda: 0)
        self.assertEqual(h.devices(), [])  # no scan yet
        L.seen = False
        d = h.devices()[0]
        self.assertEqual((d["id"], d["ok"], d["area"], d["where"]), ("lightkey", False, "Lights", "Lights.dc.html"))
        L.settings = {"ip": ""}
        self.assertEqual(h.devices(), [])


class LeftOut(unittest.TestCase):
    def test_left_out_and_events_mode(self):
        tmp = tempfile.mkdtemp()
        try:
            clock = {"t": 1000.0}
            h = hm.Health({"data_dir": tmp}, Dev("no answer"), None, None, now=lambda: clock["t"])
            h.devices()
            clock["t"] += 60
            self.assertTrue(h.devices()[0]["warn"])
            self.assertIsNone(h.set_prefs(left=["desk"]))
            d = h.devices()[0]
            self.assertEqual((d["left"], d["warn"]), (True, False))  # left out: no warning
            self.assertIsNone(h.set_prefs(left=[], events=["desk"]))
            self.assertTrue(h.devices()[0]["left"])  # events only, and events mode is off
            self.assertIsNone(h.set_prefs(events_mode=True))
            self.assertEqual(h.left_out(), set())
            self.assertEqual(h.set_prefs(left="desk"), "Bad request")
            h2 = hm.Health({"data_dir": tmp}, Dev(), None, None)  # kept
            self.assertEqual((h2.prefs["events"], h2.prefs["events_mode"]), (["desk"], True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.httpd.health.desk = Dev("no answer")
        self.httpd.health.cfg = {"propresenter": "http://10.0.0.30:50001"}
        self.httpd.health.pp = False
        self.clock = {"t": 1000.0}
        self.httpd.health.now = lambda: self.clock["t"]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, who):
        tok = None
        if who:
            body = {"name": who, "password": "c3life-admin"} if who == "Admin" else {"name": who, "pin": "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                tok = json.loads(r.read())["token"]
        req = urllib.request.Request("http://127.0.0.1:%d/api/devices" % self.port, headers={"Authorization": "Bearer " + tok} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_each_role_hears_about_what_it_can_see(self):
        self.assertEqual([d["id"] for d in self.get("Admin")[1]["devices"] if d["warn"]], [])  # only just gone quiet
        self.clock["t"] += 20
        code, j = self.get("Admin")
        self.assertEqual((code, sorted(d["id"] for d in j["devices"] if d["warn"])), (200, ["desk", "propresenter"]))
        self.assertEqual(sorted(d["id"] for d in self.get("Keys (musician)")[1]["devices"]), ["desk"])  # their in-ears
        self.assertEqual(sorted(d["id"] for d in self.get("ProPresenter")[1]["devices"]), ["propresenter"])
        self.assertEqual(self.get("Foyer team")[1]["devices"], [])
        self.assertEqual(self.get(None)[0], 401)


if __name__ == "__main__":
    unittest.main()
