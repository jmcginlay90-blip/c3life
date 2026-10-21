"""Tests for MIDI from MultiTracks Playback into Lights: a pretend Mac joins the network MIDI session the way Audio MIDI
Setup does, then sends Playback's MIDI cues; a pretend Lightkey keeps what C3 Life sends it.
Run: python3 -m unittest -v test_midi_in.py"""

import json
import logging
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
import lightkey as lk
import midi_in as mi
from test_lightkey import FakeLightkey, show


def rtp(cmds, seq=1, ssrc=0x1234ABCD, z=False):
    """An RTP-MIDI packet: the commands, each after a delta time of 0 (the first one only when z)."""
    body = b""
    for i, c in enumerate(cmds):
        if i or z:
            body += b"\x00"
        body += bytes(c)
    head = struct.pack(">BBHII", 0x80, 0x61, seq, int(time.time() * 1000) & 0xFFFFFFFF, ssrc)
    flags = len(body) if len(body) <= 15 else None
    if flags is None:
        sec = bytes([0x80 | (0x20 if z else 0) | (len(body) >> 8), len(body) & 0xFF]) + body
    else:
        sec = bytes([flags | (0x20 if z else 0)]) + body
    return head + sec


class Parse(unittest.TestCase):
    def test_messages_running_status_and_sysex(self):
        p = rtp([[0xCF, 2], [0x9F, 60, 100], [61, 90], [0xF0, 1, 2, 3, 0xF7], [0xBF, 7, 64], [0xF8]])
        self.assertEqual(mi.parse_rtp_midi(p), [(0xCF, b"\x02"), (0x9F, bytes([60, 100])), (0x9F, bytes([61, 90])), (0xBF, bytes([7, 64])), (0xF8, b"")])

    def test_long_list_and_a_first_delta(self):
        cmds = [[0x90, n, 100] for n in range(40, 50)]
        self.assertEqual(len(mi.parse_rtp_midi(rtp(cmds, z=True))), 10)

    def test_not_rtp_midi(self):
        self.assertEqual(mi.parse_rtp_midi(b"\xff\xffIN" + b"\0" * 20), [])
        self.assertEqual(mi.parse_rtp_midi(b"hello"), [])

    def test_names_like_playback(self):
        self.assertEqual(mi.note_name(60), "C3")
        self.assertEqual(mi.note_name(61), "C#3")

    def test_settings(self):
        sh = show()
        ok = dict(mi.DEFAULT, notes=[{"note": 60, "do": "go"}, {"note": 62, "do": "look", "id": "l1"}])
        self.assertIsNone(mi.valid(ok, sh))
        for bad in ({"channel": 17}, {"pc_from": 2}, {"cc_master": 128}, {"notes": [{"note": 60, "do": "go"}, {"note": 60, "do": "back"}]},
                    {"notes": [{"note": 60, "do": "dance"}]}, {"notes": [{"note": 62, "do": "look", "id": "gone"}]}, {"follow": "yes"}):
            self.assertIsNotNone(mi.valid(dict(ok, **bad), sh), bad)


class Mac:
    """Audio MIDI Setup on the Mac with Playback: joins the session, keeps the clocks in step, sends MIDI."""

    def __init__(self, port, name="MacBook Pro"):
        self.port, self.ssrc = port, 0x1234ABCD
        for _ in range(30):  # two ports side by side, like a Mac's session
            self.ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.ctl.bind(("127.0.0.1", 0))
            self.dat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.dat.bind(("127.0.0.1", self.ctl.getsockname()[1] + 1))
                break
            except (OSError, OverflowError):
                self.ctl.close()
                self.dat.close()
        for x in (self.ctl, self.dat):
            x.settimeout(2)
        self.name, self.seq = name, 0

    def join(self):
        out = []
        for sock, p in ((self.ctl, self.port), (self.dat, self.port + 1)):
            sock.sendto(b"\xff\xffIN" + struct.pack(">III", 2, 77, self.ssrc) + self.name.encode() + b"\0", ("127.0.0.1", p))
            out.append(sock.recvfrom(1024)[0])
        # clock sync: we send CK 0, it answers CK 1 with its time
        self.dat.sendto(b"\xff\xffCK" + struct.pack(">IB", self.ssrc, 0) + b"\0\0\0" + struct.pack(">Q", 12345) + b"\0" * 16, ("127.0.0.1", self.port + 1))
        out.append(self.dat.recvfrom(1024)[0])
        return out

    def send(self, *cmds):
        self.seq += 1
        self.dat.sendto(rtp(list(cmds), self.seq, self.ssrc), ("127.0.0.1", self.port + 1))

    def bye(self):
        self.ctl.sendto(b"\xff\xffBY" + struct.pack(">III", 2, 77, self.ssrc), ("127.0.0.1", self.port))

    def close(self):
        self.ctl.close()
        self.dat.close()


