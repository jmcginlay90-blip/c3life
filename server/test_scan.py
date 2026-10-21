"""Tests for the network scan and its rules, against fake devices on this computer.
Run: python3 -m unittest -v test_scan.py"""

import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import scan as sm
from test_tio import FakeTio


def web(pages):
    """A tiny web server answering {path: (status, body)}."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            st, body = pages.get(self.path, (404, "no"))
            b = (json.dumps(body) if not isinstance(body, str) else body).encode()
            self.send_response(st)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    h = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    return h


class Dev:
    """Stands in for the Wing or Tio module: just its settings."""
    def __init__(self, ip="", mode="off", port=2223):
        self.settings = {"ip": ip, "mode": mode}
        self.osc_port = self.port = port
        self.calls = []

    def configure(self, ip=None, mode=None):
        self.calls.append((ip, mode))
        if ip is not None:
            self.settings["ip"] = ip
        if mode is not None:
            self.settings["mode"] = mode


class _Log:
    def info(self, *a):
        pass
    warning = info


def wing(ip, serial):
    return {"ip": ip, "name": "Booth Wing", "model": "ngc-full", "serial": serial, "firmware": "3.0.6"}


class Parsing(unittest.TestCase):
    def test_arp_tables_from_windows_mac_and_linux(self):
        win = "Interface: 192.168.10.10 --- 0x5\n  Internet Address      Physical Address      Type\n  192.168.10.20         00-1d-c1-0a-0b-0c     dynamic\n  192.168.10.255        ff-ff-ff-ff-ff-ff     static\n  224.0.0.251           01-00-5e-00-00-fb     static\n"
        self.assertEqual(sm.parse_arp(win), {"192.168.10.20": "001dc10a0b0c"})
        mac = "? (192.168.10.30) at 0:a0:de:1:2:3 on en0 ifscope [ethernet]\n? (192.168.10.31) at (incomplete) on en0 ifscope [ethernet]\n"
        self.assertEqual(sm.parse_arp(mac), {"192.168.10.30": "00a0de010203"})
        lin = "IP address       HW type     Flags       HW address            Mask     Device\n192.168.10.40    0x1         0x2         b8:27:eb:11:22:33     *        eth0\n"
        self.assertEqual(sm.parse_arp(lin), {"192.168.10.40": "b827eb112233"})

    def test_a_bonjour_answer(self):
        def nm(x):
            return b"".join(bytes([len(p)]) + p.encode() for p in x.split(".")) + b"\0"
        rr = lambda n, t, d: nm(n) + struct.pack("!HHIH", t, 1, 120, len(d)) + d
        pkt = struct.pack("!HHHHHH", 0, 0x8400, 0, 3, 0, 0)
        pkt += rr("_netaudio-arc._udp.local", 12, nm("Tio1608-D-1a2b._netaudio-arc._udp.local"))
        pkt += rr("Tio1608-D-1a2b._netaudio-arc._udp.local", 33, struct.pack("!HHH", 0, 0, 4440) + nm("Tio1608-D-1a2b.local"))
        pkt += rr("Tio1608-D-1a2b.local", 1, socket.inet_aton("192.168.10.41"))
        r = sm.parse_mdns(pkt)
        self.assertEqual(r["ptr"], [("_netaudio-arc._udp.local", "Tio1608-D-1a2b._netaudio-arc._udp.local")])
        self.assertEqual(r["srv"]["Tio1608-D-1a2b._netaudio-arc._udp.local"], ("Tio1608-D-1a2b.local", 4440))
        self.assertEqual(r["a"], {"Tio1608-D-1a2b.local": "192.168.10.41"})
        self.assertEqual(sm.parse_mdns(b"\x00\x01junk"), {"ptr": [], "srv": {}, "a": {}})  # rubbish doesn't break it

    def test_the_question_asks_for_each_type(self):
        q = sm.mdns_query(["_apple-midi._udp.local"])
        self.assertEqual(struct.unpack("!H", q[4:6])[0], 1)
        self.assertIn(b"\x0b_apple-midi\x04_udp\x05local\x00\x00\x0c\x80\x01", q)


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wing, self.tio = Dev(), Dev(port=49280)
        self.wings = []
        self.cfg = {"propresenter": "", "scan_hosts": [], "scan_ports": {}, "scan_networks": ["10.9.9.0/24"], "scan_timeout": 0.3}
        self.sc = sm.Scanner(self.tmp, self.cfg, _Log(), wing=self.wing, tio=None, wing_find=lambda: self.wings)
        self.sc.arp = lambda: {}
        self.sc.mdns = lambda: {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self):
        self.sc.run("FOH sound")
        return self.sc.result

    def test_a_new_wing_connects_in_watch_only(self):
        self.wings = [wing("10.9.9.20", "S1")]
        r = self.go()
        self.assertEqual(self.wing.settings, {"ip": "10.9.9.20", "mode": "watch"})
        self.assertEqual(r["found"][0]["state"], "connected_now")
        self.assertEqual(r["changes"], ["Found the Behringer Wing at 10.9.9.20 and connected in Watch only"])
        self.assertEqual(self.sc.settings["seen"]["wing"]["hwid"], "wing:S1")
        r = self.go()  # and next time it's simply connected
        self.assertEqual((r["found"][0]["state"], r["changes"], r["missing"]), ("connected", [], []))

    def test_a_wing_that_moved_is_followed_and_put_back_to_watch_only(self):
        self.wings = [wing("10.9.9.20", "S1")]
        self.go()
        self.wing.settings["mode"] = "control"  # an Admin turned Control on
        self.wings = [wing("10.9.9.21", "S1")]
        r = self.go()
        self.assertEqual(self.wing.settings, {"ip": "10.9.9.21", "mode": "watch"})
        self.assertEqual((r["found"][0]["state"], r["found"][0]["was"]), ("moved", "10.9.9.20"))
        self.assertIn("put it back to Watch only", r["changes"][0])
        self.assertEqual(r["missing"], [])

    def test_a_different_wing_is_not_followed(self):
        self.wings = [wing("10.9.9.20", "S1")]
        self.go()
        self.wings = [wing("10.9.9.22", "S2")]
        r = self.go()
        self.assertEqual(self.wing.settings["ip"], "10.9.9.20")
        self.assertEqual(r["found"][0]["state"], "choose")
        self.assertEqual(r["missing"][0]["ip"], "10.9.9.20")
        self.assertEqual(r["changes"], [])

    def test_two_wings_ask_which_one(self):
        self.wings = [wing("10.9.9.20", "S1"), wing("10.9.9.22", "S2")]
        r = self.go()
        self.assertEqual(self.wing.settings, {"ip": "", "mode": "off"})
        self.assertEqual([f["state"] for f in r["found"]], ["choose", "choose"])
        self.assertIn("wasn't found", self.sc.use("wing", "10.9.9.99", 2223, "Admin"))
        self.assertIsNone(self.sc.use("wing", "10.9.9.22", 2223, "Admin"))
        self.assertEqual(self.wing.settings, {"ip": "10.9.9.22", "mode": "watch"})
        self.assertEqual([f["state"] for f in self.sc.result["found"]], ["recognised", "connected"])

    def test_a_wing_turned_off_stays_off(self):
        self.wing.settings.update(ip="10.9.9.20", mode="off")
        self.wings = [wing("10.9.9.21", "S1")]
        r = self.go()
        self.assertEqual(self.wing.settings, {"ip": "10.9.9.20", "mode": "off"})
        self.assertEqual((r["found"][0]["state"], r["missing"], r["changes"]), ("off_choice", [], []))

    def test_a_scan_never_turns_control_on(self):
        for ws in ([wing("10.9.9.20", "S1")], [wing("10.9.9.21", "S1")], [wing("10.9.9.21", "S1"), wing("10.9.9.22", "S2")]):
            self.wings = ws
            self.go()
            self.assertTrue(all(m != "control" for _, m in self.wing.calls))

    def test_devices_without_a_driver_and_hiding_them(self):
        self.sc.arp = lambda: {"10.9.9.7": "b827eb112233", "10.9.9.8": "aa0000000001", "192.168.1.1": "aa0000000002"}
        r = self.go()
        self.assertEqual([u["ip"] for u in r["unknown"]], ["10.9.9.7", "10.9.9.8"])  # 192.168.1.1 isn't on the booth network
        self.assertIn("made by Raspberry Pi (from its network card)", r["unknown"][0]["how"])
        self.sc.hide("10.9.9.7", "b827eb112233")
        self.assertEqual([u["ip"] for u in self.sc.result["unknown"]], ["10.9.9.8"])
        sc2 = sm.Scanner(self.tmp, dict(self.cfg), _Log(), wing=self.wing, wing_find=lambda: [])
        sc2.arp, sc2.mdns = self.sc.arp, lambda: {}
        sc2.run()
        self.assertEqual([u["ip"] for u in sc2.result["unknown"]], ["10.9.9.8"])  # remembered after a restart

    def test_bonjour_finds_dante_and_network_midi(self):
        self.sc.mdns = lambda: {"10.9.9.41": {"names": {"Tio1608-D-1a2b"}, "services": {"_netaudio-arc._udp.local": "Tio1608-D-1a2b._netaudio-arc._udp.local"}},
                                "10.9.9.31": {"names": {"MacBook-Pro"}, "services": {"_apple-midi._udp.local": "Playback._apple-midi._udp.local"}}}
        r = self.go()
        self.assertEqual(sorted((f["kind"], f["ip"], f["type"]) for f in r["found"]), [("dante", "10.9.9.41", "Dante (discovery)"), ("midi", "10.9.9.31", "Network MIDI")])
        self.assertEqual(r["unknown"], [])


class Devices(unittest.TestCase):
    """Real (fake) devices answering on this computer: ProPresenter, the helper, a Tio, a web camera."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pp = web({"/version": (200, {"name": "Mac mini", "platform": "mac", "host_description": "ProPresenter 7.16"})})
        self.helper = web({"/hello": (200, {"ok": True, "service": "c3life-helper", "name": "Mac mini", "apps": ["propresenter", "rremote"]})})
        self.cam = web({"/": (200, "<html><head><title>AW-UE70 Camera</title></head></html>")})
        self.ft = FakeTio()
        ports = {self.pp.server_address[1]: "propresenter", self.helper.server_address[1]: "helper", self.ft.port: "yamaha", self.cam.server_address[1]: "http"}
        self.tio = Dev(port=self.ft.port)
        self.cfg = {"propresenter": "http://127.0.0.1:%d" % self.pp.server_address[1], "scan_hosts": ["127.0.0.1"], "scan_ports": ports,
                    "scan_networks": ["10.9.9.0/24"], "scan_timeout": 0.5}
        self.sc = sm.Scanner(self.tmp, self.cfg, _Log(), wing=Dev(), tio=self.tio, wing_find=lambda: [])
        self.sc.arp, self.sc.mdns = (lambda: {}), (lambda: {})

    def tearDown(self):
        for h in (self.pp, self.helper, self.cam):
            h.shutdown()
            h.server_close()
        self.ft.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_is_recognised_and_only_asked(self):
        self.sc.run()
        r = self.sc.result
        kinds = {f["kind"]: f for f in r["found"]}
        self.assertEqual(sorted(kinds), ["helper", "propresenter", "tio"])
        self.assertEqual(kinds["propresenter"]["state"], "connected")
        self.assertEqual(kinds["propresenter"]["detail"], "ProPresenter 7.16")
        self.assertEqual(kinds["helper"]["detail"], "propresenter, rremote")
        self.assertEqual(kinds["tio"]["state"], "connected_now")
        self.assertEqual(self.tio.settings, {"ip": "127.0.0.1", "mode": "watch"})
        self.assertEqual(self.ft.sets, [])  # a scan only asks: nothing was set on the Tio
        self.assertEqual(r["unknown"], [])  # the camera's on the same address as ProPresenter here, so it isn't listed twice

    def test_a_device_without_a_driver_says_what_it_is(self):
        sc = sm.Scanner(self.tmp, {"scan_hosts": ["127.0.0.1"], "scan_ports": {self.cam.server_address[1]: "http"}, "scan_networks": ["10.9.9.0/24"]}, _Log())
        sc.arp, sc.mdns = (lambda: {}), (lambda: {})
        sc.run()
        self.assertEqual(sc.result["unknown"], [{"ip": "127.0.0.1", "hwid": "", "how": "Web page on port %d: AW-UE70 Camera" % self.cam.server_address[1]}])

    def test_propresenter_that_moved_is_followed_by_its_hardware_id(self):
        self.sc.arp = lambda: {"127.0.0.1": "00a0de010203"}
        self.sc.run()
        self.assertEqual(self.sc.settings["seen"]["propresenter"]["hwid"], "00a0de010203")
        self.cfg["propresenter"] = "http://127.0.0.2:%d" % self.pp.server_address[1]  # what C3 Life has is out of date
        self.sc.run()
        self.assertEqual(self.cfg["propresenter"], "http://127.0.0.1:%d" % self.pp.server_address[1])
        self.assertIn("ProPresenter 7 moved from 127.0.0.2 to 127.0.0.1", self.sc.result["changes"][0])
        sc2 = sm.Scanner(self.tmp, {"propresenter": "http://old"}, _Log())
        self.assertEqual(sc2.cfg["propresenter"], "http://127.0.0.1:%d" % self.pp.server_address[1])  # kept after a restart


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", scan_auto=False,
                   scan_hosts=[], scan_ports={}, wing_find_target="127.0.0.1")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.httpd.scanner.arp = lambda: {}
        self.httpd.scanner.mdns = lambda: {}
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "ProPresenter"):
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
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_can_scan(self):
        self.assertEqual(self.call("/api/scan", who=None)[0], 401)
        self.assertEqual(self.call("/api/scan", who="ProPresenter")[0], 200)  # can look
        self.assertEqual(self.call("/api/scan", "POST", {}, who="ProPresenter")[0], 403)  # can't scan
        self.assertEqual(self.call("/api/scan", "POST", {}, who="FOH sound")[0], 200)
        for _ in range(100):
            st = self.call("/api/scan")[1]
            if not st["running"] and st["result"]:
                break
            time.sleep(0.1)
        self.assertEqual(st["result"]["by"], "FOH sound")
        self.assertEqual(self.call("/api/scan/hide", "POST", {"ip": "10.0.0.1", "hwid": ""}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/scan/hide", "POST", {"ip": "nope"})[0], 400)
        self.assertEqual(self.call("/api/scan/use", "POST", {"kind": "wing", "ip": "10.0.0.9", "port": 2223}, who="FOH sound")[0], 409)
        self.assertEqual(self.call("/api/scan/settings", "PUT", {"auto": False}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/scan/settings", "PUT", {"auto": False})[1]["auto"], False)


if __name__ == "__main__":
    unittest.main()
