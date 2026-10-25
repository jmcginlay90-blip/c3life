"""Tests for patching (patching.py, the recorder's patch and playback, patchlist.py), with a fake Dante network laid
out like the church's: two Tios and the Multitracks Mac going to the Wing card, "Mon-" copies of some inputs on more
card inputs, stereo Keys, and the C3 Life server taking the same sources straight from Dante.
Run: python3 -m unittest -v test_patching.py"""

import csv
import io
import logging
import os
import shutil
import tempfile
import time
import unittest
import wave

import audio_engine as ae
import patching
import patchlist
import recorder as rec
from test_recorder import wait

LOG = logging.getLogger("test")
WING, TIO, MT, ME = "wing-d-2627f4", "Y001-box-1-Yamaha-Tio1608", "C3-LIfe-Multitracks", "C3LIFE"


class FakeDante:
    def __init__(self):
        self.r = [  # every route on the network: (rx device, rx channel, tx device, tx channel)
            (WING, "06 - Bass", TIO, "CH9 Bass"), (WING, "43 - SR W1", TIO, "CH9 Bass"),          # Bass and Mon-Bass
            (WING, "09 - Keys L", TIO, "CH10 Keys L"), (WING, "10 - Keys R", TIO, "CH11 Keys R"),  # Keys (stereo)
            (WING, "55", TIO, "CH10 Keys L"), (WING, "56", TIO, "CH11 Keys R"),                    # Mon-Keys (stereo)
            (WING, "29 - M-Keys L", MT, "07 - Keys & Piano"), (WING, "30 - M-Kyes R", MT, "08 - Keys & Piano"),
            (ME, "01", TIO, "CH9 Bass"), (ME, "02", TIO, "CH10 Keys L"), (ME, "03", TIO, "CH11 Keys R"),
            (ME, "04", MT, "07 - Keys & Piano"), (ME, "05", MT, "08 - Keys & Piano"), (ME, "06", WING, "61 - FOH L"),
            ("Y002-box-2-Yamaha-Tio1608", "Sub", WING, "63 - Sub L")]
        self.calls = []

    def routes(self):
        return [{"rx_device": a, "rx_channel": b, "tx_device": c, "tx_channel": d} for a, b, c, d in self.r]

    def all_routes(self):
        return self.routes()

    def get(self):
        return {"status": "ok", "device_name": ME, "channels": {str(int(b)): {"tx_device": c, "tx_channel": d}
                                                                for a, b, c, d in self.r if a == ME}}

    def channels(self):
        wrx = {6: "06 - Bass", 43: "43 - SR W1", 9: "09 - Keys L", 10: "10 - Keys R", 55: "55", 56: "56", 29: "29 - M-Keys L", 30: "30 - M-Kyes R"}
        return {WING: {"tx": {61: "61 - FOH L", 63: "63 - Sub L"}, "rx": wrx},
                TIO: {"tx": {9: "CH9 Bass", 10: "CH10 Keys L", 11: "CH11 Keys R", 12: "CH12 A Guitar"}, "rx": {}},
                MT: {"tx": {7: "07 - Keys & Piano", 8: "08 - Keys & Piano"}, "rx": {}},
                ME: {"tx": {k: "%02d" % k for k in range(1, 9)}, "rx": {k: "%02d" % k for k in range(1, 9)}}}

    def tx_channels(self):
        return {d: c["tx"] for d, c in self.channels().items()}

    def dvs_name(self):
        return "c3life"

    def _set(self, rx_dev, rx_ch, tx_dev, tx_ch):
        self.r = [x for x in self.r if not (x[0] == rx_dev and x[1] == rx_ch)]
        if tx_dev:
            self.r.append((rx_dev, rx_ch, tx_dev, tx_ch))

    def route_any(self, rx_device, rx_channel, tx_device, tx_channel):
        self.calls.append(("route", rx_device, rx_channel, tx_device, tx_channel))
        self._set(rx_device, rx_channel, tx_device, tx_channel)

    def unroute_any(self, rx_device, rx_channel):
        self.calls.append(("unroute", rx_device, rx_channel))
        self._set(rx_device, rx_channel, None, None)

    def route(self, tx_device, tx_channel, k):
        self.calls.append(("route-me", k, tx_device, tx_channel))
        self._set(ME, "%02d" % k, tx_device, tx_channel)

    def unroute(self, k):
        self._set(ME, "%02d" % k, None, None)


