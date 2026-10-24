"""Tests for recording and playback (recorder.py), with the audio engine's made-up tones, a fake Dante, a fake Wing and
a fake ProPresenter. Folders stand in for USB drives. Run: python3 -m unittest -v test_recorder.py"""

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import audio_engine as ae
import recorder as rec

LOG = logging.getLogger("test")


def wait(cond, secs=5):
    t = time.time() + secs
    while time.time() < t:
        if cond():
            return True
        time.sleep(0.05)
    return False


class FakeDante:
    def get(self):
        return {"status": "ok", "channels": {"1": {"tx_device": "WING-ABC", "tx_channel": "01"},
                                             "2": {"tx_device": "WING-ABC", "tx_channel": "02"},
                                             "3": {"tx_device": "Tio1608-D", "tx_channel": "03"}}}

    def tx_channels(self):
        return {"WING-ABC": {1: "01", 2: "02"}}

    def dvs_name(self):
        return "c3life"

    def all_routes(self):  # DVS outs 1 and 2 go to the Wing card's inputs 11 and 12
        return [{"rx_device": "WING-ABC", "rx_channel": "11", "tx_device": "C3LIFE", "tx_channel": "01"},
                {"rx_device": "WING-ABC", "rx_channel": "12", "tx_device": "C3LIFE", "tx_channel": "02"}]


class FakeDesk:
    def __init__(self):
        self.ch = {"3": {"name": "Lead Vox", "src_grp": "LCL", "src_in": 3}, "5": {"name": "Keys", "src_grp": "A", "src_in": 5}}
        self.sets, self.fail = [], None

    def state(self):
        return {"strips": {"ch": {k: dict(v) for k, v in self.ch.items()}}}

    def outputs(self):
        return {"CRD/1": {"grp": "LCL", "in": 3}, "CRD/2": {"grp": "A", "in": 5}}

    def set_src(self, n, grp, idx):
        if self.fail:
            return self.fail
        self.sets.append((n, grp, idx))
        self.ch[str(n)].update(src_grp=grp, src_in=idx)
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.usb = os.path.join(self.tmp, "USB-SSD")
        os.makedirs(self.usb)
        self.roots = [self.usb]
        self.e = ae.Engine({"audio_engine": "fake", "engine_port": 0, "engine_channels": 8}, LOG)
        self.e.start()
        self.assertTrue(wait(lambda: self.e.status == "running"))
        self.desk = FakeDesk()
        self.r = rec.Recorder(os.path.join(self.tmp, "data"), {}, LOG, self.e, desk=self.desk, dante=FakeDante(),
                              list_drives=lambda: rec.folder_drives(self.roots))

    def tearDown(self):
        self.r.close()
        self.e.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Drives(Base):
    def test_only_a_drive_someone_chose_or_already_prepared(self):
        self.r.check_drives()
        self.assertIsNone(self.r.drive)  # a new drive: not written to until someone says so
        self.assertIn("No recording drive", self.r.start("FOH"))
        did = self.r.drives[0]["id"]
        self.assertIsNone(self.r.use_drive(did, True, "FOH"))
        self.assertEqual(self.r.drive["id"], did)
        self.assertTrue(os.path.isdir(os.path.join(self.usb, rec.FOLDER)))
        self.assertEqual(json.loads(read(os.path.join(self.tmp, "data", "recording.json")))["drives"][0]["id"], did)  # remembered
        other = os.path.join(self.tmp, "Prepared")  # a drive that already has the folder: used as it's plugged in
        os.makedirs(os.path.join(other, rec.FOLDER))
        self.roots[:] = [other]
        self.r.check_drives()
        self.assertEqual(self.r.drive["label"], "Prepared")


