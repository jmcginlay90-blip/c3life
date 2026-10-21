"""Tests for setting up the RTA's routing for you: the Wing output and the Dante route, against a fake Wing and Dante.
Run: python3 -m unittest -v test_rta_patch.py"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
from test_wing import FakeWing

HERE = os.path.dirname(os.path.abspath(__file__))


class Patch(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWing()
        for g, c in (("CRD", 64), ("USB", 48)):
            for n in range(1, c + 1):
                self.fw.vals.update({"/io/out/%s/%d/grp" % (g, n): "OFF", "/io/out/%s/%d/in" % (g, n): 0})
        self.fw.vals.update({"/io/out/CRD/33/grp": "MAIN", "/io/out/CRD/33/in": 1})  # Main 1 L already on card out 33
        self.tmp = tempfile.mkdtemp()
        self.dante = os.path.join(self.tmp, "dante.json")
        with open(self.dante, "w") as f:
            json.dump({"subs": [{"rx_device": "C3LIFE", "rx_channel": "09", "rx_channel_number": 9, "tx_device": "WING-ABC", "tx_channel": "12"}],
                       "tx": {"WING-ABC": {str(n): "%02d" % n for n in range(1, 65)}, "Tio1608-D-1a2b": {str(n): "%02d" % n for n in range(1, 17)}}}, f)
        os.environ["FAKE_DANTE"] = self.dante
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", dante_dvs_name="C3LIFE",
                   netaudio=[sys.executable, os.path.join(HERE, "fake_netaudio.py")],
                   wing_ip="127.0.0.1", wing_osc_port=self.fw.osc_port, wing_native_port=self.fw.native_port)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fw.close()
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

    def plan(self, which):
        for _ in range(60):
            j = self.call("/api/rta/plan?which=" + which)[1]
            if not any("still reading" in b or "isn't connected" in b for b in j["blocked"]):
                return j
            time.sleep(0.25)
        return j

    def dante_state(self):
        with open(self.dante) as f:
            return json.load(f)

    def test_the_mic_is_patched_and_routed_after_yes_and_can_be_put_back(self):
        self.call("/api/rta/inputs")  # opening it starts reading the Wing's outputs
        p = self.plan("mic")
        self.assertEqual([st["text"] for st in p["steps"]], ["Wing: card output 1 from Off to Local In 8", "Dante: C3 Life server channel 1 from nothing to WING-ABC · 01"])
        # Watch only: nothing changes
        st, j = self.call("/api/rta/patch", "POST", {"which": "mic", "id": p["id"]})
        self.assertFalse(j["ok"])
        self.assertIn("Watch only", j["results"][0]["error"])
        self.assertEqual(self.fw.sets, [])
        self.call("/api/wing/settings", "PUT", {"mode": "control"})
        # FOH sound can patch the Wing but not Dante (Access levels)
        self.assertEqual(self.call("/api/rta/patch", "POST", {"which": "mic", "id": p["id"]}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/rta/patch", "POST", {"which": "mic", "id": "stale"})[0], 409)  # it changed since you looked
        st, j = self.call("/api/rta/patch", "POST", {"which": "mic", "id": p["id"]})
        self.assertEqual((st, j["ok"], [r["ok"] for r in j["results"]]), (200, True, [True, True]), j)
        self.assertIn(("/io/out/CRD/1/grp", "LCL"), self.fw.sets)
        self.assertIn(("/io/out/CRD/1/in", 8), self.fw.sets)
        self.assertIn({"rx_device": "C3LIFE", "rx_channel": "01", "rx_channel_number": 1, "tx_device": "WING-ABC", "tx_channel": "01"}, self.dante_state()["subs"])
        p2 = self.plan("mic")
        self.assertEqual((p2["steps"], p2["notes"]), ([], ["Wing card output 1 already sends Local In 8.", "Dante already routes WING-ABC · 01 to channel 1."]))
        st, j = self.call("/api/rta/unpatch", "POST", {"which": "mic"})
        self.assertTrue(j["ok"], j)
        self.assertEqual(self.fw.vals["/io/out/CRD/1/grp"], "OFF")
        self.assertFalse(any(x["rx_channel_number"] == 1 for x in self.dante_state()["subs"]))
        self.assertEqual(self.call("/api/rta/unpatch", "POST", {"which": "mic"})[0], 409)  # only once

    def test_the_reference_uses_the_card_output_that_already_sends_it(self):
        self.call("/api/rta/inputs")
        p = self.plan("ref")
        self.assertEqual([st["text"] for st in p["steps"]], ["Dante: C3 Life server channel 2 from nothing to WING-ABC · 33"])
        self.assertEqual(p["notes"], ["Wing card output 33 already sends Main 1 L."])
        # FOH sound can't change Dante; an Admin can, and the Wing needn't be in Control as it isn't touched
        self.assertEqual(self.call("/api/rta/patch", "POST", {"which": "ref", "id": p["id"]}, who="FOH sound")[0], 403)
        st, j = self.call("/api/rta/patch", "POST", {"which": "ref", "id": p["id"]})
        self.assertTrue(j["ok"], j)
        self.assertEqual(self.fw.sets, [])

    def test_stereo_patches_and_routes_both_sides(self):
        self.call("/api/rta/inputs", "PUT", {"ref": {"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": True, "from2": "MAIN/1/R", "ch2": 3},
                                             "mic": {"from": "LCL/8", "via": "dvs", "ch": 1, "stereo": True, "from2": "LCL/7", "ch2": 4}})
        p = self.plan("ref")
        self.assertEqual([st["text"] for st in p["steps"]], ["Dante: C3 Life server channel 2 from nothing to WING-ABC · 33 (left)",
                                                            "Wing: card output 1 from Off to Main 1 R (right)", "Dante: C3 Life server channel 3 from nothing to WING-ABC · 01 (right)"])
        self.assertEqual(p["notes"], ["Wing card output 33 already sends Main 1 L."])
        p = self.plan("mic")
        self.assertEqual([st["text"] for st in p["steps"]][::2], ["Wing: card output 1 from Off to Local In 8 (left)", "Wing: card output 2 from Off to Local In 7 (right)"])  # two different outputs
        self.call("/api/wing/settings", "PUT", {"mode": "control"})
        st, j = self.call("/api/rta/patch", "POST", {"which": "mic", "id": p["id"]})
        self.assertEqual([r["ok"] for r in j["results"]], [True] * 4, j)
        self.assertEqual((self.fw.vals["/io/out/CRD/1/in"], self.fw.vals["/io/out/CRD/2/in"]), (8, 7))
        subs = {x["rx_channel_number"]: x["tx_channel"] for x in self.dante_state()["subs"]}
        self.assertEqual((subs[1], subs[4]), ("01", "02"))

    def test_the_subs_are_patched_and_routed_with_the_reference(self):
        self.call("/api/rta/inputs", "PUT", {"ref": {"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": True, "from2": "MAIN/1/R", "ch2": 3,
                                                     "subs": {"on": True, "from": "MTX/2", "ch": 5, "from2": "MTX/2/R", "ch2": 6}}})
        p = self.plan("ref")
        self.assertEqual([st["text"] for st in p["steps"]][-4:], ["Wing: card output 2 from Off to Matrix 2 L (subs left)", "Dante: C3 Life server channel 5 from nothing to WING-ABC · 02 (subs left)",
                                                                 "Wing: card output 3 from Off to Matrix 2 R (subs right)", "Dante: C3 Life server channel 6 from nothing to WING-ABC · 03 (subs right)"])

    def test_a_tio_input_is_routed_straight_and_aes67_is_left_to_dante_controller(self):
        self.call("/api/rta/inputs", "PUT", {"mic": {"from": "TIO/2", "via": "dvs", "ch": 9}})
        p = self.plan("mic")
        self.assertEqual([st["text"] for st in p["steps"]], ["Dante: C3 Life server channel 9 from WING-ABC · 12 to Tio1608-D-1a2b · 02"])
        self.call("/api/rta/inputs", "PUT", {"mic": {"from": "LCL/8", "via": "aes67", "ch": 1}})
        p = self.plan("mic")
        self.assertEqual(p["steps"], [])
        self.assertIn("Dante Controller", p["blocked"][0])


if __name__ == "__main__":
    unittest.main()
