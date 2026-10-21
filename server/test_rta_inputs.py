"""Tests for where the RTA's room mic and desk reference come from.
Run: python3 -m unittest -v test_rta_inputs.py"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import rta_inputs as ri
from test_wing import FakeWing


class FakeDante:
    def get(self):
        return {"status": "ok", "device": "c3life-server", "at": "10:00",
                "channels": {"1": {"tx_device": "WING-card", "tx_channel": "RTA Mic", "status": "Connected"},
                             "5": {"tx_device": "Tio1608-D", "tx_channel": "02", "status": "Connected"}}}


class Settings(unittest.TestCase):
    def test_what_is_allowed(self):
        ok = json.loads(json.dumps(ri.DEFAULT))
        self.assertIsNone(ri.valid(ok))
        good = [{"mic": {"from": "TIO/2", "via": "dvs", "ch": 5}}, {"mic": {"from": "DANTE", "via": "aes67", "ch": 1}},
                {"mic": {"from": "CRD/64", "via": "usb", "ch": 48}}, {"ref": {"from": "BUS/5", "via": "dvs", "ch": 33}}, {"ref": {"from": "MTX/8", "via": "dvs", "ch": 2}}]
        for g in good:
            self.assertIsNone(ri.valid(dict(ok, **g)), g)
        bad = [{"mic": {"from": "LCL/9", "via": "dvs", "ch": 1}}, {"mic": {"from": "MAIN/1", "via": "dvs", "ch": 1}}, {"ref": {"from": "LCL/1", "via": "dvs", "ch": 2}},
               {"ref": {"from": "DANTE", "via": "dvs", "ch": 2}}, {"mic": {"from": "LCL/8", "via": "aes67", "ch": 9}}, {"mic": {"from": "LCL/8", "via": "mp3", "ch": 1}},
               {"mic": {"from": "LCL/8", "via": "dvs", "ch": 2}}, {"mic": {"from": "LCL/8", "via": "dvs", "ch": True}}]
        for b in bad:
            self.assertIsNotNone(ri.valid(dict(ok, **b)), b)

    def test_stereo_left_and_right_from_anywhere(self):
        st = {"mic": {"from": "LCL/8", "via": "dvs", "ch": 1}, "ref": {"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": True, "from2": "MAIN/1/R", "ch2": 3}}
        self.assertIsNone(ri.valid(st))
        self.assertIsNone(ri.valid(dict(st, mic={"from": "TIO/1", "via": "dvs", "ch": 5, "stereo": True, "from2": "LCL/3", "ch2": 9})))  # each side from anywhere
        for bad in ({"from2": "MAIN/1/R", "ch2": 2}, {"from2": "MAIN/1/X", "ch2": 3}, {"from2": "LCL/1", "ch2": 3}, {"from2": "MAIN/1/R", "ch2": 1}, {"from2": "MAIN/1/R", "ch2": 99}):
            self.assertIsNotNone(ri.valid(dict(st, ref=dict(st["ref"], **bad))), bad)  # same channel twice, not a side, not a desk mix, the mic's channel, too high
        tmp = tempfile.mkdtemp()
        try:
            r = ri.RtaInputs(tmp)
            r.save(dict(st, mic={"from": "TIO/2", "via": "dvs", "ch": 1, "stereo": True, "from2": "TIO/3", "ch2": 5}))
            v = ri.RtaInputs(tmp).view(FakeDante())
            self.assertEqual((v["mic"]["ch2"], v["mic"]["route2"]["tx_device"], v["ref"]["from2"]), (5, "Tio1608-D", "MAIN/1/R"))
            r.save(dict(st, ref={"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": False, "from2": "MAIN/1/R", "ch2": 3}))
            self.assertEqual(ri.RtaInputs(tmp).get()["ref"], {"from": "MAIN/1", "via": "dvs", "ch": 2})  # back to mono: the right side isn't kept
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_subs_added_to_the_reference_in_stereo(self):
        st = {"mic": {"from": "LCL/8", "via": "dvs", "ch": 1},
              "ref": {"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": True, "from2": "MAIN/1/R", "ch2": 3,
                      "subs": {"on": True, "from": "MTX/2", "ch": 4, "from2": "MTX/2/R", "ch2": 5}}}
        self.assertIsNone(ri.valid(st))
        for bad in ({"from": "LCL/1"}, {"ch": 3}, {"ch2": 1}, {"ch": 99}, {"on": "yes"}):
            self.assertIsNotNone(ri.valid(dict(st, ref=dict(st["ref"], subs=dict(st["ref"]["subs"], **bad)))), bad)  # not a desk mix, channels in use, too high
        self.assertIsNotNone(ri.valid(dict(st, mic=dict(st["mic"], subs=st["ref"]["subs"]))))  # only the reference has subs
        tmp = tempfile.mkdtemp()
        try:
            r = ri.RtaInputs(tmp)
            r.save(st)
            v = ri.RtaInputs(tmp).view(FakeDante())
            self.assertEqual((v["ref"]["subs"]["from2"], v["ref"]["subs"]["ch2"], v["ref"]["subs"]["route2"]["tx_device"]), ("MTX/2/R", 5, "Tio1608-D"))
            r.save(dict(st, ref=dict(st["ref"], subs={"on": False, "from": "MTX/2", "ch": 4, "from2": "MTX/2/R", "ch2": 5})))
            self.assertNotIn("subs", ri.RtaInputs(tmp).get()["ref"])  # off: not kept
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kept_and_shows_the_dante_route(self):
        tmp = tempfile.mkdtemp()
        try:
            r = ri.RtaInputs(tmp)
            r.save({"mic": {"from": "TIO/2", "via": "dvs", "ch": 5}, "ref": {"from": "BUS/5", "via": "aes67", "ch": 2}})
            v = ri.RtaInputs(tmp).view(FakeDante())
            self.assertEqual(v["mic"]["from"], "TIO/2")
            self.assertEqual(v["mic"]["route"], {"tx_device": "Tio1608-D", "tx_channel": "02", "status": "Connected"})
            self.assertIsNone(v["ref"]["route"])  # AES67: Dante's DVS routes don't apply
            self.assertEqual(v["dante"]["status"], "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Api(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWing()
        self.fw.vals.update({"/ch/8/name": "RTA Mic", "/main/1/name": "Main LR", "/bus/5/name": "Stream"})
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="",
                   wing_ip="127.0.0.1", wing_osc_port=self.fw.osc_port, wing_native_port=self.fw.native_port, netaudio=["false"])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "ProPresenter", "Foyer team"):
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
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_can_see_and_choose(self):
        self.assertEqual(self.call("/api/rta/inputs", who="Foyer team")[0], 403)
        st, j = self.call("/api/rta/inputs", who="ProPresenter")
        self.assertEqual((st, j["mic"]["from"], j["ref"]["from"]), (200, "LCL/8", "MAIN/1"))
        self.assertEqual(self.call("/api/rta/inputs", "PUT", {"ref": {"from": "BUS/5", "via": "dvs", "ch": 2}}, who="ProPresenter")[0], 403)  # RTA settings: view only
        st, j = self.call("/api/rta/inputs", "PUT", {"ref": {"from": "BUS/5", "via": "dvs", "ch": 2}}, who="FOH sound")
        self.assertEqual((st, j["ref"]["from"], j["mic"]["from"]), (200, "BUS/5", "LCL/8"))  # the mic stays as it was
        self.assertEqual(self.call("/api/rta/inputs", "PUT", {"ref": {"from": "BUS/5", "via": "dvs", "ch": 1}}, who="FOH sound")[0], 400)  # same channel as the mic
        self.assertEqual(self.fw.sets, [])  # choosing doesn't touch the desk

    def test_the_desk_names_the_sources(self):
        for _ in range(60):
            j = self.call("/api/rta/inputs")[1]
            if j["names"].get("LCL/8") and j["names"].get("MAIN/1"):
                break
            time.sleep(0.25)
        self.assertEqual((j["names"].get("LCL/8"), j["names"].get("MAIN/1"), j["names"].get("BUS/5")), ("RTA Mic", "Main LR", "Stream"))


if __name__ == "__main__":
    unittest.main()
