"""Tests for the run sheet (macros.py): steps that run in order only when someone presses Run, stop at a step that
doesn't work, Stop, Undo, and Playback steps sent as network MIDI. Run: python3 -m unittest -v test_macros.py"""

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
import macros as mm
import midi_in as mi
from test_midi_in import Mac


class FakeDesk:
    KINDS = {"ch": 40, "aux": 8, "bus": 16, "main": 4, "mtx": 8, "dca": 16}

    def __init__(self):
        self.strips = {"ch": {"15": {"name": "Pastor HH", "mute": 1, "fdr": -144.0}}, "dca": {"1": {"name": "Band", "mute": 0, "fdr": 0.0}}}
        self.sets, self.fades, self.fail = [], [], {}

    def state(self):
        return {"status": "connected", "mode": "control", "strips": json.loads(json.dumps(self.strips))}

    def set(self, kind, n, field, v):
        if (kind, n, field) in self.fail:
            return self.fail[(kind, n, field)]
        self.sets.append((kind, n, field, v))
        if field in ("mute", "fdr"):
            self.strips.setdefault(kind, {}).setdefault(str(n), {})[field] = (1 if v else 0) if field == "mute" else v
        return None

    def fade(self, moves):
        self.fades.append(moves)
        for m in moves:
            self.strips.setdefault(m["kind"], {}).setdefault(str(m["n"]), {})["fdr"] = m["to"]
        return None

    def bus_sends(self, b):
        return {"15": {"on": 0, "lvl": -144.0}}


class FakeFoyer:
    def __init__(self):
        self.l = {"source": "a", "fdr": -10.0, "mute": 0}
        self.done = []

    def live(self, desk):
        return dict(self.l)

    def do(self, desk, body, who, log=None):
        self.done.append(body)
        if body["do"] == "source":
            self.l["source"] = body["to"]
        elif body["do"] == "volume":
            self.l["fdr"] = body["db"]
        else:
            self.l["mute"] = 1 if body["on"] else 0
        return None

    def view(self, desk):
        return {"name": "Foyer", "mix_a_name": "Spotify", "mix_b_name": "Live service"}


class FakeMusic:
    def __init__(self):
        self.done, self.err = [], None

    def do(self, body, who):
        self.done.append(body)
        return self.err

    def get(self):
        return {"playlists": [{"name": "Walk-in", "uri": "spotify:playlist:x"}]}


class FakeLights:
    def __init__(self):
        self.master, self.blackout, self.done = 1.0, False, []
        self.show = {"cues": [{"id": "c1", "name": "Pre-service"}], "looks": []}

    def do(self, act, who=""):
        self.done.append(act)
        if act["do"] == "master":
            self.master = act["value"]
        if act["do"] == "blackout":
            self.blackout = act["on"]
        return True, None, []


class FakeMidi:
    def __init__(self):
        self.sent, self.err = [], None

    def send(self, msg, to=None):
        if self.err:
            return self.err
        self.sent.append(list(msg))
        return None


class Log:
    def info(self, *a):
        pass

    warning = info


def wait_done(m, secs=5):
    for _ in range(int(secs * 50)):
        r = m.view()["run"]
        if r and r["done"]:
            return r
        time.sleep(0.02)
    return m.view()["run"]


class Steps(unittest.TestCase):
    def test_the_starters_and_whats_refused(self):
        self.assertIsNone(mm.valid({"macros": mm.STARTERS, "playback": mm.PLAYBACK}))
        good = {"dev": "wing", "do": "fade", "kind": "dca", "n": 1, "db": -144, "secs": 3, "label": "Fade DCA 1 out"}
        self.assertIsNone(mm.check_step(good))
        for bad in ({"dev": "companion", "label": "x"}, dict(good, secs=0), dict(good, kind="fx"), dict(good, label=""),
                    {"dev": "wing", "do": "send", "kind": "bus", "n": 1, "bus": 2, "db": 0, "label": "x"}, {"dev": "wait", "secs": 90, "label": "x"},
                    {"dev": "playback", "do": "rewind", "label": "x"}, {"dev": "foyer", "do": "source", "to": "c", "label": "x"}):
            self.assertIsNotNone(mm.check_step(bad), bad)
        self.assertIsNotNone(mm.valid({"macros": [{"id": "a", "name": "A", "color": "#FFFFFF", "steps": []}] * 2}))  # the same id twice
        self.assertIsNotNone(mm.valid({"macros": [], "playback": {"channel": 16, "notes": {"play": 1, "stop": 1, "next": 2, "previous": 3}}}))