class FakeWing:
    def __init__(self):
        self.ch = {"9": {"name": "Bass", "src_grp": "CRD", "src_in": 6}, "10": {"name": "Mon-Bass", "src_grp": "CRD", "src_in": 43},
                   "12": {"name": "Keys", "src_grp": "CRD", "src_in": 9}, "13": {"name": "Mon-Keys", "src_grp": "CRD", "src_in": 55},
                   "14": {"name": "M. Keys", "src_grp": "CRD", "src_in": 29}, "45": {"name": "Room (RTA)", "src_grp": "LCL", "src_in": 1}}
        self.inputs = {"CRD/9": {"mode": "ST"}, "CRD/55": {"mode": "ST"}, "CRD/29": {"mode": "ST"}, "CRD/6": {"mode": "M"}}
        self.settings = {"mode": "control"}
        self.sets = []

    def state(self):
        return {"strips": {"ch": {k: dict(v) for k, v in self.ch.items()}, "bus": {"2": {"name": "Bass"}}, "main": {"1": {"name": "FOH"}}},
                "inputs": dict(self.inputs)}

    def outputs(self):
        return {"CRD/61": {"grp": "MAIN", "in": 1}, "CRD/63": {"grp": "BUS", "in": 3}}

    def set_src(self, n, grp, idx):
        self.sets.append((n, grp, idx))


