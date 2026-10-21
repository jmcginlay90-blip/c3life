"""Tests for Lights: Lightkey on the lighting Mac, run over OSC. A pretend Lightkey keeps every message it's sent.
Run: python3 -m unittest -v test_lightkey.py"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import lightkey as lk
import wing as wing_mod


class FakeLightkey:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.got = []
        self.stop = False
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.stop:
            try:
                d, _ = self.sock.recvfrom(4096)
            except OSError:
                continue
            self.got.append((time.time(), wing_mod.parse(d)))

    def msgs(self):
        return [m for _, m in self.got]

    def close(self):
        self.stop = True
        self.sock.close()


def show():
    fx = [{"id": "f1", "name": "M1", "label": "Mover left", "kind": "mover", "group": "Movers", "x": 30, "y": 20,
           "pan": [-270, 270], "tilt": [-135, 135], "wheel": ["White", "Red", "Blue"], "gobos": ["Open", "Dots"]},
          {"id": "f2", "name": "M2", "label": "Mover right", "kind": "mover", "group": "Movers", "x": 70, "y": 20,
           "pan": [-270, 270], "tilt": [-135, 135], "wheel": [], "gobos": []},
          {"id": "f3", "name": "Wash 1", "kind": "par", "group": "Front", "x": 50, "y": 60, "pan": [-1, 1], "tilt": [-1, 1], "wheel": [], "gobos": []}]
    looks = [{"id": "l1", "name": "Movers on the cross", "fade": 0, "sets": [{"fx": "M1", "prop": "panAngle", "value": -40}, {"fx": "M1", "prop": "color", "value": "#FFAA00"}]}]
    cues = [{"id": "c1", "name": "Pre-service", "page": "Main Page", "fade": 3},
            {"id": "c2", "name": "Worship big", "page": "selected", "fade": None, "look": "l1"},
            {"id": "c3", "name": "Message", "page": "Main Page", "fade": 2}]
    presets = [{"id": "p1", "name": "Indigo", "path": "Movers/Colors/Indigo", "group": "Colours", "color": "#4B0082"}]
    return {"cues": cues, "presets": presets, "fixtures": fx, "looks": looks}


class Addresses(unittest.TestCase):
    def test_names_and_palette_paths(self):
        self.assertEqual(lk.osc_part("Front Lights On"), "Front_Lights_On")
        self.assertEqual(lk.osc_part("Café #2"), "Caf?_?2")
        self.assertEqual(lk.palette_addr("Movers/Colors/Indigo"), "/palette/Movers/Colors/Indigo")
        self.assertEqual(lk.palette_addr("//Indigo"), "/palette//Indigo")
        self.assertEqual(lk.palette_addr("Movers//Indigo"), "/palette/Movers//Indigo")

    def test_what_a_show_may_hold(self):
        self.assertIsNone(lk.valid_show(show()))
        bad = [lambda d: d["fixtures"][1].update(name="M1"), lambda d: d["fixtures"][0].update(kind="laser"),
               lambda d: d["fixtures"][0].update(pan=[270, -270]), lambda d: d["cues"][0].update(fade=-1),
               lambda d: d["cues"][1].update(look="gone"), lambda d: d["looks"][0]["sets"].append({"fx": "M9", "prop": "dimmer", "value": 1}),
               lambda d: d["looks"][0]["sets"].append({"fx": "M1", "prop": "panAngle", "value": 300}),
               lambda d: d["looks"][0].update(fade=61), lambda d: d["presets"][0].update(path=""),
               lambda d: d["cues"].append(dict(d["cues"][0]))]
        for f in bad:
            d = show()
            f(d)
            self.assertIsNotNone(lk.valid_show(d))

    def test_values(self):
        self.assertIsNone(lk.valid_value("color", "#4b0082"))
        self.assertIsNone(lk.valid_value("shutterState", "strobe"))
        self.assertIsNotNone(lk.valid_value("shutterState", "wobble"))
        self.assertIsNotNone(lk.valid_value("dimmer", 1.5))
        self.assertIsNotNone(lk.valid_value("gobo1", 1.5))
        self.assertIsNotNone(lk.valid_value("blink", 1))


class FoundByTheScan(unittest.TestCase):
    """Lightkey offers its OSC over Bonjour: the network scan fills in its address, and follows the lighting Mac."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_found_then_followed(self):
        import scan as sm
        L = lk.Lightkey(self.tmp)
        mac = {"10.0.0.32": {"names": {"Events-Mac.local"}, "services": {"_osc._udp.local": "Lightkey (Events Mac)._osc._udp.local"}}}
        sc = sm.Scanner(self.tmp, {"scan_hosts": [], "scan_ports": {}}, None)
        sc.arp, sc.mdns, sc.on_found = (lambda: {}), (lambda: mac), [L.found]
        sc.log = type("Log", (), {"info": lambda *a: None, "warning": lambda *a: None})()
        sc.run()
        self.assertEqual((L.settings["ip"], L.settings["mode"], L.bonjour, L.seen), ("10.0.0.32", "off", "Lightkey (Events Mac)", True))  # Off: Control is an Admin's
        self.assertIn("Found Lightkey (Lightkey (Events Mac)) at 10.0.0.32", sc.result["changes"][0])
        L.settings["mode"] = "control"
        mac = {"10.0.0.47": mac["10.0.0.32"]}  # the Mac got a new address
        sc.mdns = lambda: mac
        sc.run()
        self.assertEqual((L.settings["ip"], L.settings["mode"]), ("10.0.0.47", "control"))
        self.assertIn("moved from 10.0.0.32 to 10.0.0.47", sc.result["changes"][0])
        self.assertEqual(lk.Lightkey(self.tmp).settings["ip"], "10.0.0.47")  # kept after a restart
        sc.mdns = lambda: {}
        sc.run()
        self.assertEqual((L.settings["ip"], L.seen), ("10.0.0.47", False))  # not seen: kept, and the warning says so

    def test_two_lightkeys_and_none_set_changes_nothing(self):
        L = lk.Lightkey(self.tmp)
        two = [{"kind": "lightkey", "ip": "10.0.0.%d" % n, "port": 21600, "name": "Lightkey (%d)" % n} for n in (5, 6)]
        self.assertEqual((L.found(two), L.settings["ip"]), ([], ""))
        L.settings["ip"] = "10.0.0.9"; L.bonjour = "Other"
        self.assertEqual((L.found(two), L.settings["ip"], L.seen), ([], "10.0.0.9", False))


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeLightkey()
        self.k = lk.Lightkey(self.tmp)
        self.k.save_show(show())

    def tearDown(self):
        self.k.close()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def on(self):
        self.k.save_settings(dict(lk.SETTINGS, ip="127.0.0.1", port=self.fake.port, mode="control"))

    def wait(self, n, t=2.0):
        end = time.time() + t
        while len(self.fake.got) < n and time.time() < end:
            time.sleep(0.02)
        return self.fake.msgs()

    def test_off_sends_nothing_but_says_what_it_would(self):
        ok, err, msgs = self.k.do({"do": "go"})
        self.assertFalse(ok)
        self.assertIn("off", err)
        self.assertEqual(msgs, [("/live/Main_Page/cue/Pre-service/activate", [3.0])])
        time.sleep(0.2)
        self.assertEqual(self.fake.got, [])
        self.assertIsNone(self.k.view()["state"]["live"])

    def test_go_back_and_the_cue_before_lets_go(self):
        self.on()
        self.k.do({"do": "go"})
        self.k.do({"do": "go"})
        got = self.wait(5)
        self.assertEqual(got[0], ("/live/Main_Page/cue/Pre-service/activate", [3.0]))
        # Worship big has no fade of its own and brings its look with it
        self.assertEqual(got[1], ("/live/Main_Page/cue/Pre-service/deactivate", []))
        self.assertEqual(got[2], ("/live/selected/cue/Worship_big/activate", []))
        self.assertEqual(got[3][0], "/fixture/M1/overrides/panAngle")
        self.assertAlmostEqual(got[3][1][0], -40.0)
        self.assertEqual(got[4], ("/fixture/M1/overrides/color", ["ffaa00"]))
        v = self.k.view()
        self.assertEqual((v["state"]["live"], v["state"]["next"]), ("c2", "c3"))
        self.assertEqual(v["state"]["overrides"]["M1"]["color"], "#FFAA00")
        ok, _, _ = self.k.do({"do": "back"})
        self.assertTrue(ok)
        self.assertEqual(self.k.view()["state"]["live"], "c1")
        self.k.do({"do": "cue", "id": "c3"})
        ok, err, _ = self.k.do({"do": "go"})
        self.assertFalse(ok)
        self.assertIn("end", err)

    def test_lightkeys_own_cuelist(self):
        self.k.save_settings(dict(lk.SETTINGS, ip="127.0.0.1", port=self.fake.port, mode="control", go="lightkey"))
        self.k.do({"do": "go"})
        self.k.do({"do": "back"})
        self.assertEqual(self.wait(2), [("/live/nextCue", []), ("/live/previousCue", [])])

    def test_palette_master_blackout_tempo(self):
        self.on()
        for a in ({"do": "preset", "id": "p1"}, {"do": "master", "value": 0.8}, {"do": "blackout", "on": True},
                  {"do": "master", "value": 0.5}, {"do": "blackout", "on": False}, {"do": "tap"}, {"do": "tempo", "bpm": 72}):
            self.assertTrue(self.k.do(a)[0], a)
        got = self.wait(6)
        self.assertEqual(got[0], ("/palette/Movers/Colors/Indigo/activate", []))
        self.assertEqual(got[1][0], "/output/master")
        self.assertEqual(got[2], ("/output/master", [0.0]))
        # the master moved while blacked out: nothing sent, and it comes back at the new level
        self.assertAlmostEqual(got[3][1][0], 0.5)
        self.assertEqual(got[4:], [("/beat/tap", []), ("/beat/tempo", [72.0])])
        self.assertEqual(self.k.view()["state"]["presets_on"], ["p1"])

    def test_programmer_and_clear(self):
        self.on()
        self.assertTrue(self.k.do({"do": "fixture", "fx": ["M1", "M2"], "prop": "tiltAngle", "value": 30})[0])
        self.assertTrue(self.k.do({"do": "fixture", "fx": ["M1"], "prop": "shutterState", "value": "strobe"})[0])
        self.assertFalse(self.k.do({"do": "fixture", "fx": ["M1"], "prop": "panAngle", "value": 400})[0])
        self.assertFalse(self.k.do({"do": "fixture", "fx": [], "prop": "dimmer", "value": 1})[0])
        self.k.do({"do": "clear", "fx": ["M1"], "prop": "tiltAngle"})
        got = self.wait(4)
        self.assertEqual([m[0] for m in got], ["/fixture/M1/overrides/tiltAngle", "/fixture/M2/overrides/tiltAngle",
                                               "/fixture/M1/overrides/shutterState", "/fixture/M1/overrides/position/clear"])
        self.assertEqual(self.k.view()["state"]["overrides"], {"M1": {"shutterState": "strobe"}, "M2": {"tiltAngle": 30}})
        self.k.do({"do": "clear", "fx": "all"})
        self.assertEqual(self.wait(5)[-1], ("/fixture/*/overrides/clear", []))
        self.assertEqual(self.k.view()["state"]["overrides"], {})

    def test_programmer_fade(self):
        self.on()
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "dimmer", "value": 0.0})
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "color", "value": "#000000"})
        self.wait(2)
        t0 = time.time()
        # dimmer and colour slide over 1 s; the gobo can't, so it changes straight away; M2's dimmer was never set, so it jumps
        self.k.do({"do": "fixture", "fx": ["M1", "M2"], "prop": "dimmer", "value": 1.0, "fade": 1})
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "color", "value": "#FF0000", "fade": 1})
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "gobo1", "value": 1, "fade": 1})
        self.assertTrue(self.k.view()["state"]["fading"])
        time.sleep(1.4)
        later = [(t - t0, m) for t, m in self.fake.got[2:]]
        dims = [(t, m[1][0]) for t, m in later if m[0] == "/fixture/M1/overrides/dimmer"]
        reds = [m[1][0] for t, m in later if m[0] == "/fixture/M1/overrides/color"]
        self.assertGreater(len(dims), 8)
        self.assertTrue(all(b >= a for (_, a), (_, b) in zip(dims, dims[1:])))
        self.assertTrue(any(0.3 < v < 0.7 for _, v in dims))
        self.assertAlmostEqual(dims[-1][1], 1.0)
        self.assertGreater(dims[-1][0], 0.9)
        self.assertEqual(reds[-1], "ff0000")
        self.assertTrue(any(r not in ("000000", "ff0000") for r in reds))
        firsts = {m[0]: t for t, m in reversed(later)}
        self.assertLess(firsts["/fixture/M2/overrides/dimmer"], 0.2)
        self.assertLess(firsts["/fixture/M1/overrides/gobo1"], 0.2)
        self.assertEqual(self.k.view()["state"]["fading"], [])
        self.assertFalse(self.k.do({"do": "fixture", "fx": ["M1"], "prop": "dimmer", "value": 1, "fade": 61})[0])

    def test_clear_stops_a_fade(self):
        self.on()
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "panAngle", "value": 0})
        self.k.do({"do": "fixture", "fx": ["M1"], "prop": "panAngle", "value": 90, "fade": 5})
        time.sleep(0.3)
        self.k.do({"do": "clear", "fx": "all"})
        time.sleep(0.2)
        n = len(self.fake.got)
        time.sleep(0.3)
        self.assertEqual(len(self.fake.got), n)

    def test_kept_after_a_restart(self):
        self.on()
        k2 = lk.Lightkey(self.tmp)
        self.assertEqual(k2.view()["show"]["cues"][1]["name"], "Worship big")
        self.assertEqual(k2.view()["settings"]["mode"], "control")
        k2.close()