class Ready(Base):
    """A drive that's already a recording drive, plugged in."""
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.usb, rec.FOLDER))
        self.r.check_drives()


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Recording(Ready):

    def test_names_from_the_wing_files_markers_and_projects(self):
        t = self.r.tracks_for(8)
        self.assertEqual([x["file"] for x in t], ["01 Lead Vox.wav", "02 Keys.wav", "03 Tio1608-D 03.wav"])
        self.assertEqual([x["wing_ch"] for x in t], [3, 5, None])
        self.assertIsNone(self.r.start("FOH"))
        self.assertIn("already", self.r.start("FOH"))
        for slide in ({"presentation": "Way Maker", "index": 0}, {"presentation": "Way Maker", "index": 1},
                      {"presentation": "Goodness of God", "index": 0}, {"presentation": "Message", "index": 0},
                      {"presentation": "Message", "index": 4}):
            self.r._on_slide(slide)
            time.sleep(0.25)
        self.assertIsNone(self.r.marker("Altar call", "FOH"))
        time.sleep(0.3)
        self.assertIsNone(self.r.stop("FOH"))
        last = self.r.last
        self.assertEqual([m["name"] for m in last["markers"]], ["Way Maker", "Goodness of God", "Message", "Altar call"])
        self.assertEqual((last["tracks"], last["error"]), (3, ""))
        folder = last["folder"]
        with wave.open(os.path.join(folder, "01 Lead Vox.wav")) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 3, 48000))
            n = w.getnframes()
            self.assertGreater(n, 48000)  # over a second
            raw = w.readframes(4800)
        vals = [int.from_bytes(raw[i:i + 3], "little", signed=True) for i in range(0, len(raw), 3)]
        self.assertAlmostEqual(max(vals) / 2 ** 23, 0.1, delta=0.005)  # channel 1's tone, -20 dBFS
        with wave.open(os.path.join(folder, "02 Keys.wav")) as w:
            self.assertEqual(w.getnframes(), n)  # every track the same length
        csv = read(os.path.join(folder, "markers.csv"))
        self.assertIn('"Goodness of God"', csv)
        proj = read(os.path.join(folder, last["name"] + ".RPP"))
        self.assertIn('NAME "Lead Vox"', proj)
        self.assertIn('FILE "01 Lead Vox.wav"', proj)
        self.assertIn('"Message"', proj)
        s = json.loads(read(os.path.join(folder, "session.json")))
        self.assertEqual(s["tracks"][0]["wing_ch"], 3)
        self.assertEqual(self.r.sessions()[0]["name"], last["name"])

    def test_by_itself_on_the_start_and_end_slides(self):
        self.assertIn("which slide", self.r.save_settings({"auto": "slide"}, "FOH"))
        self.assertIsNone(self.r.save_settings({"auto": "slide", "start": {"kind": "presentation", "text": "Welcome"},
                                                "stop": {"kind": "label", "text": "Benediction"}}, "FOH"))
        self.r._on_slide({"presentation": "Announcements", "label": ""})
        self.assertIsNone(self.r.rec)  # not the start slide
        self.r._on_slide({"presentation": "welcome", "label": ""})
        self.assertIsNotNone(self.r.rec)
        self.assertIn("start slide", self.r.rec["by"])
        time.sleep(0.3)
        self.r._on_slide({"presentation": "Closing", "label": "Benediction"})
        self.assertIsNone(self.r.rec)
        self.assertIn("end slide", self.r.last["stopped_by"])
        self.assertEqual([m["name"] for m in self.r.last["markers"]], ["welcome"])

    def test_as_soon_as_the_drive_is_plugged_in_and_unplugging_stops_it(self):
        self.assertIsNone(self.r.save_settings({"auto": "plug"}, "FOH"))
        self.roots[:] = []
        self.r.check_drives()
        self.assertIsNone(self.r.drive)
        self.roots[:] = [self.usb]
        self.r.check_drives()
        self.assertIsNotNone(self.r.rec)
        time.sleep(0.3)
        self.roots[:] = []
        self.r.check_drives()
        self.assertIsNone(self.r.rec)
        self.assertIn("unplugged", self.r.last["error"])

    def test_settings_are_checked(self):
        self.assertIn("0.5 to 12", self.r.save_settings({"max_hours": 99}, "FOH"))
        self.assertIn("Channels", self.r.save_settings({"channels": [0]}, "FOH"))
        self.assertIsNone(self.r.save_settings({"channels": [2, 1]}, "FOH"))
        self.assertEqual([x["dvs"] for x in self.r.tracks_for(8)], [2, 1])


