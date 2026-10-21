"""Tests for renaming Dante devices and channels without losing routes, against the pretend netaudio.
Run: python3 -m unittest -v test_dante_names.py"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s

HERE = os.path.dirname(os.path.abspath(__file__))


class Rename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "dante.json")
        num = lambda c: {str(n): "%02d" % n for n in range(1, c + 1)}
        with open(self.state, "w") as f:
            json.dump({"tx": {"WING-ABC123": dict(num(64), **{"8": "RTA Mic", "33": "FOH L"}), "Tio1608-D-1a2b": num(16), "C3LIFE": num(2)},
                       "rx": {"WING-ABC123": num(64), "Tio1608-D-1a2b": num(8), "C3LIFE": num(64)},
                       "subs": [{"rx_device": "C3LIFE", "rx_channel": "01", "rx_channel_number": 1, "tx_device": "WING-ABC123", "tx_channel": "RTA Mic"},
                                {"rx_device": "C3LIFE", "rx_channel": "04", "rx_channel_number": 4, "tx_device": "WING-ABC123", "tx_channel": "FOH L"},
                                {"rx_device": "WING-ABC123", "rx_channel": "01", "rx_channel_number": 1, "tx_device": "Tio1608-D-1a2b", "tx_channel": "01"},
                                {"rx_device": "Tio1608-D-1a2b", "rx_channel": "01", "rx_channel_number": 1, "tx_device": "WING-ABC123", "tx_channel": "01"}]}, f)
        os.environ["FAKE_DANTE"] = self.state
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", dante_dvs_name="C3LIFE",
                   netaudio=[sys.executable, os.path.join(HERE, "fake_netaudio.py")])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "Foyer team"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="Admin"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def dante(self):
        with open(self.state) as f:
            return json.load(f)

    def test_devices_and_who_is_who(self):
        self.assertEqual(self.call("/api/dante/devices", who="Foyer team")[0], 403)
        st, j = self.call("/api/dante/devices", who="FOH sound")
        d = {x["name"]: x for x in j["devices"]}
        self.assertEqual((d["WING-ABC123"]["is"], d["Tio1608-D-1a2b"]["is"], d["C3LIFE"]["is"]), ("wing", "tio", "dvs"))
        self.assertEqual(d["WING-ABC123"]["tx"]["33"], "FOH L")

    def test_renaming_the_wing_keeps_its_routes(self):
        st, c = self.call("/api/dante/rename/check", "POST", {"device": "WING-ABC123", "new": "Wing"}, who="FOH sound")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r["text"] for r in c["routes"]), ["C3LIFE · 01 ← WING-ABC123 · RTA Mic", "C3LIFE · 04 ← WING-ABC123 · FOH L", "Tio1608-D-1a2b · 01 ← WING-ABC123 · 01"])
        self.assertIn("This is the Wing's Dante card", c["notes"][0])
        self.assertEqual(self.call("/api/dante/rename", "POST", {"device": "WING-ABC123", "new": "Wing", "id": c["id"]}, who="FOH sound")[0], 403)  # Dante control: Admin
        self.assertEqual(self.call("/api/dante/rename", "POST", {"device": "WING-ABC123", "new": "Wing", "id": "stale"})[0], 409)
        st, j = self.call("/api/dante/rename", "POST", {"device": "WING-ABC123", "new": "Wing", "id": c["id"]})
        self.assertTrue(j["ok"], j)
        self.assertEqual(len(j["results"]), 4)
        d = self.dante()
        self.assertIn("Wing", d["tx"])
        live = [x for i, x in enumerate(d["subs"]) if i not in d["broken"]]
        self.assertIn({"rx_device": "C3LIFE", "rx_channel": "04", "rx_channel_number": 4, "tx_device": "Wing", "tx_channel": "FOH L"}, live)
        self.assertFalse(any(x["tx_device"] == "WING-ABC123" for x in live))
        self.assertEqual(self.cfg["dante_wing_name"], "Wing")  # C3 Life finds it by its new name
        with open(os.path.join(self.tmp, "data", "dante.json")) as f:
            self.assertEqual(json.load(f)["wing_name"], "Wing")

    def test_renaming_a_channel(self):
        st, c = self.call("/api/dante/rename/check", "POST", {"device": "WING-ABC123", "kind": "tx", "number": 33, "new": "Main L"})
        self.assertEqual([r["text"] for r in c["routes"]], ["C3LIFE · 04 ← WING-ABC123 · FOH L"])
        st, j = self.call("/api/dante/rename", "POST", {"device": "WING-ABC123", "kind": "tx", "number": 33, "new": "Main L", "id": c["id"]})
        self.assertTrue(j["ok"], j)
        d = self.dante()
        self.assertEqual(d["tx"]["WING-ABC123"]["33"], "Main L")
        self.assertIn({"rx_device": "C3LIFE", "rx_channel": "04", "rx_channel_number": 4, "tx_device": "WING-ABC123", "tx_channel": "Main L"},
                      [x for i, x in enumerate(d["subs"]) if i not in d["broken"]])
        st, c = self.call("/api/dante/rename/check", "POST", {"device": "C3LIFE", "kind": "rx", "number": 1, "new": "Room mic"})
        self.assertEqual((c["routes"], c["notes"]), ([], ["Routes don't use receive channel names, so nothing else changes."]))

    def test_names_dante_wont_take(self):
        for t in ({"device": "WING-ABC123", "new": "Wing desk"}, {"device": "WING-ABC123", "new": "-Wing"}, {"device": "WING-ABC123", "new": "x" * 32},
                  {"device": "WING-ABC123", "new": "Tio1608-D-1a2b"}, {"device": "Nope", "new": "Wing"},
                  {"device": "WING-ABC123", "kind": "tx", "number": 8, "new": "RTA.Mic"}, {"device": "WING-ABC123", "kind": "tx", "number": 33, "new": "RTA Mic"},
                  {"device": "WING-ABC123", "kind": "tx", "number": 99, "new": "X"}):
            st, c = self.call("/api/dante/rename/check", "POST", t)
            self.assertEqual(st, 400, t)
            self.assertTrue(c["error"], t)


if __name__ == "__main__":
    unittest.main()