class Api(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLightkey()
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", wing_ip="", tio_ip="")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        cfg["_lightkey_browse"] = lambda types, timeout: {"192.168.10.32": {"names": set(), "services": {"_osc._udp.local": "Lightkey (Events Mac)._osc._udp.local"}}}
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "Lighting", "FOH sound", "Viewer"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "" if n == "Viewer" else "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.close()
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

    def test_who_may_do_what(self):
        self.assertEqual(self.call("/api/lights", who="Viewer")[0], 403)          # Viewer: no Lights
        st, v = self.call("/api/lights", who="FOH sound")                         # FOH sound: Lights view
        self.assertEqual((st, v["on"]), (200, False))
        self.assertEqual(self.call("/api/lights/show", "PUT", show(), who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/lights/show", "PUT", show(), who="Lighting")[0], 200)
        bad = show()
        bad["fixtures"][0]["kind"] = "laser"
        self.assertEqual(self.call("/api/lights/show", "PUT", bad, who="Lighting")[0], 400)
        on = {"ip": "127.0.0.1", "port": self.fake.port, "mode": "control"}
        self.assertEqual(self.call("/api/lights/settings", "PUT", on, who="Lighting")[0], 403)  # the link: Admin
        self.assertEqual(self.call("/api/lights/do", "POST", {"do": "go"}, who="FOH sound")[0], 403)
        st, r = self.call("/api/lights/do", "POST", {"do": "go"}, who="Lighting")
        self.assertEqual((st, r["ok"], r["messages"][0]["addr"]), (409, False, "/live/Main_Page/cue/Pre-service/activate"))
        self.assertEqual(self.call("/api/lights/settings", "PUT", {"mode": "control"})[0], 400)  # no address yet
        self.assertEqual(self.call("/api/lights/settings", "PUT", on)[0], 200)
        st, r = self.call("/api/lights/do", "POST", {"do": "go"}, who="Lighting")
        self.assertEqual((st, r["ok"], r["state"]["live"]), (200, True, "c1"))
        st, r = self.call("/api/lights/do", "POST", {"do": "fixture", "fx": ["M1"], "prop": "dimmer", "value": 2}, who="Lighting")
        self.assertEqual(st, 400)
        end = time.time() + 2
        while not self.fake.got and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(self.fake.msgs(), [("/live/Main_Page/cue/Pre-service/activate", [3.0])])
        st, v = self.call("/api/lights", who="FOH sound")
        self.assertEqual((v["sent"][-1]["addr"], v["state"]["live"]), ("/live/Main_Page/cue/Pre-service/activate", "c1"))

    def test_find_lightkey(self):
        self.assertEqual(self.call("/api/lights/find", "POST", {}, who="Lighting")[0], 403)
        st, r = self.call("/api/lights/find", "POST", {})
        self.assertEqual(r["found"], [{"name": "Lightkey (Events Mac)", "ip": "192.168.10.32", "port": 21600, "lightkey": True}])


if __name__ == "__main__":
    unittest.main()