class Running(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.desk, self.foyer, self.music, self.lights, self.midi = FakeDesk(), FakeFoyer(), FakeMusic(), FakeLights(), FakeMidi()
        self.m = mm.Macros(self.tmp, {}, Log(), desk=self.desk, music=self.music, foyer=self.foyer, lights=self.lights, midi=self.midi)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, steps):
        self.m.save({"macros": [{"id": "m1", "name": "Message", "color": "#6CB4F5", "steps": steps}], "playback": mm.PLAYBACK}, "FOH")

    def test_nothing_runs_until_run_then_in_order_and_undo(self):
        self.put([{"dev": "wing", "do": "unmute", "kind": "ch", "n": 15, "label": "Unmute Ch 15"},
                  {"dev": "wing", "do": "fade", "kind": "dca", "n": 1, "db": -144, "secs": 3, "label": "Fade DCA 1 out"},
                  {"dev": "wing", "do": "send", "kind": "ch", "n": 15, "bus": 5, "db": 0, "label": "Ch 15 to the stream"},
                  {"dev": "foyer", "do": "source", "to": "b", "label": "Foyer: Live"},
                  {"dev": "lights", "do": "master", "value": 0.4, "label": "Master to 40%"},
                  {"dev": "music", "do": "fade", "to": 0, "seconds": 10, "then": "pause", "label": "Fade out"},
                  {"dev": "playback", "do": "play", "label": "Play"}])
        time.sleep(0.2)
        self.assertEqual((self.desk.sets, self.foyer.done, self.midi.sent), ([], [], []))  # saved, but nothing ran
        self.assertIsNone(self.m.run("m1", "FOH"))
        r = wait_done(self.m)
        self.assertEqual((r["done"], r["error"], len(r["log"])), (True, "", 7))
        self.assertEqual(self.desk.sets[:1], [("ch", 15, "mute", False)])
        self.assertEqual(self.desk.fades[0], [{"kind": "dca", "n": 1, "to": -144.0, "secs": 3.0}])
        self.assertIn(("ch", 15, "send/5/lvl", 0.0), self.desk.sets)
        self.assertEqual((self.foyer.l["source"], self.lights.master, self.music.done[0]["do"]), ("b", 0.4, "fade"))
        self.assertEqual(self.midi.sent, [[0x9F, 60, 100], [0x8F, 60, 0]])  # channel 16, note 60
        self.assertTrue(r["can_undo"])
        self.assertEqual([u["can"] for u in r["undo"]], [True, True, True, True, True, False, False])
        self.assertIsNone(self.m.undo("FOH"))
        self.assertEqual((self.desk.strips["ch"]["15"]["mute"], self.desk.strips["dca"]["1"]["fdr"], self.foyer.l["source"], self.lights.master), (1, 0.0, "a", 1.0))
        self.assertIn(("ch", 15, "send/5/lvl", -144.0), self.desk.sets)
        self.assertIn("already been undone", self.m.undo("FOH"))

    def test_it_stops_at_a_step_that_doesnt_work(self):
        self.desk.fail[("dca", 1, "mute")] = "The app is set to watch the Wing only."
        self.put([{"dev": "wing", "do": "mute", "kind": "dca", "n": 1, "label": "Mute the band"},
                  {"dev": "foyer", "do": "source", "to": "b", "label": "Foyer: Live"}])
        self.m.run("m1", "FOH")
        r = wait_done(self.m)
        self.assertEqual(r["error"], "Stopped at step 1 (Mute the band): The app is set to watch the Wing only.")
        self.assertEqual(self.foyer.done, [])  # step 2 never ran
        self.midi.err = "Playback isn't connected."
        self.put([{"dev": "playback", "do": "stop", "label": "Stop"}])
        self.m.run("m1", "FOH")
        self.assertIn("Playback isn't connected", wait_done(self.m)["error"])

    def test_stop_during_a_wait_and_only_one_at_a_time(self):
        self.put([{"dev": "wait", "secs": 30, "label": "Wait 30s"}, {"dev": "lights", "do": "go", "label": "Go"}])
        self.assertIsNone(self.m.run("m1", "FOH"))
        time.sleep(0.1)
        self.assertIn("still running", self.m.run("m1", "FOH"))
        t0 = time.time()
        self.assertIsNone(self.m.stop("FOH"))
        r = wait_done(self.m)
        self.assertLess(time.time() - t0, 2)
        self.assertEqual((r["stopped"], self.lights.done), (True, []))
        self.assertIn("Nothing is running", self.m.stop("FOH"))

    def test_choices_come_from_whats_set_up_and_are_kept(self):
        c = self.m.choices()["steps"]
        self.assertIn("Play playlist: Walk-in", [x["label"] for x in c["music"]])
        self.assertIn("Foyer: Live service", [x["label"] for x in c["foyer"]])
        self.assertIn("Fire cue: Pre-service", [x["label"] for x in c["lights"]])
        self.assertTrue(all(mm.check_step(x) is None for d in c.values() for x in d))  # everything offered can run
        self.assertEqual(self.m.choices()["wing"]["names"]["ch"]["15"], "Pastor HH")
        self.put([{"dev": "lights", "do": "go", "label": "Go"}])
        self.assertEqual(mm.Macros(self.tmp, {}, Log()).get()["macros"][0]["name"], "Message")