class Playback(Ready):
    def test_asks_switches_the_wing_plays_and_back_to_stage(self):
        self.r.start("FOH")
        self.r._on_slide({"presentation": "Song A"})
        time.sleep(0.6)
        self.r._on_slide({"presentation": "Song B"})
        time.sleep(0.6)
        self.r.stop("FOH")
        name = self.r.last["name"]
        plan, err = self.r.play_plan(name)
        self.assertIsNone(err)
        self.assertEqual([(s["wing_ch"], s["now"], s["to"]) for s in plan["steps"]], [(3, "LCL/3", "CRD/11"), (5, "A/5", "CRD/12")])
        self.assertIn("isn't from a Wing channel", " ".join(plan["notes"]))  # the Tio track
        self.r.start("FOH")
        self.assertIn("Not while recording", self.r.play(name, 0, "FOH"))
        self.r.stop("FOH")
        song_b = next(m["t"] for m in plan["markers"] if m["name"] == "Song B")
        self.assertIsNone(self.r.play(name, song_b, "FOH"))
        self.assertEqual(self.desk.sets, [(3, "CRD", 11), (5, "CRD", 12)])
        self.assertIn("Not while playing", self.r.start("FOH"))
        # DVS out 1 carries the Lead Vox track (channel 1's tone); out 3 (not played) stays silent
        self.assertTrue(wait(lambda: any(b != bytes(len(b)) for b in self.e.backend.written[-5:])))
        blk = next(b for b in reversed(self.e.backend.written) if b != bytes(len(b)))
        o1 = rec.split24(blk, 0, 8)
        o3 = rec.split24(blk, 2, 8)
        self.assertGreater(max(abs(int.from_bytes(o1[i:i + 3], "little", signed=True)) for i in range(0, len(o1), 3)), 2 ** 23 * 0.09)
        self.assertEqual(o3, bytearray(len(o3)))
        self.assertGreater(self.r.view()["playing"]["secs"], song_b - 0.1)
        self.assertIsNone(self.r.back_to_stage("FOH"))
        self.assertEqual(self.desk.sets[-2:], [(3, "LCL", 3), (5, "A", 5)])  # every input back as it was
        self.assertTrue(self.r.view()["on_stage"])

    def test_watch_only_changes_nothing(self):
        self.r.start("FOH")
        time.sleep(0.3)
        self.r.stop("FOH")
        self.desk.fail = "The Wing is in Watch only."
        self.assertEqual(self.r.play(self.r.last["name"], 0, "FOH"), "The Wing is in Watch only.")
        self.assertIsNone(self.r.player)
        self.assertEqual(self.desk.sets, [])


class FakePP(BaseHTTPRequestHandler):
    slide = {"uuid": "P1", "name": "Way Maker", "index": 2}

    def do_GET(self):
        s = FakePP.slide
        if self.path == "/v1/presentation/slide_index":
            body = {"presentation_index": {"index": s["index"], "presentation_id": {"uuid": s["uuid"], "name": s["name"]}}}
        elif self.path == "/v1/presentation/P1":
            body = {"presentation": {"id": {"name": "Way Maker"}, "groups": [{"name": "Verse 1", "slides": [{"label": ""}, {"label": ""}]},
                                                                            {"name": "Chorus", "slides": [{"label": "Big"}]}]}}
        elif self.path == "/v1/playlist/active":
            body = {"presentation": {"playlist": {"name": "Sunday"}, "item": {"name": "Song 1: Way Maker"}}}
        else:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass


class Slides(unittest.TestCase):
    def test_which_slide_is_up(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePP)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        got = []
        w = rec.SlideWatch({"propresenter": "http://127.0.0.1:%d" % srv.server_address[1]}, got.append)
        w.poll()
        w.poll()  # the same slide: nothing new
        self.assertEqual(got, [{"presentation": "Way Maker", "group": "Chorus", "label": "Big", "item": "Song 1: Way Maker", "index": 2}])
        w2 = rec.SlideWatch({}, got.append)
        w2.poll()
        self.assertIn("hasn't been found", w2.error)
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
