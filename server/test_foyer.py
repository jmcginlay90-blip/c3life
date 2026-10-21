"""Tests for the foyer settings: its name and its two mixes, named from the desk.
Run: python3 -m unittest -v test_foyer.py"""

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
import foyer as fm
from test_wing import FakeWing


class Settings(unittest.TestCase):
    def test_what_is_allowed(self):
        ok = dict(fm.DEFAULT)
        self.assertIsNone(fm.valid(ok))
        for bad in ({"name": ""}, {"name": "x" * 31}, {"name": "A\nB"}, {"mix_a": 0}, {"mix_b": 17}, {"mix_a": 8}, {"mtx": 9}, {"max_db": -7}, {"mix_a": True}):
            self.assertIsNotNone(fm.valid(dict(ok, **bad)), bad)

    def test_kept_and_falls_back_without_a_desk(self):
        tmp = tempfile.mkdtemp()
        try:
            f = fm.Foyer(tmp)
            f.save(dict(fm.DEFAULT, name="  Café  ", mix_a=3))
            v = fm.Foyer(tmp).view()
            self.assertEqual((v["name"], v["mix_a"], v["mix_a_name"], v["mix_b_name"], v["from_desk"]), ("Café", 3, "Spotify", "Live service", False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Api(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWing()
        self.fw.vals.update({"/bus/7/name": "Foyer Spotify", "/bus/8/name": "Foyer Live", "/bus/3/name": "Café music", "/mtx/1/name": "Foyer spk"})
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
        for n in ("Admin", "FOH sound", "Foyer team", "Keys (musician)"):
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
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_the_mixes_are_named_from_the_desk(self):
        for _ in range(60):
            st, j = self.call("/api/foyer", who="Foyer team")
            if j.get("from_desk") and j.get("mix_a_name") != "Spotify" and j.get("mtx_name"):
                break
            time.sleep(0.25)
        self.assertEqual(st, 200)
        self.assertEqual((j["name"], j["mix_a_name"], j["mix_b_name"], j["mtx_name"]), ("Foyer", "Foyer Spotify", "Foyer Live", "Foyer spk"))
        self.assertEqual(j["names"]["bus"]["3"], "Café music")

    def test_only_foyer_setup_can_rename_it(self):
        self.assertEqual(self.call("/api/foyer", who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/foyer", "PUT", {"name": "Café"}, who="Foyer team")[0], 403)
        self.assertEqual(self.call("/api/foyer", "PUT", {"name": "Café"}, who="FOH sound")[0], 403)
        st, j = self.call("/api/foyer", "PUT", {"name": " Café ", "mix_a": 3})
        self.assertEqual((st, j["name"], j["mix_a"]), (200, "Café", 3))
        self.assertEqual(self.call("/api/foyer", "PUT", {"mix_b": 3})[0], 400)  # the same bus twice
        self.assertEqual(self.call("/api/foyer", who="Foyer team")[1]["name"], "Café")  # every device sees it


if __name__ == "__main__":
    unittest.main()