class PlaybackMidi(unittest.TestCase):
    """The Playback steps really go out over the network MIDI session Playback joined."""

    def test_notes_reach_the_mac(self):
        tmp = tempfile.mkdtemp()
        m = mi.Midi(tmp, {"midi_port": 0}, Log(), lights=None, allowed=lambda ip: True)
        self.assertIn("isn't running", m.send([0x9F, 60, 100]))
        m.start()
        mac = Mac(m.port)
        try:
            self.assertIn("isn't connected", m.send([0x9F, 60, 100]))
            mac.join()
            self.assertIsNone(m.send([0x9F, 62, 100]))
            pkt = mac.dat.recvfrom(1024)[0]
            self.assertEqual(mi.parse_rtp_midi(pkt), [(0x9F, bytes([62, 100]))])
        finally:
            mac.close(); m.close(); shutil.rmtree(tmp, ignore_errors=True)


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("FOH sound", "Keys (musician)", "ProPresenter", "Viewer"):
            body = {"name": n, "pin": "" if n == "Viewer" else "1234"}
            self.tok[n] = self.call("/api/login", "POST", body)[1].get("token")

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_may_and_a_run_through_the_server(self):
        code, j = self.call("/api/macros", who="FOH sound")
        self.assertEqual((code, j["can"], [m["name"] for m in j["macros"]][:2]), (200, True, ["Walk-in", "Worship start"]))
        self.assertIn("steps", j["choices"])
        self.assertEqual(self.call("/api/macros", who="Keys (musician)")[0], 403)
        st, j = self.call("/api/macros", who="ProPresenter")  # view only
        self.assertEqual((st, j["can"], "choices" in j), (200, False, False))
        self.assertEqual(self.call("/api/macros/run", "POST", {"id": "walkin"}, who="ProPresenter")[0], 403)
        self.assertEqual(self.call("/api/macros", who="Viewer")[0], 403)
        bad = {"macros": [{"id": "x", "name": "X", "color": "#FFFFFF", "steps": [{"dev": "companion", "label": "Press"}]}]}
        code, j = self.call("/api/macros", "PUT", bad, who="FOH sound")
        self.assertEqual(code, 400); self.assertIn("step 1", j["error"])
        good = {"macros": [{"id": "x", "name": "X", "color": "#FFFFFF", "steps": [{"dev": "wing", "do": "mute", "kind": "ch", "n": 1, "label": "Mute Ch 1"}]}]}
        self.assertEqual(self.call("/api/macros", "PUT", good, who="FOH sound")[0], 200)
        code, j = self.call("/api/macros/run", "POST", {"id": "x"}, who="FOH sound")
        self.assertEqual(code, 200)
        for _ in range(50):
            j = self.call("/api/macros", who="FOH sound")[1]
            if j["run"]["done"]:
                break
            time.sleep(0.05)
        self.assertIn("Stopped at step 1 (Mute Ch 1)", j["run"]["error"])  # no sound desk here: it says so, and stops


if __name__ == "__main__":
    unittest.main()