def wait(cond, secs=4):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Session(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeLightkey()
        self.k = lk.Lightkey(self.tmp)
        self.k.save_show(show())
        self.k.save_settings(dict(lk.SETTINGS, ip="127.0.0.1", port=self.fake.port, mode="control"))
        self.m = mi.Midi(self.tmp, {"midi_port": 0}, logging.getLogger("test"), self.k)
        self.m.start()
        self.assertTrue(self.m.ctl, self.m.error)
        self.mac = Mac(self.m.port)

    def tearDown(self):
        self.mac.close()
        self.m.close()
        self.k.close()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def heard(self):
        return self.m.view()["heard"]

    def test_the_mac_joins_and_leaves(self):
        ok1, ok2, ck = self.mac.join()
        self.assertEqual((ok1[:4], ok2[:4]), (b"\xff\xffOK", b"\xff\xffOK"))
        self.assertIn(b"C3 Life\0", ok1)
        self.assertEqual(struct.unpack(">I", ok1[8:12])[0], 77)  # its token back
        self.assertEqual((ck[:4], ck[8]), (b"\xff\xffCK", 1))
        self.assertEqual(ck[12:20], struct.pack(">Q", 12345))
        self.assertEqual([(p["name"], p["live"]) for p in self.m.view()["peers"]], [("MacBook Pro", True)])
        self.mac.bye()
        self.assertTrue(wait(lambda: self.m.view()["peers"] == []))

    def test_follow_off_only_says_what_it_would_do(self):
        self.mac.join()
        self.mac.send([0xCF, 1])  # Program 2 (Playback counts from 1) on channel 16
        self.assertTrue(wait(lambda: self.heard()))
        h = self.heard()[0]
        self.assertEqual((h["msg"], h["ch"], h["sent"]), ("Program 2", 16, False))
        self.assertEqual(h["did"], "cue Worship big (not sent: Follow Playback is off)")
        time.sleep(0.2)
        self.assertEqual(self.fake.got, [])

    def test_following_playback(self):
        self.mac.join()
        self.m.save(dict(mi.DEFAULT, follow=True, channel=16, cc_master=7, notes=[{"note": 60, "do": "go"}, {"note": 62, "do": "look", "id": "l1"}, {"note": 64, "do": "blackout_on"}]))
        self.mac.send([0xCF, 0])                    # Program 1: cue 1
        self.mac.send([0x9F, 60, 100], [60, 0])     # C3: Go (and its note off does nothing)
        self.mac.send([0xBF, 7, 127])               # CC 7: the master, full
        self.mac.send([0x9F, 64, 90])               # E3: blackout
        self.mac.send([0x90, 60, 100])              # channel 1: not ours
        self.assertTrue(wait(lambda: len(self.fake.got) >= 5))
        got = [m[0] for m in self.fake.msgs()]
        self.assertEqual(got[0], "/live/Main_Page/cue/Pre-service/activate")
        self.assertIn("/live/selected/cue/Worship_big/activate", got)  # Go: the next one
        self.assertIn(("/output/master", [1.0]), self.fake.msgs())
        self.assertEqual(self.fake.msgs()[-1], ("/output/master", [0.0]))
        h = self.heard()
        self.assertEqual(h[0]["did"], "Blackout on")
        self.assertTrue(all(x["sent"] for x in h))
        self.assertEqual(len(h), 4)  # channel 1 wasn't for us

    def test_a_program_with_no_cue(self):
        self.mac.join()
        self.m.save(dict(mi.DEFAULT, follow=True))
        self.mac.send([0xC0, 50])
        self.assertTrue(wait(lambda: self.heard()))
        self.assertIn("no cue 51", self.heard()[0]["did"])

    def test_tempo_from_the_midi_clock(self):
        self.mac.join()
        self.m.save(dict(mi.DEFAULT, follow=True, tempo=True))
        beat = 60.0 / 120  # 120 BPM: 24 clocks a beat, each with the time it arrived (Windows can't sleep 21 ms steps)
        t0 = time.time() - 60 * beat / 24
        for i in range(60):
            self.m.handle(0xF8, b"", now=t0 + i * beat / 24)
        self.assertTrue(wait(lambda: any(m[0] == "/beat/tempo" for m in self.fake.msgs())))
        bpm = [m[1][0] for m in self.fake.msgs() if m[0] == "/beat/tempo"][0]
        self.assertAlmostEqual(bpm, 120, delta=4)

    def test_only_the_booth_network(self):
        self.m.allowed = lambda ip: False
        self.mac.ctl.settimeout(0.5)
        self.mac.ctl.sendto(b"\xff\xffIN" + struct.pack(">III", 2, 77, 1) + b"x\0", ("127.0.0.1", self.m.port))
        with self.assertRaises(socket.timeout):
            self.mac.ctl.recvfrom(1024)

    def test_kept(self):
        self.m.save(dict(mi.DEFAULT, follow=True, channel=3))
        m2 = mi.Midi(self.tmp, {"midi_port": 0}, logging.getLogger("test"), self.k)
        self.assertEqual((m2.settings()["follow"], m2.settings()["channel"]), (True, 3))


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", wing_ip="", tio_ip="")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Lighting", "FOH sound", "Viewer"):
            body = {"name": n, "pin": "" if n == "Viewer" else "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="Lighting"):
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

    def test_who_may_look_and_change(self):
        self.assertEqual(self.call("/api/lights/midi", who="Viewer")[0], 403)
        st, v = self.call("/api/lights/midi", who="FOH sound")
        self.assertEqual((st, v["session"], v["settings"]["follow"]), (200, "C3 Life", False))
        self.assertTrue(v["port"])
        self.assertEqual(self.call("/api/lights/midi", "PUT", {"follow": True}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/lights/midi", "PUT", {"channel": 99})[0], 400)
        st, v = self.call("/api/lights/midi", "PUT", {"follow": True, "channel": 16})
        self.assertEqual((st, v["settings"]["follow"], v["settings"]["channel"]), (200, True, 16))


if __name__ == "__main__":
    unittest.main()
