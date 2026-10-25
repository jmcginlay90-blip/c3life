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
        self.r.save_settings({"local": False}, "FOH")
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


class OnThisComputer(Base):
    def test_making_room_deletes_only_what_is_safely_on_the_drive(self):
        self.r.save_settings({"free_local": False}, "FOH")  # keep a copy here too
        self.r.check_drives()
        self.assertIsNone(self.r.start("FOH"))
        time.sleep(0.3)
        self.r.stop("FOH")
        name = self.r.last["name"]
        base = os.path.join(self.tmp, "recordings", rec.FOLDER)
        os.makedirs(os.path.join(self.usb, rec.FOLDER))  # the recording drive is plugged in: copied, and kept here
        self.r.check_drives()
        self.assertTrue(wait(lambda: self.r.job and self.r.job["finished"]))
        c = json.loads(read(os.path.join(base, name, rec.COPIED)))
        self.assertEqual((c["label"], "01 Lead Vox.wav" in c["files"]), ("USB-SSD", True))
        self.assertFalse(os.path.exists(os.path.join(self.usb, rec.FOLDER, name, rec.COPIED)))  # the note stays here
        # two older ones: one copied (the same note), one only here
        for old, copied in (("2026-01-04 1000 Sunday", True), ("2026-01-11 1000 Sunday", False)):
            os.makedirs(os.path.join(base, old))
            with open(os.path.join(base, old, "01 Lead Vox.wav"), "wb") as f:
                f.write(b"x" * 300)
            if copied:
                with open(os.path.join(base, old, rec.COPIED), "w") as f:
                    json.dump({"label": "USB-SSD", "at": "2026-01-04 12:00", "files": {"01 Lead Vox.wav": 300}}, f)
        sizes = lambda: sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(base) for f in fs)
        loc = self.r._local_drive()
        self.r._local_drive = lambda: dict(loc, free=10 ** 7 - sizes())  # a small disk, so the test can fill it
        need = 10 ** 7 - sizes() + 200  # deleting the oldest copied one (300 bytes) is enough
        gone, short = self.r.make_room(need)
        self.assertEqual((gone, short), (["2026-01-04 1000 Sunday"], False))
        gone, short = self.r.make_room(10 ** 7)  # far too little room: everything copied goes, never the one only here
        self.assertEqual((gone, short), ([name], True))
        self.assertTrue(os.path.isdir(os.path.join(base, "2026-01-11 1000 Sunday")))
        self.assertTrue(os.path.isfile(os.path.join(self.usb, rec.FOLDER, name, "01 Lead Vox.wav")))  # the drive's copy is untouched
        self.assertTrue(self.r.view()["short"])
        self.r.save_settings({"make_room": False}, "FOH")
        self.assertEqual(self.r.make_room(10 ** 7, delete=False), ([], True))  # off: it only warns

    def test_records_here_with_no_drive_then_copies_when_one_is_plugged_in(self):
        self.r.check_drives()
        self.assertIsNone(self.r.drive)
        self.assertIsNone(self.r.start("FOH"))  # no drive: onto this computer
        self.assertEqual(self.r.rec["drive"], "local")
        self.r.check_drives()
        self.assertIsNotNone(self.r.rec)  # not "unplugged"
        time.sleep(0.4)
        self.r.stop("FOH")
        name = self.r.last["name"]
        here = os.path.join(self.tmp, "recordings", rec.FOLDER, name)
        self.assertTrue(os.path.isfile(os.path.join(here, "01 Lead Vox.wav")))
        self.assertEqual([(x["name"], x["where"]) for x in self.r.sessions()], [(name, "local")])
        size = os.path.getsize(os.path.join(here, "01 Lead Vox.wav"))
        os.makedirs(os.path.join(self.usb, rec.FOLDER))  # the recording drive is plugged in: copied by itself
        self.r.check_drives()
        self.assertTrue(wait(lambda: self.r.job and self.r.job["finished"]))
        self.assertEqual(self.r.job["error"], "")
        there = os.path.join(self.usb, rec.FOLDER, name)
        self.assertEqual(os.path.getsize(os.path.join(there, "01 Lead Vox.wav")), size)
        self.assertTrue(os.path.isfile(os.path.join(there, name + ".RPP")))
        self.assertFalse(os.path.exists(here))  # checked, then cleared from this computer
        self.assertEqual([(x["name"], x["where"]) for x in self.r.sessions()], [(name, "drive")])
        # another drive (to take home): a copy with the button; the recording drive keeps its own
        home = os.path.join(self.tmp, "Take home")
        os.makedirs(home)
        self.roots.append(home)
        self.r.check_drives()
        other = next(d["id"] for d in self.r.drives if d["label"] == "Take home")
        self.assertIsNone(self.r.copy([(name, "drive")], other, "FOH"))
        self.assertTrue(wait(lambda: self.r.job["finished"]))
        self.assertTrue(os.path.isfile(os.path.join(home, rec.COPIES, name, "01 Lead Vox.wav")))
        self.r.check_drives()  # the take-home drive didn't become a recording drive by being copied to
        self.assertEqual(([d["ok"] for d in self.r.drives if d["label"] == "Take home"], self.r.drive["label"]), ([False], "USB-SSD"))
        self.assertTrue(os.path.isfile(os.path.join(there, "01 Lead Vox.wav")))
        self.assertIn("isn't plugged in", self.r.copy([(name, "drive")], "nope", "FOH"))


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

    def test_song_sections_from_slide_groups(self):
        self.assertIsNone(self.r.start("FOH"))
        for slide in ({"presentation": "Way Maker", "group": "Verse 1"}, {"presentation": "Way Maker", "group": "Verse 1"},
                      {"presentation": "Way Maker", "group": "Chorus"}, {"presentation": "Way Maker", "group": "Verse 1"},
                      {"presentation": "Goodness of God", "group": "Verse 1"}, {"presentation": "Message", "group": ""}):
            self.r._on_slide(slide)
            time.sleep(0.25)
        self.assertIsNone(self.r.marker("Altar call", "FOH"))
        time.sleep(0.25)
        self.r.stop("FOH")
        ms = self.r.last["markers"]
        self.assertEqual([(m["name"], rec.kind(m)) for m in ms],
                         [("Way Maker", "song"), ("Verse 1", "section"), ("Chorus", "section"), ("Verse 1", "section"),
                          ("Goodness of God", "song"), ("Verse 1", "section"), ("Message", "song"), ("Altar call", "")])
        self.assertEqual(ms[2]["song"], "Way Maker")
        p = {m["name"] + str(i): m for i, m in enumerate(self.r.sessions()[0]["markers"])}
        self.assertEqual(p["Chorus2"]["end"], ms[3]["t"])          # a section ends at the next section
        self.assertEqual(p["Verse 13"]["end"], ms[4]["t"])         # ...or the next song
        self.assertEqual(p["Way Maker0"]["end"], ms[4]["t"])       # a song ends at the next song
        self.assertNotIn("end", p["Altar call7"])                  # one added by hand is only somewhere to play from
        folder = self.r.last["folder"]
        proj = read(os.path.join(folder, self.r.last["name"] + ".RPP"))
        self.assertIn('MARKER 4 %.3f "Chorus" 1' % ms[2]["t"], proj)   # a REAPER region: its start...
        self.assertIn('MARKER 4 %.3f "" 1' % ms[3]["t"], proj)         # ...and its end
        self.assertIn('"Way Maker" 0', proj)
        labels = read(os.path.join(folder, "markers.txt"))
        self.assertIn("%.3f\t%.3f\tWay Maker: Chorus" % (ms[2]["t"], ms[3]["t"]), labels)  # an Audacity region
        self.assertIn(',section,"Way Maker"', read(os.path.join(folder, "markers.csv")))
        self.assertIsNone(self.r.save_settings({"sections": False}, "FOH"))
        self.r.start("FOH")
        self.r._on_slide({"presentation": "Way Maker", "group": "Verse 1"})
        self.r._on_slide({"presentation": "Way Maker", "group": "Chorus"})
        time.sleep(0.2)
        self.r.stop("FOH")
        self.assertEqual([m["name"] for m in self.r.last["markers"]], ["Way Maker"])

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
        self.assertIn("nothing on the Wing hears where it was recorded from", " ".join(plan["notes"]))  # the Tio track: no Wing input takes it
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

    def test_one_section_and_looping_it(self):
        self.r.start("FOH")
        self.r._on_slide({"presentation": "Song A", "group": "Verse"})
        time.sleep(0.4)
        self.r._on_slide({"presentation": "Song A", "group": "Chorus"})
        time.sleep(0.6)
        self.r._on_slide({"presentation": "Song A", "group": "Bridge"})
        time.sleep(0.4)
        self.r.stop("FOH")
        name = self.r.last["name"]
        ch = next(m for m in self.r.sessions()[0]["markers"] if m["name"] == "Chorus")
        self.assertIn("Choose a song", self.r.play(name, ch["t"], "FOH", loop=True))
        self.assertIn("no length", self.r.play(name, ch["t"], "FOH", end_at=ch["t"]))
        self.assertIsNone(self.r.play(name, ch["t"], "FOH", end_at=ch["end"], loop=True, part="Song A: Chorus"))
        time.sleep((ch["end"] - ch["t"]) * 2 + 0.3)  # round it more than once
        v = self.r.view()["playing"]
        self.assertEqual((v["from"], v["to"], v["loop"], v["part"], v["ended"]), (ch["t"], ch["end"], True, "Song A: Chorus", False))
        self.assertTrue(ch["t"] - 0.05 <= v["secs"] <= ch["end"] + 0.05, v)
        self.assertIsNone(self.r.back_to_stage("FOH"))
        self.assertIsNone(self.r.play(name, ch["t"], "FOH", end_at=ch["end"]))  # just once: it stops at the end of the chorus
        self.assertTrue(wait(lambda: self.r.view()["playing"]["ended"], 4))
        self.assertLessEqual(self.r.view()["playing"]["secs"], ch["end"] + 0.05)
        self.r.back_to_stage("FOH")

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