class Patch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.usb = os.path.join(self.tmp, "USB-SSD")
        os.makedirs(os.path.join(self.usb, rec.FOLDER))
        self.e = ae.Engine({"audio_engine": "fake", "engine_port": 0, "engine_channels": 8}, LOG)
        self.e.start()
        self.assertTrue(wait(lambda: self.e.status == "running"))
        self.d, self.w = FakeDante(), FakeWing()
        self.r = rec.Recorder(os.path.join(self.tmp, "data"), {}, LOG, self.e, desk=self.w, dante=self.d,
                              list_drives=lambda: rec.folder_drives([self.usb]))
        self.r.check_drives()

    def tearDown(self):
        self.r.close()
        self.e.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_names_through_the_wing_and_stereo_pairs(self):
        n = self.r.names(fresh=True)
        self.assertEqual((n[1]["name"], n[1]["wing_chs"], n[1]["side"]), ("Bass", [9, 10], ""))  # not "Mon-Bass"
        self.assertEqual((n[2]["name"], n[2]["side"], n[3]["name"], n[3]["side"]), ("Keys", "L", "Keys", "R"))
        self.assertEqual(n[4]["name"], "M. Keys")
        self.assertEqual(n[2]["from"], TIO + " · CH10 Keys L")
        t = self.r.tracks_for(8)
        self.assertEqual([(x["dvs"], x["pair"], x["name"]) for x in t][:3], [(1, None, "Bass"), (2, 3, "Keys"), (4, 5, "M. Keys")])
        self.assertIn("Main 1", t[3]["name"])  # a Wing output: what the Wing sends there
        self.assertIsNone(self.r.patch_mono(2, True, "FOH"))  # keep the Keys as two mono tracks
        self.assertEqual([x["dvs"] for x in self.r.tracks_for(8)][:4], [1, 2, 3, 4])

    def test_stereo_file_and_playback_back_where_it_came_from(self):
        self.assertIsNone(self.r.start("FOH"))
        time.sleep(1.2)
        self.r.stop("FOH")
        folder, name = self.r.last["folder"], self.r.last["name"]
        with wave.open(os.path.join(folder, "02 Keys.wav")) as wv:
            self.assertEqual((wv.getnchannels(), wv.getsampwidth()), (2, 3))  # one stereo file for the pair
            frames = wv.readframes(4800)
        left = [int.from_bytes(frames[i:i + 3], "little", signed=True) for i in range(0, len(frames), 6)]
        right = [int.from_bytes(frames[i:i + 3], "little", signed=True) for i in range(3, len(frames), 6)]
        self.assertNotEqual(left[:200], right[:200])  # DVS 2 and 3 carry different tones
        plan, err = self.r.play_plan(name)
        self.assertIsNone(err)
        got = sorted((s["rx_ch"], s["tx_ch"]) for s in plan["steps"] if s["kind"] == "dante")
        self.assertEqual(got, [("06 - Bass", "01"), ("09 - Keys L", "02"), ("10 - Keys R", "03"), ("29 - M-Keys L", "04"),
                               ("30 - M-Kyes R", "05"), ("43 - SR W1", "01"), ("55", "02"), ("56", "03")])  # the Mon- copies too
        bass = next(s for s in plan["steps"] if s["rx_ch"] == "43 - SR W1")
        self.assertEqual((bass["now"], bass["wing"]), (TIO + " · CH9 Bass", ["10 Mon-Bass"]))
        self.assertTrue(any("Main 1" in x or "61 - FOH L" in x for x in plan["notes"]))  # recorded from a Wing output: not sent
        before = sorted(self.d.r)
        self.w.settings["mode"] = "watch"
        self.assertIn("Watch only", self.r.play(name, 0, "FOH"))
        self.assertEqual(sorted(self.d.r), before)  # nothing changed
        self.w.settings["mode"] = "control"
        self.assertIsNone(self.r.play(name, 0, "FOH"))
        self.assertIn((WING, "06 - Bass", ME, "01"), self.d.r)
        self.assertIn((WING, "56", ME, "03"), self.d.r)
        self.assertFalse(self.r.view()["on_stage"])
        self.assertTrue(wait(lambda: any(b != bytes(len(b)) for b in self.e.backend.written[-5:])))
        self.assertIsNone(self.r.back_to_stage("FOH"))
        self.assertEqual(sorted(self.d.r), before)  # every route back as it was
        self.assertTrue(self.r.view()["on_stage"])

    def test_send_somewhere_else_or_not_at_all(self):
        self.r.start("FOH")
        time.sleep(0.6)
        self.r.stop("FOH")
        name = self.r.last["name"]
        plan, _ = self.r.play_plan(name, send={"1": "off", "2": [{"dev": "Y002-box-2-Yamaha-Tio1608", "ch": "01 Wireless 2"},
                                                                {"dev": "Y002-box-2-Yamaha-Tio1608", "ch": "02 Wireless 3"}]})
        self.assertFalse(any(s["track"] == "Bass" for s in plan["steps"]))
        self.assertEqual(sorted((s["rx_ch"], s["tx_ch"], s["prev_dev"]) for s in plan["steps"] if s["track"] == "Keys"),
                         [("01 Wireless 2", "02", ""), ("02 Wireless 3", "03", "")])
        self.assertIsNone(self.r.play(name, 0, "FOH", send={"1": "off", "2": [{"dev": "Y002-box-2-Yamaha-Tio1608", "ch": "01 Wireless 2"}]}))
        self.assertIn(("Y002-box-2-Yamaha-Tio1608", "01 Wireless 2", ME, "02"), self.d.r)
        self.r.back_to_stage("FOH")
        self.assertFalse(any(x[1] == "01 Wireless 2" for x in self.d.r))  # it had nothing before: cleared again

    def test_patch_routes_and_layouts(self):
        v = self.r.patch_view()
        self.assertEqual((v["device"], v["wing"], v["used"]), (ME, WING, 6))
        self.assertIn({"dev": TIO, "ch": "CH12 A Guitar", "num": 12}, v["sources"])
        self.assertFalse(any(x["dev"] == ME for x in v["sources"]))
        self.assertIsNone(self.r.layout_save("Sunday", "FOH"))
        self.assertIsNone(self.r.patch_route(7, TIO, "CH12 A Guitar", "FOH"))
        self.assertIn((ME, "07", TIO, "CH12 A Guitar"), self.d.r)
        self.assertIsNone(self.r.patch_route(2, MT, "07 - Keys & Piano", "FOH", pair={"dev": MT, "ch": "08 - Keys & Piano"}))
        ch, err = self.r.layout_plan("Sunday")
        self.assertEqual(sorted(c["dvs"] for c in ch), [2, 3, 7])
        self.assertEqual(next(c for c in ch if c["dvs"] == 7)["to"], "")  # Sunday had nothing on 7: cleared
        self.assertIsNone(self.r.layout_apply("Sunday", "FOH"))
        self.assertIn((ME, "02", TIO, "CH10 Keys L"), self.d.r)
        self.assertFalse(any(x[0] == ME and x[1] == "07" for x in self.d.r))
        self.r.start("FOH")
        self.assertIn("Not while recording", self.r.patch_route(7, TIO, "CH12 A Guitar", "FOH"))
        self.r.stop("FOH")
        self.assertIn("isn't there", self.r.layouts.delete("Nope"))


