"""Tests for the desk interface: every driver has every method, the Wing driver is one, and C3 Life runs with no desk.
Run: python3 -m unittest -v test_desk.py"""

import inspect
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import logging

import c3life_server as s
import desk as dm
import wing as wm


class Interface(unittest.TestCase):
    def test_every_driver_has_the_whole_interface(self):
        base = {n: inspect.signature(f) for n, f in inspect.getmembers(dm.Desk, inspect.isfunction) if not n.startswith("_")}
        self.assertGreater(len(base), 15)
        for name, (mod, cls) in dm.DRIVERS.items():
            k = getattr(__import__(mod), cls)
            self.assertTrue(issubclass(k, dm.Desk), name)
            for meth, sig in base.items():
                self.assertTrue(callable(getattr(k, meth, None)), "%s has no %s" % (name, meth))
                got = [p for p in inspect.signature(getattr(k, meth)).parameters]
                want = [p for p in sig.parameters]
                self.assertEqual(got[:len(want)], want, "%s.%s%s should take %s" % (name, meth, got, want))
            self.assertEqual(set(k.CAN), set(dm.CAN), name)

    def test_the_wing_is_a_driver(self):
        self.assertTrue(all(wm.Wing.CAN.values()))
        self.assertEqual((wm.Wing.KINDS, wm.Wing.SENDERS, wm.Wing.NAME), (wm.KINDS, wm.SENDERS, "Behringer Wing"))
        self.assertIn("eq/1g", wm.Wing.BUS_EQ)

    def test_choosing_a_desk(self):
        tmp = tempfile.mkdtemp()
        try:
            log = logging.getLogger("test")
            self.assertIsInstance(dm.make({}, tmp, log), wm.Wing)
            d = dm.make({"desk": "none"}, tmp, log)
            self.assertIsInstance(d, dm.NoDesk)
            self.assertEqual(d.set("ch", 1, "fdr", 0), dm.CANT)
            self.assertEqual(d.status()["status"], "off")
            self.assertEqual(d.find(), [])
            with self.assertRaises(SystemExit) as e:
                dm.make({"desk": "dlive"}, tmp, log)
            self.assertIn("none, wing", str(e.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NoDeskServer(unittest.TestCase):
    """C3 Life with "desk": "none": the screens' calls answer, and say the desk can't."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", tio_ip="", desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        body = {"name": "Admin", "password": "c3life-admin"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.tok = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.tok)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_runs_and_says_the_desk_cant(self):
        st, h = self.call("/api/health")
        self.assertEqual((st, h["desk"]), (200, "No sound desk"))
        st, w = self.call("/api/wing")
        self.assertEqual((st, w["status"]), (200, "off"))
        st, r = self.call("/api/wing/set", "POST", {"kind": "ch", "n": 1, "field": "fdr", "value": 0})
        self.assertEqual(st, 409)
        self.assertIn("can't", r["error"])
        self.assertEqual(self.call("/api/wing/rta")[1]["rta"], [])
        self.assertEqual(self.call("/api/wing/sends?bus=1")[0], 400)  # no buses to read
        self.assertEqual(self.call("/api/rta/plan?which=mic")[0], 200)  # Set it up for me: nothing it can patch


if __name__ == "__main__":
    unittest.main()
