"""Tests for the live Wing link, against a fake Wing (OSC on UDP, meters by the native protocol).
Run: python3 -m unittest -v test_wing.py"""

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

import c3life_server as s
import wing as wm


class FakeWing:
    """Answers like a Wing: queries get the (string, position, value) triplets, sets change it,
    /*s subscribes, and the native port sends meters to whoever asks."""

    def __init__(self):
        self.vals = {}
        for k, c in wm.KINDS.items():
            for n in range(1, c + 1):
                self.vals["/%s/%d/fdr" % (k, n)] = -144.0
                self.vals["/%s/%d/mute" % (k, n)] = 0
                self.vals["/%s/%d/name" % (k, n)] = ""
                self.vals["/%s/%d/col" % (k, n)] = 1
                if k != "dca":
                    self.vals["/%s/%d/pan" % (k, n)] = 0.0
        for n in range(1, 41):
            self.vals["/ch/%d/in/conn/grp" % n] = "LCL" if n <= 8 else ("A" if n <= 16 else "USB")
            self.vals["/ch/%d/in/conn/in" % n] = n if n <= 8 else (n - 8 if n <= 16 else n)
        for g, c in (("LCL", 8), ("A", 48)):
            for n in range(1, c + 1):
                self.vals["/io/in/%s/%d/g" % (g, n)] = 20.0
                self.vals["/io/in/%s/%d/vph" % (g, n)] = 0
                self.vals["/io/in/%s/%d/mode" % (g, n)] = "M"
        for n in range(1, 49):
            self.vals["/io/in/USB/%d/mode" % n] = "ST" if n == 20 else "M"
        for n in range(1, 41):  # the standard plugins' starting values, as a reset Wing has them
            for k, v in {"in/set/trim": 0.0, "in/set/inv": 0, "flt/lc": 0, "flt/lcf": 100.2, "flt/hc": 0, "flt/hcf": 10023.7, "flt/mdl": "TILT",
                         "gate/on": 0, "gate/thr": -40.0, "gate/range": 40.0, "gate/mdl": "GATE", "eq/on": 0, "eq/mdl": "STD",
                         "eq/lg": 0.0, "eq/lf": 80.2, "eq/lq": 2.0, "eq/1g": 0.0, "eq/1f": 200.0, "eq/1q": 2.0, "eq/2g": 0.0, "eq/2f": 500.0, "eq/2q": 2.0,
                         "eq/3g": 0.0, "eq/3f": 1500.0, "eq/3q": 2.0, "eq/4g": 0.0, "eq/4f": 3990.5, "eq/4q": 2.0, "eq/hg": 0.0, "eq/hf": 11999.3, "eq/hq": 2.0,
                         "dyn/on": 0, "dyn/thr": -10.0, "dyn/ratio": 3.0, "dyn/gain": 0.0, "dyn/att": 50.0, "dyn/rel": 152.6, "dyn/auto": 1, "dyn/mdl": "COMP"}.items():
                self.vals["/ch/%d/%s" % (n, k)] = v
        self.vals["/ch/4/eq/mdl"] = "SOUL"
        for n in range(1, 41):  # sends to the 16 buses: Pre, pan link off, -inf, except bus 1 (the wedges)
            for b in range(1, 17):
                self.vals.update({"/ch/%d/send/%d/on" % (n, b): 1, "/ch/%d/send/%d/lvl" % (n, b): -144.0, "/ch/%d/send/%d/mode" % (n, b): "PRE",
                                  "/ch/%d/send/%d/plink" % (n, b): 0, "/ch/%d/send/%d/pan" % (n, b): 0.0})
        self.vals.update({"/ch/9/send/1/mode": "POST", "/ch/10/send/1/mode": "POST", "/ch/1/send/4/plink": 1, "/ch/1/send/4/lvl": -6.0})
        self.vals.update({"/ch/1/name": "Lead Vox", "/ch/1/fdr": 0.0, "/ch/1/col": 3, "/ch/15/name": "Pastor HH", "/ch/15/mute": 1,
                          "/main/1/name": "Main LR", "/main/1/fdr": 0.0, "/dca/1/name": "Band"})
        self.vals.update({"/cfg/rta/rtasrc": 0, "/cfg/rta/rtatap": "IN", "/cfg/rta/$src": 1, "/cfg/rta/$tap": "IN"})
        self.rta_base = 1  # the Wing's index counts from 0: rtasrc 0 is channel 1 (set to 0 to count from 1)
        self.rta_wanted = False
        self.sets, self.sub, self.silent = [], None, False
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self.osc_port = self.udp.getsockname()[1]
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(2)
        self.tcp.settimeout(0.2)
        self.native_port = self.tcp.getsockname()[1]
        self.meter_to, self.levels = None, {n: -60.0 for n in range(1, 41)}
        self.levels[1] = -12.0
        self.stop = threading.Event()
        for fn in (self.osc_loop, self.native_loop, self.meter_loop):
            threading.Thread(target=fn, daemon=True).start()

    def reply(self, addr, to):
        v = self.vals[addr]
        f = addr.rsplit("/", 1)[1]
        if f in ("fdr", "g", "pan") or (isinstance(v, float) and "/ch/" in addr and addr.count("/") >= 4):
            pkt = wm.osc(addr, "-oo" if v <= -144 else "%.1f" % v, float(wm.db_to_pos(v)), float(v))
        elif isinstance(v, str):
            pkt = wm.osc(addr, v)
        else:
            pkt = wm.osc(addr, str(v), float(v), int(v))
        self.udp.sendto(pkt, to)

    def change(self, addr, value):
        """Someone moves it on the desk: subscribers hear at once."""
        self.vals[addr] = value
        if self.sub:
            self.reply(addr, self.sub)

    def osc_loop(self):
        while not self.stop.is_set():
            try:
                data, frm = self.udp.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if self.silent:
                continue
            addr, args = wm.parse(data)
            if addr == "/?":
                self.udp.sendto(wm.osc("/?", "WING,127.0.0.1,Booth Wing,ngc-full,S123,3.0.6"), frm)
            elif addr == "/*s":
                self.sub = frm
            elif addr in self.vals:
                if args:
                    self.sets.append((addr, args[0]))
                    v = args[0]
                    self.vals[addr] = float(v) if isinstance(self.vals[addr], float) else (v if isinstance(self.vals[addr], str) else int(v))
                    if addr == "/cfg/rta/rtasrc":
                        self.vals["/cfg/rta/$src"] = self.vals[addr] + self.rta_base
                    if addr == "/cfg/rta/rtatap":
                        self.vals["/cfg/rta/$tap"] = self.vals[addr]
                else:
                    self.reply(addr, frm)

    def native_loop(self):
        while not self.stop.is_set():
            try:
                c, frm = self.tcp.accept()
            except (socket.timeout, OSError):
                continue
            c.settimeout(0.3)
            buf = b""
            while not self.stop.is_set():
                try:
                    d = c.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not d:
                    break
                buf += d
                i = buf.find(b"\xdf\xd3\xd3")
                if b"\xdc\xaa\xde" in buf:
                    self.rta_wanted = True
                if i >= 0 and len(buf) >= i + 7:
                    p = buf[i + 3:i + 7].replace(b"\xdf\xde", b"\xdf")  # a 0xdf data byte comes escaped
                    self.meter_to = (frm[0], (p[0] << 8) | p[1])
            c.close()

    def meter_loop(self):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self.stop.is_set():
            if self.meter_to and not self.silent:
                vals = []
                for n in range(1, 41):
                    lv = int(self.levels[n] * 256)
                    # input, output (right 6 dB lower), gate key and gain, dyn key and gain (ch 1: 5 dB of compression)
                    vals += [lv, lv, lv, lv - 256 * 6, lv, 0, lv, 64 if n == 1 else 0]
                out.sendto(struct.pack(">HH", 1, 0) + struct.pack(">%dh" % len(vals), *vals), self.meter_to)
                if self.rta_wanted:  # a gentle slope: -20 dB at 20 Hz down to -50 dB at 20 kHz
                    rta = [int((-20 - 30 * i / 119) * 256) for i in range(120)]
                    out.sendto(struct.pack(">HH", 2, 0) + struct.pack(">120h", *rta), self.meter_to)
            self.stop.wait(0.05)

    def close(self):
        self.stop.set()
        self.udp.close()
        self.tcp.close()


