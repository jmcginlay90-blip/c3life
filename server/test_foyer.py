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
        for bad in ({"name": ""}, {"name": "x" * 31}, {"name": "A\nB"}, {"mix_a": 0}, {"mix_b": 17}, {"mix_a": 8}, {"mtx": 9}, {"max_db": -7}, {"mix_a": True}, {"a_label": "x" * 31}, {"a_from": 3}, {"b_from": "a\nb"}):
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

    def test_buttons_named_from_the_desk_or_their_own_name(self):
        for _ in range(60):
            j = self.call("/api/foyer", who="Foyer team")[1]
            if j.get("from_desk") and j.get("mix_a_name") == "Foyer Spotify":
                break
            time.sleep(0.25)
        self.assertEqual((j["a_title"], j["b_title"], j["a_from"]), ("Foyer Spotify", "Foyer Live", ""))  # the desk's names
        st, j = self.call("/api/foyer", "PUT", {"name": "Welcome area", "a_label": " Music ", "a_from": "Spotify on the Media Mac", "b_from": "The hall"})
        self.assertEqual((st, j["name"], j["a_title"], j["b_title"], j["a_from"], j["b_from"]), (200, "Welcome area", "Music", "Foyer Live", "Spotify on the Media Mac", "The hall"))
        self.assertEqual(self.call("/api/foyer", "PUT", {"a_label": "x" * 31})[0], 400)
        self.assertEqual(self.call("/api/foyer", "PUT", {"b_from": "x" * 61})[0], 400)
        self.assertEqual(self.call("/api/foyer", "PUT", {"a_label": "Music"}, who="FOH sound")[0], 403)  # Admins (Foyer setup)


class OnTheWing(Api):
    """The Foyer screens move the Wing: which mix the foyer hears, its volume and mute (Control only)."""

    def setUp(self):
        super().setUp()
        self.fw.vals.update({"/bus/7/send/1/on": 1, "/bus/7/send/1/lvl": 0.0, "/bus/8/send/1/on": 0, "/bus/8/send/1/lvl": 0.0})
        self.fw.vals.update({"/mtx/1/fdr": -10.0, "/mtx/1/mute": 0})

    def live(self, ok=lambda l: l["source"] is not None):
        for _ in range(80):
            st, j = self.call("/api/foyer", who="Foyer team")
            if j.get("live") and ok(j["live"]):
                return j["live"]
            time.sleep(0.25)
        return j.get("live")

    def test_it_shows_what_the_desk_is_doing(self):
        lv = self.live(lambda l: l["source"] is not None and l["fdr"] is not None)
        self.assertEqual((lv["connected"], lv["source"], lv["a_on"], lv["b_on"], lv["fdr"], lv["mute"], lv["mode"]), (True, "a", 1, 0, -10.0, 0, "watch"))

    def test_watch_only_changes_nothing(self):
        self.live()
        st, j = self.call("/api/foyer/do", "POST", {"do": "source", "to": "b"}, who="Foyer team")
        self.assertEqual(st, 409); self.assertIn("watch the Wing only", j["error"])
        self.assertFalse([x for x in self.fw.sets if x[0].startswith(("/bus/", "/mtx/"))])

    def test_switch_volume_and_mute_on_the_wing(self):
        self.httpd.wing.configure(mode="control")
        self.live()
        st, j = self.call("/api/foyer/do", "POST", {"do": "source", "to": "b"}, who="Foyer team")
        self.assertEqual(st, 200, j)
        for _ in range(40):
            sets = [x for x in self.fw.sets if x[0].startswith("/bus/")]
            if len(sets) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual(sets[:2], [("/bus/8/send/1/on", 1), ("/bus/7/send/1/on", 0)])  # the new one on first: no gap
        self.assertEqual(self.live(lambda l: l["source"] == "b")["source"], "b")
        st, j = self.call("/api/foyer/do", "POST", {"do": "volume", "db": -2.0}, who="Foyer team")  # above the maximum (−5)
        self.assertEqual(st, 409); self.assertIn("can't go above -5 dB", j["error"])
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "volume", "db": -12.5}, who="Foyer team")[0], 200)
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "mute", "on": True}, who="FOH sound")[0], 200)
        for _ in range(40):
            if ("/mtx/1/mute", 1) in self.fw.sets:
                break
            time.sleep(0.05)
        self.assertIn(("/mtx/1/fdr", -12.5), self.fw.sets)
        self.assertIn(("/mtx/1/mute", 1), self.fw.sets)
        lv = self.live(lambda l: l["mute"] == 1 and l["fdr"] == -12.5)
        self.assertEqual((lv["fdr"], lv["mute"]), (-12.5, 1))

    def test_balance_is_foyer_setup(self):
        self.httpd.wing.configure(mode="control")
        self.live()
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "balance", "mix": "b", "db": -4.0}, who="Foyer team")[0], 403)
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "balance", "mix": "b", "db": -4.0})[0], 200)
        lv = self.live(lambda l: l["b_lvl"] == -4.0)
        self.assertEqual((lv["a_lvl"], lv["b_lvl"]), (0.0, -4.0))

    def test_who_may(self):
        self.httpd.wing.configure(mode="control")
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "mute", "on": True}, who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/foyer/do", "POST", {"do": "source", "to": "c"}, who="Foyer team")[0], 409)
        self.assertFalse([x for x in self.fw.sets if x[0].startswith(("/bus/", "/mtx/"))])


if __name__ == "__main__":
    unittest.main()