class Api(unittest.TestCase):
    """Through the C3 Life server: who may, recording to the drive, and playing back to the (fake) Wing."""

    def setUp(self):
        import urllib.request  # noqa
        import c3life_server as s
        from test_wing import FakeWing
        self.s = s
        self.fw = FakeWing()
        self.fw.vals.update({"/ch/3/name": "Lead Vox", "/io/out/CRD/1/grp": "LCL", "/io/out/CRD/1/in": 3})
        self.tmp = tempfile.mkdtemp()
        usb = os.path.join(self.tmp, "USB")
        os.makedirs(os.path.join(usb, rec.FOLDER))
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="",
                   wing_ip="127.0.0.1", wing_osc_port=self.fw.osc_port, wing_native_port=self.fw.native_port,
                   audio_engine="fake", engine_port=0, engine_channels=8, record_drive_roots=[usb])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.httpd.recorder.dante = FakeDante()
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "ProPresenter"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            self.tok[n] = self.call("/api/login", "POST", body)[1]["token"]

    def tearDown(self):
        self.httpd.tio.close(); self.httpd.wing.close()
        self.httpd.shutdown(); self.httpd.server_close()
        self.fw.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_wing_patch_through_the_server(self):
        self.assertTrue(wait(lambda: self.httpd.wing.status()["status"] == "connected"))
        self.assertTrue(wait(lambda: self.httpd.wing.state()["strips"]["ch"]["3"].get("src_grp") == "LCL"))
        self.call("/api/wing/patch", who="FOH sound")
        self.assertTrue(wait(lambda: "CRD/1" in self.call("/api/wing/patch", who="FOH sound")[1]["outs"]))
        code, j = self.call("/api/wing/patch", who="FOH sound")
        self.assertEqual((j["outs"]["CRD/1"], j["used"].get("LCL/3"), j["count"]["CRD"]), ({"grp": "LCL", "in": 3}, "Lead Vox", 64))
        self.assertIn("Watch only", self.call("/api/wing/patch", "POST", {"out": "CRD/1", "grp": "A", "idx": 5}, who="FOH sound")[1]["error"])
        self.assertEqual(self.call("/api/wing/patch", "POST", {"out": "CRD/1", "grp": "A", "idx": 5}, who="ProPresenter")[0], 403)
        self.httpd.wing.configure(mode="control")
        self.assertEqual(self.call("/api/wing/patch", "POST", {"out": "CRD/1", "grp": "A", "idx": 5}, who="FOH sound")[0], 200)
        self.assertTrue(wait(lambda: self.fw.vals.get("/io/out/CRD/1/grp") == "A" and self.fw.vals.get("/io/out/CRD/1/in") == 5))

    def test_record_and_play_back_through_the_server(self):
        self.assertTrue(wait(lambda: self.call("/api/recording", who="FOH sound")[1].get("drive")))
        code, j = self.call("/api/recording", who="ProPresenter")
        self.assertEqual((code, j["can"], j["drive"]["label"]), (200, False, "USB"))
        self.assertEqual(self.call("/api/recording/start", "POST", {}, who="ProPresenter")[0], 403)
        self.assertTrue(wait(lambda: self.httpd.wing.status()["status"] == "connected"))
        self.assertTrue(wait(lambda: (self.httpd.wing.state()["strips"]["ch"]["3"].get("src_grp") == "LCL")))
        self.httpd.wing.outputs()
        self.assertTrue(wait(lambda: "CRD/1" in self.httpd.wing.outputs()))
        self.httpd.recorder.names_cache = (0, {})
        code, j = self.call("/api/recording/start", "POST", {}, who="FOH sound")
        self.assertEqual(code, 200, j)
        self.assertEqual(j["tracks"][0]["name"], "Lead Vox")
        self.assertTrue(self.call("/api/rta/live", who="FOH sound")[1]["rec"]["on"])
        self.assertEqual(self.call("/api/recording/marker", "POST", {"name": "Song 1"}, who="FOH sound")[0], 200)
        time.sleep(0.5)
        code, j = self.call("/api/recording/stop", "POST", {}, who="FOH sound")
        self.assertEqual((code, j["last"]["markers"][0]["name"]), (200, "Song 1"))
        name = j["sessions"][0]["name"]
        code, j = self.call("/api/recording/plan", "POST", {"name": name}, who="FOH sound")
        self.assertEqual([(x["wing_ch"], x["to"]) for x in j["plan"]["steps"]], [(3, "CRD/11")])
        code, j = self.call("/api/recording/play", "POST", {"name": name}, who="FOH sound")
        self.assertEqual(code, 409); self.assertIn("Watch only", j["error"])  # nothing changes on the desk in Watch only
        self.httpd.wing.configure(mode="control")
        self.assertEqual(self.call("/api/recording/play", "POST", {"name": name, "loop": "yes"}, who="FOH sound")[1]["error"], "Bad request")
        self.assertIn("Choose a song", self.call("/api/recording/play", "POST", {"name": name, "loop": True}, who="FOH sound")[1]["error"])
        code, j = self.call("/api/recording/play", "POST", {"name": name, "from": 0, "to": 0.5, "loop": True, "part": "Start"}, who="FOH sound")
        self.assertEqual((code, j["playing"]["loop"], j["playing"]["to"]), (200, True, 0.5), j)
        self.call("/api/recording/stage", "POST", {}, who="FOH sound")
        code, j = self.call("/api/recording/play", "POST", {"name": name}, who="FOH sound")
        self.assertEqual(code, 200, j)
        self.assertFalse(j["on_stage"])
        self.assertTrue(wait(lambda: ("/ch/3/in/conn/grp", "CRD") in self.fw.sets))
        self.assertIn(("/ch/3/in/conn/in", 11), self.fw.sets)
        code, j = self.call("/api/recording/stage", "POST", {}, who="FOH sound")
        self.assertEqual((code, j["on_stage"]), (200, True))
        self.assertTrue(wait(lambda: self.fw.sets[-2:] == [("/ch/3/in/conn/grp", "LCL"), ("/ch/3/in/conn/in", 3)] or
                             ("/ch/3/in/conn/grp", "LCL") in self.fw.sets))
        self.assertEqual(self.call("/api/recording/settings", "PUT", {"auto": "plug"}, who="FOH sound")[1]["settings"]["auto"], "plug")