def wait(cond, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Unit(unittest.TestCase):
    def test_osc_round_trip(self):
        pkt = wm.osc("/ch/1/fdr", "-3.0", 0.675, -3.0)
        a, args = wm.parse(pkt)
        self.assertEqual(a, "/ch/1/fdr")
        self.assertEqual(args[0], "-3.0")
        self.assertAlmostEqual(args[2], -3.0)
        self.assertEqual(wm.parse(wm.osc("/?")), ("/?", []))
        self.assertEqual(len(wm.osc("/ch")) % 4, 0)

    def test_fader_law_matches_the_wing(self):
        # the Wing's own examples: -3 dB is 0.675, -2 is 0.70, 0 is 0.75, +3 is 0.825, +10 is 1
        for db, pos in ((-3, 0.675), (-2, 0.70), (0, 0.75), (3, 0.825), (4, 0.85), (10, 1.0)):
            self.assertAlmostEqual(wm.db_to_pos(db), pos)
            self.assertAlmostEqual(wm.pos_to_db(pos), db)
        self.assertEqual(wm.pos_to_db(0), -144.0)
        for db in (-80, -45, -20, -5):
            self.assertAlmostEqual(wm.pos_to_db(wm.db_to_pos(db)), db)

    def test_meter_request_escapes_df(self):
        w = wm.Wing(tempfile.mkdtemp(), {}, None)
        r = w.meter_request(0xDF10)
        self.assertTrue(r.startswith(b"\xdf\xd3\xd3\xdf\xde\x10\xd4"))
        self.assertTrue(r.endswith(b"\xde\xdf\xd1"))
        self.assertEqual(r.count(b"\xa0"), 40)


class Link(unittest.TestCase):
    def setUp(self):
        self.f = FakeWing()
        self.tmp = tempfile.mkdtemp()
        self.w = wm.Wing(self.tmp, {"wing_ip": "127.0.0.1", "wing_osc_port": self.f.osc_port, "wing_native_port": self.f.native_port}, _Log())
        self.w.start()

    def tearDown(self):
        self.w.close()
        self.f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starts_watching_and_reads_the_desk(self):
        self.assertEqual(self.w.settings["mode"], "watch")
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected" and self.w.strips["ch"][1].get("name") == "Lead Vox"
                             and self.w.strips["dca"][16].get("col") is not None and self.w.strips["ch"][40].get("src_in") is not None))
        st = self.w.state()
        self.assertEqual(st["console"]["name"], "Booth Wing")
        self.assertEqual(st["strips"]["ch"]["1"]["fdr"], 0.0)
        self.assertEqual(st["strips"]["ch"]["1"]["col"], 3)
        self.assertEqual(st["strips"]["ch"]["15"]["mute"], 1)
        self.assertEqual(st["strips"]["main"]["1"]["name"], "Main LR")
        self.assertEqual(st["strips"]["dca"]["1"]["name"], "Band")
        self.assertTrue(wait(lambda: self.w.state()["inputs"].get("LCL/1", {}).get("g") == 20.0))
        self.assertEqual(self.w.src(10), "A/2")
        self.assertIsNone(self.w.src(20))  # USB: no gain or phantom

    def test_watch_only_sends_nothing(self):
        wait(lambda: self.w.status()["status"] == "connected")
        self.assertIn("watch the Wing only", self.w.set("ch", 1, "fdr", -5.0))
        self.assertIn("watch the Wing only", self.w.fade([{"n": 1, "to": -10, "secs": 1}]))
        time.sleep(0.3)
        self.assertEqual(self.f.sets, [])

    def test_control_moves_the_desk(self):
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected"))
        self.assertIsNone(self.w.set("ch", 1, "fdr", -5.0))
        self.assertIsNone(self.w.set("ch", 1, "mute", True))
        self.assertTrue(wait(lambda: self.w.strips["ch"][1].get("fdr") == -5.0 and self.w.strips["ch"][1].get("mute") == 1))
        self.assertIn(("/ch/1/fdr", -5.0), self.f.sets)
        self.assertIn("−∞ to +10", self.w.set("ch", 1, "fdr", 20))
        self.assertIsNone(self.w.set("ch", 1, "pan", -40))
        self.assertTrue(wait(lambda: self.w.strips["ch"][1].get("pan") == -40))
        self.assertIn("−100 (left)", self.w.set("dca", 1, "pan", 0))
        self.assertIn("1 to 16", self.w.set("ch", 1, "name", "x" * 17))
        self.assertIn("no bus 17", self.w.set("bus", 17, "fdr", 0))

    def test_gain_and_phantom_go_to_the_channels_mic_input(self):
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.src(10) == "A/2"))
        self.assertIsNone(self.w.set("ch", 10, "gain", 32.5))
        self.assertIsNone(self.w.set("ch", 10, "vph", True))
        self.assertTrue(wait(lambda: self.w.inputs.get("A/2", {}).get("vph") == 1 and self.w.inputs["A/2"].get("g") == 32.5))
        self.assertIn(("/io/in/A/2/vph", 1), self.f.sets)
        self.assertTrue(wait(lambda: self.w.src_any(20) == "USB/20"))  # its source arrives in its own time
        self.assertIn("isn't fed by a mic input (it's USB)", self.w.set("ch", 20, "vph", True))
        self.assertIn("−3 to +60", self.w.set("ch", 10, "gain", 80))

    def test_changes_on_the_desk_show_at_once(self):
        self.assertTrue(wait(lambda: self.f.sub is not None and self.w.strips["ch"][5].get("name") is not None))
        v = self.w.version
        self.f.change("/ch/5/name", "Piano")
        self.assertTrue(wait(lambda: self.w.strips["ch"][5]["name"] == "Piano", 1))
        self.assertGreater(self.w.version, v)

    def test_channel_processing(self):
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected"))
        self.w.channel(3)
        self.assertTrue(wait(lambda: self.w.proc.get(3, {}).get("dyn/mdl") == "COMP" and self.w.proc[3].get("eq/4f") == 3990.5))
        p = self.w.channel(3)
        self.assertEqual((p["eq/on"], p["eq/1f"], p["gate/thr"], p["dyn/ratio"], p["flt/lcf"]), (0, 200.0, -40.0, 3.0, 100.2))
        for f, v in (("eq/on", True), ("eq/2g", -4.5), ("eq/2f", 320.0), ("flt/lc", True), ("flt/lcf", 120.0), ("gate/thr", -50.0),
                     ("dyn/on", True), ("dyn/thr", -18.0), ("dyn/ratio", 3.8), ("in/set/trim", -6.0), ("in/set/inv", True)):
            self.assertIsNone(self.w.set("ch", 3, f, v), f)
        self.assertTrue(wait(lambda: self.w.proc[3].get("dyn/ratio") == 4.0 and self.w.proc[3].get("eq/2g") == -4.5 and self.w.proc[3].get("in/set/inv") == 1))
        self.assertIn(("/ch/3/dyn/ratio", 4.0), self.f.sets)  # snapped to the Wing's own step
        self.assertIn("outside what the Wing allows (-15 to 15)", self.w.set("ch", 3, "eq/1g", 20))
        self.assertIn("plugin model on the Wing", self.w.set("ch", 3, "eq/mdl", "SOUL"))
        self.assertIn("on channels", self.w.set("bus", 1, "eq/1g", 2))
        self.w.channel(4)
        self.assertTrue(wait(lambda: self.w.proc.get(4, {}).get("eq/mdl") == "SOUL"))
        # a change on the desk shows while the channel is open
        self.f.change("/ch/3/gate/on", 1)
        self.assertTrue(wait(lambda: self.w.proc[3].get("gate/on") == 1, 2))

    def test_fades_run_here(self):
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.strips["ch"][1].get("fdr") == 0.0))
        self.assertIsNone(self.w.fade([{"n": 1, "to": -10.0, "secs": 0.5}, {"n": 15, "from": -144, "mute": False, "to": 0.0, "secs": 0.5}]))
        self.assertTrue(wait(lambda: self.f.vals["/ch/1/fdr"] == -10.0 and self.f.vals["/ch/15/fdr"] == 0.0, 3))
        self.assertEqual(self.f.vals["/ch/15/mute"], 0)
        steps = [v for a, v in self.f.sets if a == "/ch/1/fdr"]
        self.assertGreater(len(steps), 4)  # moved in steps, not jumped
        # fade out then off, fader back where it was
        self.assertIsNone(self.w.fade([{"n": 1, "to": -144, "secs": 0.3, "then_mute": True, "then_fdr": -10.0}]))
        self.assertTrue(wait(lambda: self.f.vals["/ch/1/mute"] == 1 and self.f.vals["/ch/1/fdr"] == -10.0, 3))

    def test_meters(self):
        self.assertTrue(wait(lambda: self.w.meter_state()["meters"]))
        m = self.w.meter_state()["ch"]
        self.assertEqual(m["1"], -12.0)
        self.assertEqual(m["2"], -60.0)
        self.assertEqual(self.w.meter_state()["lr"]["1"], [-12.0, -18.0])  # left and right, for stereo channels
        self.assertTrue(wait(lambda: self.w.inputs.get("USB/20", {}).get("mode") == "ST"))
        self.assertEqual(self.w.inputs["LCL/1"]["mode"], "M")

    def test_gain_reduction_history(self):
        self.w.channel(1)
        self.assertTrue(wait(lambda: len(self.w.history(1)) > 5))
        t, i, o, gk, ggr, dk, dgr = self.w.history(1)[-1]
        self.assertEqual((i, o, dk, dgr, ggr), (-12.0, -12.0, -12.0, 5.0, 0.0))  # 64/256 of 20 dB
        self.assertEqual(self.w.history(2), [])  # only channels someone has open
        last = self.w.history(1)[-1][0]
        self.assertTrue(all(x[0] > last for x in self.w.history(1, since=last)))

    def test_rta(self):
        self.assertTrue(wait(lambda: self.w.rta_state()["fresh"]))
        r = self.w.rta_state()
        self.assertEqual(len(r["rta"]), 120)
        self.assertEqual((r["rta"][0], r["rta"][119]), (-20.0, -50.0))
        self.assertIn("watch the Wing only", self.w.rta_point(3, "POSTEQ"))
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected"))
        self.assertIsNone(self.w.rta_point(3, "POSTEQ"))
        self.assertTrue(wait(lambda: self.w.rta_state()["src"] == 3 and self.w.rta_state()["tap"] == "POSTEQ"))
        self.assertEqual(self.f.vals["/cfg/rta/rtasrc"], 2)
        self.assertIn("before or after the EQ", self.w.rta_point(3, "SOMEWHERE"))

    def test_rta_index_counted_from_one(self):
        self.f.rta_base = 0  # a Wing whose RTA index counts from 1
        self.w.configure(mode="control")
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected"))
        self.assertIsNone(self.w.rta_point(5, "PREEQ"))
        self.assertTrue(wait(lambda: self.w.rta_state()["src"] == 5))  # read back, then corrected once
        self.assertEqual(self.f.vals["/cfg/rta/rtasrc"], 5)

    def test_no_answer(self):
        self.assertTrue(wait(lambda: self.w.status()["status"] == "connected"))
        self.f.silent = True
        self.w.seen = time.time() - 7
        self.assertEqual(self.w.status()["status"], "no answer")
        self.w.configure(mode="control")
        self.assertIn("isn't answering", self.w.set("ch", 1, "mute", True))

    def test_settings_are_kept(self):
        self.w.configure(ip="10.0.0.9", mode="control")
        w2 = wm.Wing(self.tmp, {}, _Log())
        self.assertEqual(w2.settings, {"ip": "10.0.0.9", "mode": "control"})
        self.assertIn("off, watch or control", self.w.configure(mode="loud"))

    def test_find(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind(("127.0.0.1", 0))

        def answer():
            d, frm = u.recvfrom(100)
            if d == b"WING?":
                u.sendto(b"WING,127.0.0.1,Booth Wing,ngc-full,S123,3.0.6", frm)
        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(wm.find(port=u.getsockname()[1], timeout=1, target="127.0.0.1"),
                         [{"ip": "127.0.0.1", "name": "Booth Wing", "model": "ngc-full", "serial": "S123", "firmware": "3.0.6"}])
        u.close()


class _Log:
    def info(self, *a):
        pass

    warning = error = info


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
        self.tok = {n: self.login(n) for n in ("Admin", "FOH sound", "ProPresenter", "Viewer")}

    def tearDown(self):
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "" if name == "Viewer" else "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

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

    def test_who_can_see_and_change(self):
        self.assertTrue(wait(lambda: self.call("/api/wing")[1].get("status") == "connected"))
        code, d = self.call("/api/wing", who="FOH sound")
        self.assertEqual((code, d["mode"], d["can_control"]), (200, "watch", True))
        self.assertEqual(self.call("/api/wing", who="Viewer")[0], 403)  # Viewer has no Mixer access
        self.assertEqual(self.call("/api/wing/settings", "PUT", {"mode": "control"}, who="FOH sound")[0], 403)
        code, d = self.call("/api/wing/set", "POST", {"kind": "ch", "n": 1, "field": "fdr", "value": -6.0}, who="FOH sound")
        self.assertEqual(code, 409)
        self.assertIn("watch the Wing only", d["error"])
        self.assertEqual(self.call("/api/wing/settings", "PUT", {"mode": "control"})[0], 200)
        self.assertEqual(self.call("/api/wing/set", "POST", {"kind": "ch", "n": 1, "field": "fdr", "value": -6.0}, who="FOH sound")[0], 200)
        self.assertTrue(wait(lambda: self.call("/api/wing")[1]["strips"]["ch"]["1"].get("fdr") == -6.0))
        self.assertEqual(self.call("/api/wing/set", "POST", {"kind": "ch", "n": 1, "field": "mute", "value": True}, who="ProPresenter")[0], 403)
        # slide cues: ProPresenter has Macros view only, so it can't run fades either
        self.assertEqual(self.call("/api/wing/fade", "POST", {"moves": [{"n": 1, "to": 0, "secs": 0}]}, who="ProPresenter")[0], 403)
        self.assertEqual(self.call("/api/wing/fade", "POST", {"moves": [{"n": 1, "to": 0, "secs": 0}]}, who="FOH sound")[0], 200)

    def test_since_and_meters(self):
        self.assertTrue(wait(lambda: self.call("/api/wing")[1].get("status") == "connected"))
        v = self.call("/api/wing")[1]["version"]
        d = self.call("/api/wing?since=%d" % v)[1]
        self.assertTrue(d["changed"] is False or d["version"] != v)
        self.assertTrue(wait(lambda: self.call("/api/wing/meters")[1].get("meters")))
        self.assertEqual(self.call("/api/wing/meters")[1]["ch"]["1"], -12.0)

    def test_cue_run_check_says_what_the_wing_does(self):
        self.assertTrue(wait(lambda: self.call("/api/wing")[1].get("status") == "connected"))
        by = lambda: {x["what"]: x for x in self.call("/api/cues/check")[1]["lines"]}
        self.assertIn("watching only", by()["Wing"]["text"])
        self.assertIsNone(by()["Wing"]["ok"])
        self.call("/api/wing/settings", "PUT", {"mode": "control"})
        self.assertTrue(by()["Wing"]["ok"])
        self.assertIn("cues move the desk itself", by()["Wing"]["text"])

    def test_channel_api(self):
        self.assertTrue(wait(lambda: self.call("/api/wing")[1].get("status") == "connected"))
        self.call("/api/wing/channel?n=2")
        self.assertTrue(wait(lambda: self.call("/api/wing/channel?n=2")[1]["proc"].get("eq/mdl") == "STD"))
        self.assertEqual(self.call("/api/wing/channel?n=41")[0], 400)
        self.assertEqual(self.call("/api/wing/channel?n=2", who="Viewer")[0], 403)
        self.assertEqual(self.call("/api/wing/set", "POST", {"kind": "ch", "n": 2, "field": "eq/1g", "value": 3.0}, who="FOH sound")[0], 409)  # watch only
        self.call("/api/wing/settings", "PUT", {"mode": "control"})
        self.assertEqual(self.call("/api/wing/set", "POST", {"kind": "ch", "n": 2, "field": "eq/1g", "value": 3.0}, who="FOH sound")[0], 200)
        self.assertTrue(wait(lambda: self.call("/api/wing/channel?n=2")[1]["proc"].get("eq/1g") == 3.0))
        self.assertEqual(self.call("/api/wing/set", "POST", {"kind": "ch", "n": 2, "field": "dyn/thr", "value": -20.0}, who="ProPresenter")[0], 403)

    def test_settings(self):
        self.assertEqual(self.call("/api/wing/settings", "PUT", {"ip": "not an ip"})[0], 400)
        code, d = self.call("/api/wing/settings", "PUT", {"ip": "10.1.2.3", "mode": "watch"})
        self.assertEqual((code, d["ip"], d["mode"]), (200, "10.1.2.3", "watch"))
        self.assertEqual(self.call("/api/wing/settings", "PUT", {"mode": "off"})[1]["status"], "off")


if __name__ == "__main__":
    unittest.main()