class PatchList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d, self.w = FakeDante(), FakeWing()

        def read():
            st = self.w.state()
            return self.d.routes(), self.d.channels(), ME, WING, st["strips"], st["inputs"], self.w.outputs()
        here = os.path.dirname(os.path.abspath(__file__))
        self.pl = patchlist.PatchList(self.tmp, LOG, read, seed=os.path.join(here, "seed", "patch_sheet.csv"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, data, title):
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        i = next(n for n, r in enumerate(rows) if r and r[0] == title)
        out = []
        for r in rows[i + 2:]:
            if not r or not any(r):
                break
            out.append(r)
        return rows[i + 1], out

    def test_the_sheet_the_inputs_outputs_and_what_changed(self):
        data, text, changes, since = self.pl.report()
        self.assertIsNone(since)
        cols, ins = self.rows(data, "INPUTS")
        self.assertEqual(cols[:6], ["FOH", "Preamp", "Dante", "Input Name", "Mic / DI / Tracks", "Stand"])
        bass = next(r for r in ins if r[3] == "Bass")
        self.assertEqual(bass[:3] + bass[4:5], ["9", "Stage Box 1.9", "6", "DI - LD Systems"])  # from the church's sheet
        self.assertEqual((bass[7], bass[8]), (TIO + " · CH9 Bass", "C3 Life 01"))
        keys = [r for r in ins if r[3].startswith("Keys")]
        self.assertEqual([(r[0], r[2], r[3], r[6]) for r in keys], [("12", "9", "Keys L", "Stereo"), ("", "10", "Keys R", "")])
        cols, outs = self.rows(data, "OUTPUTS (Wing card)")
        self.assertIn(["Card out 63", "Bus 2 L (Bass)", "63 - Sub L", "Y002-box-2-Yamaha-Tio1608 · Sub"], outs)
        _, hp = self.rows(data, "C3 LIFE SERVER (the HP)")
        self.assertEqual(hp[0][:2], ["1", TIO + " · CH9 Bass"])
        self.assertIn("first patch list", text)
        self.d.r = [x for x in self.d.r if x[1] != "43 - SR W1"] + [(WING, "43 - SR W1", TIO, "CH12 A Guitar")]
        _, text, changes, since = self.pl.report()
        self.assertTrue(since)
        self.assertIn({"What": WING + " 43 - SR W1", "Was": TIO + " · CH9 Bass", "Now": TIO + " · CH12 A Guitar"}, changes)
        _, _, changes, _ = self.pl.report()
        self.assertEqual(changes, [])

    def test_loading_a_sheet_and_typing_a_mic(self):
        self.assertIn("doesn't look like a patch sheet", self.pl.load_sheet("a,b,c\n1,2,3\n", "Admin")[0])
        self.assertIsNone(self.pl.set_note(14, "M. Keys", "Tracks", "", "FOH"))
        self.assertEqual(self.pl._note(14, "M. Keys")["mic"], "Tracks")
        self.assertEqual(self.pl._note(12, "Keys")["preamp"], "Stage Box 1.10")  # the sheet says "Keys L": still the same channel
        self.assertEqual(self.pl._note(1, "Lead Vox"), {})  # channel 1 is the Kick Out on the sheet: not this one's mic
        self.assertIn("60", self.pl.set_note(14, "M. Keys", "x" * 61, "", "FOH"))


class Trace(unittest.TestCase):
    def test_a_stereo_input_pairs_only_left_then_right(self):
        names = {1: {"wing_ch": 5, "side": "R"}, 2: {"wing_ch": 5, "side": "L"}, 3: {"wing_ch": 5, "side": "R"}, 4: {"wing_ch": 6, "side": "L"}}
        self.assertEqual(patching.pairs(names), {2: 3})
        self.assertEqual(patching.pairs(names, split={2}), {})


if __name__ == "__main__":
    unittest.main()
