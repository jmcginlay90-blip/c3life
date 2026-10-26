"""Tests for Listen. Run: python3 -m unittest -v test_listen.py

Needs ffmpeg. Instead of Dante Virtual Soundcard, a generated 16-channel test signal is used, so the
whole path runs for real: channels picked, MP3 made, sent to listeners.
"""

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
import listen as L

# 16 channels, each a different tone, like 16 Dante channels arriving at DVS
FAKE_DVS = ["-re", "-f", "lavfi", "-i", "aevalsrc=" + "|".join("0.2*sin(%d*2*PI*t)" % (220 + 55 * i) for i in range(16)) + ":s=48000:c=hexadecagonal"]


class ListenTest(unittest.TestCase):
    remote = False
    open_group = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        for n in ("player.html", "project/Listen.dc.html", "project/Main.dc.html"):
            with open(os.path.join(app, n), "w") as fh:
                fh.write("<x-dc>%s</x-dc>" % n)
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), listen_input=FAKE_DVS, listen_kbps=64)
        if self.remote:  # pretend this PC is on the home network: only listening is allowed
            cfg["_nets"], cfg["_remote"] = [], [s.ipaddress.ip_network("127.0.0.0/8")]
        else:
            cfg["_nets"], cfg["_remote"] = [s.ipaddress.ip_network("127.0.0.0/8")], []
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        if not self.open_group:  # most tests check per-person rules, without the group open to everyone
            c = self.httpd.listen.config()
            for g in c["groups"]:
                g["open"] = False
            self.httpd.listen.save(c)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        for src in list(self.httpd.listen.sources.values()) + list(self.httpd.listen.captioners.values()):
            src.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw or b"{}") if "json" in (r.headers.get("Content-Type") or "") else {"raw": raw})
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {}

    def login(self, name):
        if name in ("Viewer", "Listener"):
            return self.call("/api/login", "POST", {"name": name, "pin": ""})[1]["token"]
        if name == "Admin":
            return self.call("/api/login", "POST", {"name": name, "password": "c3life-admin"})[1]["token"]
        return self.call("/api/login", "POST", {"name": name, "pin": "" if name == "Viewer" else "1234"})[1]["token"]

    def listen_bytes(self, sid, tok, n=12000, timeout=8):
        r = urllib.request.urlopen("http://127.0.0.1:%d/listen/%s.mp3?t=%s" % (self.port, sid, tok), timeout=timeout)
        self.assertEqual(r.headers["Content-Type"], "audio/mpeg")
        got = b""
        t0 = time.time()
        while len(got) < n and time.time() - t0 < timeout:
            got += r.read(1024)
        return r, got


class OnSite(ListenTest):
    def test_admin_hears_every_source(self):
        a = self.login("Admin")
        mine = self.call("/api/listen/mine", token=a)[1]
        self.assertEqual([x["id"] for x in mine["sources"]], ["main", "stream", "foyer", "speech"])

    def test_people_hear_only_their_groups(self):
        a = self.login("Admin")
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=self.login("Foyer team"))[1]["sources"]], ["foyer", "main"])
        self.assertEqual(self.call("/api/listen/mine", token=self.login("Lighting"))[1]["sources"], [])
        c = self.call("/api/listen/config", token=a)[1]
        c["groups"][0]["members"].append("Lighting")  # Creche team
        self.assertEqual(self.call("/api/listen/config", "PUT", c, token=a)[0], 200)
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=self.login("Lighting"))[1]["sources"]], ["main"])

    def test_real_mp3_audio_comes_through(self):
        r, data = self.listen_bytes("main", self.login("Admin"))
        r.close()
        self.assertGreaterEqual(len(data), 12000)
        self.assertTrue(data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0), data[:4])

    def test_two_listeners_share_one_stream(self):
        a = self.login("Admin")
        r1, d1 = self.listen_bytes("main", a, n=4000)
        r2, d2 = self.listen_bytes("main", a, n=4000)
        src = self.httpd.listen.sources["main"]
        self.assertEqual(len(src.listeners), 2)
        self.assertTrue(src.live)
        r1.close(); r2.close()

    def test_not_allowed_and_not_signed_in(self):
        f = self.login("Foyer team")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/speech.mp3?t=%s" % (self.port, f), timeout=5)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/main.mp3?t=nonsense" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 401)

    def test_only_admin_changes_the_setup(self):
        f = self.login("FOH sound")
        self.assertEqual(self.call("/api/listen/config", token=f)[0], 403)
        self.assertEqual(self.call("/api/listen/config", "PUT", L.DEFAULT_LISTEN, token=f)[0], 403)

    def test_bad_setup_refused(self):
        a = self.login("Admin")
        c = json.loads(json.dumps(L.DEFAULT_LISTEN))
        c["sources"][0]["ch"] = [0]
        self.assertEqual(self.call("/api/listen/config", "PUT", c, token=a)[0], 400)
        c = json.loads(json.dumps(L.DEFAULT_LISTEN))
        c["groups"][0]["sources"] = ["nope"]
        self.assertEqual(self.call("/api/listen/config", "PUT", c, token=a)[0], 400)
        c = json.loads(json.dumps(L.DEFAULT_LISTEN))
        c["sources"][0]["id"] = "../x"
        self.assertEqual(self.call("/api/listen/config", "PUT", c, token=a)[0], 400)

    def test_room_player(self):
        a = self.login("Admin")
        code, j = self.call("/api/listen/pair", "POST", {"room": "creche"}, token=a)
        self.assertEqual(code, 200)
        room = j["token"]
        mine = self.call("/api/listen/mine", token=room)[1]
        self.assertEqual((mine["room"]["name"], [x["id"] for x in mine["sources"]]), ("Crèche", ["main"]))
        r, data = self.listen_bytes("main", room, n=4000)
        r.close()
        self.assertGreater(len(data), 3000)
        self.assertEqual(self.call("/api/mixes", token=room)[0], 401)  # a room player can't touch anyone's mixes
        # the Admin changes what the creche hears
        c = self.call("/api/listen/config", token=a)[1]
        c["rooms"][0]["source"] = "stream"
        self.call("/api/listen/config", "PUT", c, token=a)
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=room)[1]["sources"]], ["stream"])
        self.call("/api/listen/unpair", "POST", {"room": "creche"}, token=a)
        self.assertEqual(self.call("/api/listen/mine", token=room)[0], 401)

    def test_changing_a_room_doesnt_cut_other_listeners(self):
        a = self.login("Admin")
        r, _ = self.listen_bytes("main", a, n=3000)
        c = self.call("/api/listen/config", token=a)[1]
        c["rooms"][0]["source"] = "stream"
        self.call("/api/listen/config", "PUT", c, token=a)
        more = r.read(4000)
        r.close()
        self.assertEqual(len(more), 4000)  # still flowing
        self.assertTrue(self.httpd.listen.live("main"))

    def test_changing_a_sources_channels_restarts_only_that_one(self):
        a = self.login("Admin")
        r1, _ = self.listen_bytes("main", a, n=2000)
        r2, _ = self.listen_bytes("foyer", a, n=2000)
        c = self.call("/api/listen/config", token=a)[1]
        c["sources"][2]["ch"] = [10]  # foyer moves to DVS 10
        self.call("/api/listen/config", "PUT", c, token=a)
        self.assertEqual(len(r1.read(4000)), 4000)  # main untouched
        self.assertNotIn("foyer", self.httpd.listen.sources)
        r1.close(); r2.close()

    def test_stream_stops_when_nobody_listens(self):
        L.IDLE_STOP_S = 1
        try:
            r, _ = self.listen_bytes("foyer", self.login("Admin"), n=2000)
            r.close()
            time.sleep(7.5)
            self.assertFalse(self.httpd.listen.live("foyer"))
        finally:
            L.IDLE_STOP_S = 30


class FromHome(ListenTest):
    remote = True

    def test_only_listen_is_reachable(self):
        self.assertEqual(self.call("/project/Listen.dc.html")[0], 200)
        self.assertEqual(self.call("/player.html")[0], 200)
        self.assertEqual(self.call("/project/Main.dc.html")[0], 403)
        self.assertEqual(self.call("/api/mixes")[0], 403)
        self.assertEqual(self.call("/api/listen/config")[0], 403)

    def test_only_home_groups_from_home(self):
        a = self.login("Admin")
        c = self.httpd.listen.config()
        c["groups"][3]["members"] = ["Viewer"]  # "Listen from home" group
        c["groups"][2]["members"] = ["Viewer", "Foyer team"]  # Welcome team (not home)
        self.httpd.listen.save(c)
        v = self.login("Viewer")
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=v)[1]["sources"]], ["stream"])
        self.assertEqual(self.call("/api/listen/mine", token=self.login("Foyer team"))[1]["sources"], [])
        self.assertEqual(self.call("/api/listen/mine", token=a)[1]["sources"], [])  # even an Admin only hears home groups from home
        r, data = self.listen_bytes("stream", v, n=3000)
        r.close()
        self.assertGreater(len(data), 2000)


class FakeEngine:
    """Says a line every 2 seconds of audio, like a preacher who never stops."""
    rate = 16000

    def __init__(self):
        self.n = 0

    def feed(self, data):
        self.n += len(data)
        if self.n >= 2 * 16000 * 2:
            self.n = 0
            return "the lord is my shepherd i shall not want", ""
        return "", "the lord is"


class Subtitles(ListenTest):
    def setUp(self):
        super().setUp()
        self.httpd.listen.cfg["captions_engine"] = FakeEngine

    def test_subtitles_arrive_and_follow_on(self):
        a = self.login("Admin")
        code, j = self.call("/api/listen/captions/speech?since=0", token=a)
        self.assertEqual(code, 200)
        lines = []
        t0 = time.time()
        while not lines and time.time() - t0 < 10:
            time.sleep(0.5)
            lines = self.call("/api/listen/captions/speech?since=0", token=a)[1]["lines"]
        self.assertEqual(lines[0]["text"], "The lord is my shepherd I shall not want.")
        later = self.call("/api/listen/captions/speech?since=%d" % lines[-1]["id"], token=a)[1]
        self.assertTrue(all(x["id"] > lines[-1]["id"] for x in later["lines"]))

    def test_subtitles_need_permission_and_the_switch_on(self):
        self.assertEqual(self.call("/api/listen/captions/speech?since=0", token=self.login("Foyer team"))[0], 403)
        self.assertEqual(self.call("/api/listen/captions/main?since=0", token=self.login("Admin"))[0], 404)  # off for main
        self.assertEqual(self.call("/api/listen/captions/speech?since=0&t=nonsense")[0], 401)

    def test_room_player_gets_subtitles_by_link_token(self):
        a = self.login("Admin")
        room = self.call("/api/listen/pair", "POST", {"room": "kids"}, token=a)[1]["token"]
        self.assertEqual(self.call("/api/listen/captions/speech?since=0&t=" + room)[0], 200)

    def test_no_model_says_so_kindly(self):
        self.httpd.listen.cfg["captions_engine"] = "vosk"
        self.httpd.listen.cfg["captions_model"] = os.path.join(self.tmp, "missing")
        j = self.call("/api/listen/captions/speech?since=0", token=self.login("Admin"))[1]
        self.assertEqual(j["status"], "unavailable")
        self.assertIn("aren't set up", j["reason"])


@unittest.skipUnless(os.environ.get("C3_VOSK_MODEL"), "set C3_VOSK_MODEL and C3_SPEECH to test real speech-to-text")
class RealSpeech(ListenTest):
    def test_vosk_hears_the_psalm(self):
        self.httpd.listen.cfg.update(listen_input=["-re", "-i", os.environ["C3_SPEECH"]], captions_model=os.environ["C3_VOSK_MODEL"])
        c = self.httpd.listen.config()
        c["sources"][3]["ch"] = [1]
        self.httpd.listen.save(c)
        a = self.login("Admin")
        text, t0 = "", time.time()
        while time.time() - t0 < 25 and "shepherd" not in text:
            time.sleep(1)
            j = self.call("/api/listen/captions/speech?since=0", token=a)[1]
            text = " ".join(x["text"] for x in j["lines"]) + " " + j["partial"]
        print("\n  heard:", text.strip())
        self.assertIn("shepherd", text.lower())


class FakeTranslate:
    def __init__(self, lang):
        self.lang = lang

    def translate(self, text):
        return "[%s] %s" % (self.lang, text)


class Translation(ListenTest):
    def setUp(self):
        super().setUp()
        self.httpd.listen.cfg.update(captions_engine=FakeEngine, translate_engine=FakeTranslate, translate_langs=[("es", "Spanish"), ("fa", "Persian")])

    def wait_lines(self, path, tok, n=1, timeout=12):
        t0, j = time.time(), {}
        while time.time() - t0 < timeout:
            j = self.call(path, token=tok)[1]
            if len(j.get("lines", [])) >= n:
                break
            time.sleep(0.5)
        return j

    def test_listeners_see_the_installed_languages(self):
        mine = self.call("/api/listen/mine", token=self.login("Admin"))[1]
        self.assertEqual([(x["code"], x["native"]) for x in mine["languages"]], [("fa", "فارسی"), ("es", "Español")])

    def test_subtitles_in_spanish(self):
        a = self.login("Admin")
        j = self.wait_lines("/api/listen/captions/speech?since=0&lang=es", a)
        self.assertEqual(j["lines"][0]["text"], "[es] The lord is my shepherd I shall not want.")
        self.assertEqual(j["lines"][0]["original"], "The lord is my shepherd I shall not want.")
        self.assertEqual(j["partial"], "")  # half-heard words aren't translated
        # English at the same time is untouched
        e = self.call("/api/listen/captions/speech?since=0", token=a)[1]
        self.assertTrue(e["lines"][0]["text"].startswith("The lord"))

    def test_joining_late_still_gets_translations(self):
        a = self.login("Admin")
        self.wait_lines("/api/listen/captions/speech?since=0", a, n=6, timeout=20)  # six lines in English first
        j = self.wait_lines("/api/listen/captions/speech?since=0&lang=fa", a)
        self.assertTrue(j["lines"] and all(x["text"].startswith("[fa]") for x in j["lines"]))
        self.assertLessEqual(len(j["lines"]), 4)  # catches up on the last few, not the whole service
        last = j["lines"][-1]["id"]
        j2 = self.wait_lines("/api/listen/captions/speech?since=%d&lang=fa" % last, a)
        self.assertTrue(j2["lines"][0]["id"] > last and j2["lines"][0]["text"].startswith("[fa]"))

    def test_language_not_installed(self):
        self.assertEqual(self.call("/api/listen/captions/speech?since=0&lang=de", token=self.login("Admin"))[0], 404)
        self.assertEqual(self.call("/api/listen/captions/speech?since=0&lang=../x", token=self.login("Admin"))[0], 404)


@unittest.skipUnless(os.environ.get("C3_VOSK_MODEL") and os.environ.get("C3_ARGOS_DIR"), "set C3_VOSK_MODEL, C3_SPEECH and C3_ARGOS_DIR to test real translation")
class RealTranslation(ListenTest):
    def test_psalm_in_spanish(self):
        self.httpd.listen.cfg.update(listen_input=["-re", "-i", os.environ["C3_SPEECH"]], captions_model=os.environ["C3_VOSK_MODEL"])
        self.httpd.listen.translator.dir = os.environ["C3_ARGOS_DIR"]
        c = self.httpd.listen.config()
        c["sources"][3]["ch"] = [1]
        self.httpd.listen.save(c)
        a = self.login("Admin")
        self.assertIn("es", [x["code"] for x in self.call("/api/listen/mine", token=a)[1]["languages"]])
        text, t0 = "", time.time()
        while time.time() - t0 < 30 and "pastor" not in text.lower():
            time.sleep(1)
            text = " ".join(x["text"] for x in self.call("/api/listen/captions/speech?since=0&lang=es", token=a)[1]["lines"])
        print("\n  Spanish:", text)
        self.assertIn("pastor", text.lower())


class Transcripts(ListenTest):
    def setUp(self):
        super().setUp()
        self.httpd.listen.cfg["captions_engine"] = FakeEngine

    def lines(self, tok, n=1, timeout=12):
        t0, j = time.time(), {}
        while time.time() - t0 < timeout:
            j = self.call("/api/listen/captions/speech?since=0", token=tok)[1]
            if len(j.get("lines", [])) >= n:
                break
            time.sleep(0.5)
        return j["lines"]

    def test_saved_and_carries_on_after_a_restart(self):
        a = self.login("Admin")
        got = self.lines(a, 2)
        day = time.strftime("%Y-%m-%d")
        f = os.path.join(self.tmp, "data", "transcripts", "%s-speech.jsonl" % day)
        self.assertTrue(os.path.exists(f))
        # the C3 Life server restarts: a new Listen on the same data folder
        self.httpd.listen.captioners["speech"].stop()
        fresh = L.Listen(os.path.join(self.tmp, "data"), dict(self.httpd.listen.cfg), self.httpd.listen.log)
        r = fresh.captions({"id": "speech", "name": "Speech", "ch": [9], "captions": True}, 0)
        self.assertGreaterEqual(len(r["lines"]), 2)
        self.assertEqual(r["lines"][0]["text"], got[0]["text"])
        fresh.captioners["speech"].stop()
        self.assertEqual(fresh.captioners["speech"].next_id, r["lines"][-1]["id"] + 1)  # numbering carries on

    def test_admin_downloads_the_transcript(self):
        a = self.login("Admin")
        self.lines(a, 1)
        lst = self.call("/api/listen/transcripts", token=a)[1]
        self.assertEqual(lst["days"], 30)
        t = lst["transcripts"][0]
        self.assertEqual((t["source"], t["day"]), ("speech", time.strftime("%Y-%m-%d")))
        code, j = self.call("/api/listen/transcript/%s-speech.txt?t=%s" % (t["day"], a))
        self.assertEqual(code, 200)
        self.assertIn(b"The lord is my shepherd", j["raw"])
        f = self.login("FOH sound")
        self.assertEqual(self.call("/api/listen/transcript/%s-speech.txt?t=%s" % (t["day"], f))[0], 403)
        self.assertEqual(self.call("/api/listen/transcripts", token=f)[0], 403)
        self.assertEqual(self.call("/api/listen/transcript/..%%2F..%%2Fusers.txt?t=%s" % a)[0], 404)

    def test_admin_chooses_how_long_and_deletes(self):
        a = self.login("Admin")
        self.lines(a, 1)
        self.assertEqual(self.call("/api/listen/transcripts", "PUT", {"days": 90}, token=a)[1]["days"], 90)
        self.assertEqual(self.call("/api/listen/transcripts", "PUT", {"days": 12}, token=a)[0], 400)
        self.assertEqual(self.call("/api/listen/transcripts", "PUT", {"days": 7}, token=self.login("FOH sound"))[0], 403)
        # kept after a restart
        self.assertEqual(L.Transcripts(self.httpd.listen.transcripts.folder, 30, self.httpd.listen.log).days, 90)
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(self.call("/api/listen/transcript/%s-speech.txt" % day, "DELETE", token=a)[0], 200)
        self.assertEqual(self.call("/api/listen/transcripts", token=a)[1]["transcripts"], [] if not self.httpd.listen.transcripts.list() else self.httpd.listen.transcripts.list())
        self.assertEqual(self.call("/api/listen/transcript/..%2Fx.txt", "DELETE", token=a)[0], 404)

    def test_dont_save_deletes_them_now(self):
        a = self.login("Admin")
        self.lines(a, 1)
        self.httpd.listen.captioners["speech"].stop()
        self.assertEqual(self.call("/api/listen/transcripts", "PUT", {"days": 0}, token=a)[0], 200)
        self.assertEqual(self.httpd.listen.transcripts.list(), [])

    def test_old_transcripts_deleted(self):
        tr = self.httpd.listen.transcripts
        old = os.path.join(tr.folder, "2020-01-05-speech.jsonl")
        with open(old, "w") as fh:
            fh.write('{"id":1,"t":"10:00:00","text":"Old."}\n')
        tr.last_tidy = 0
        tr.tidy()
        self.assertFalse(os.path.exists(old))

    def test_zero_days_saves_nothing(self):
        self.httpd.listen.transcripts.days = 0
        self.lines(self.login("Admin"), 1)
        self.assertEqual(self.httpd.listen.transcripts.list(), [])


class PerPerson(ListenTest):
    def person(self, a, name, sources=(), fixed="", password="c3life-admin"):
        return self.call("/api/listen/person", "PUT", {"name": name, "sources": list(sources), "fixed": fixed, "password": password}, token=a)

    def mine(self, name):
        return [x["id"] for x in self.call("/api/listen/mine", token=self.login(name))[1]["sources"]]

    def test_admin_gives_one_person_more(self):
        a = self.login("Admin")
        self.assertEqual(self.mine("Lighting"), [])
        self.assertEqual(self.person(a, "Lighting", ["speech"])[0], 200)
        self.assertEqual(self.mine("Lighting"), ["speech"])
        # and on top of their groups
        self.assertEqual(self.person(a, "Foyer team", ["speech"])[0], 200)
        self.assertEqual(sorted(self.mine("Foyer team")), ["foyer", "main", "speech"])

    def test_kept_to_one_source(self):
        a = self.login("Admin")
        self.assertEqual(self.person(a, "Foyer team", fixed="foyer")[0], 200)
        self.assertEqual(self.mine("Foyer team"), ["foyer"])  # their groups no longer add anything
        f = self.login("Foyer team")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/main.mp3?t=%s" % (self.port, f), timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.person(a, "Foyer team")  # cleared: back to their groups
        self.assertEqual(sorted(self.mine("Foyer team")), ["foyer", "main"])

    def test_listener_account_needs_no_pin_and_hears_main(self):
        code, j = self.call("/api/login", "POST", {"name": "Listener", "pin": ""})
        self.assertEqual((code, j["role"]), (200, "Listener"))
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=j["token"])[1]["sources"]], ["main"])
        self.assertEqual(self.call("/api/listen/config", token=j["token"])[0], 403)  # can't change anything

    def test_needs_the_admin_password_again(self):
        a = self.login("Admin")
        self.assertEqual(self.person(a, "Lighting", ["main"], password="wrong-pass1")[0], 401)
        self.assertEqual(self.person(a, "Lighting", ["main"], password="")[0], 401)
        self.assertEqual(self.mine("Lighting"), [])
        f = self.login("FOH sound")
        self.assertEqual(self.person(f, "Lighting", ["main"])[0], 403)  # only Admins
        self.assertEqual(self.person(a, "Nobody", ["main"])[0], 400)
        self.assertEqual(self.person(a, "Lighting", ["nope"])[0], 400)

    def test_listen_settings_keep_people(self):
        a = self.login("Admin")
        self.person(a, "Lighting", ["speech"])
        c = self.call("/api/listen/config", token=a)[1]
        self.assertEqual(c["people"]["Lighting"]["sources"], ["speech"])
        body = {k: c[k] for k in ("sources", "groups", "rooms")}
        body["sources"][0]["name"], body["sources"][0]["dante"] = "FOH", True  # named from Dante
        self.assertEqual(self.call("/api/listen/config", "PUT", body, token=a)[0], 200)
        c = self.call("/api/listen/config", token=a)[1]
        self.assertEqual(c["people"]["Lighting"]["sources"], ["speech"])
        self.assertTrue(c["sources"][0]["dante"])


class NoSignIn(ListenTest):
    open_group = True

    def test_anyone_hears_the_open_group_without_signing_in(self):
        code, j = self.call("/api/listen/mine")
        self.assertEqual(code, 200)
        self.assertTrue(j["guest"])
        self.assertEqual([x["id"] for x in j["sources"]], ["stream"])
        r, data = self.listen_bytes("stream", "")
        r.close()
        self.assertGreater(len(data), 3000)

    def test_only_the_open_group(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/main.mp3?t=" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.call("/api/listen/config")[0], 401)  # and nothing else

    def test_signed_in_people_hear_it_too(self):
        self.assertEqual(sorted(x["id"] for x in self.call("/api/listen/mine", token=self.login("Foyer team"))[1]["sources"]), ["foyer", "main", "stream"])

    def test_livestream_has_subtitles_from_the_speech_mics(self):
        self.httpd.listen.cfg.update(captions_engine=FakeEngine, translate_engine=FakeTranslate, translate_langs=[("es", "Spanish")])
        t0, j = time.time(), {}
        while time.time() - t0 < 12 and not j.get("lines"):
            time.sleep(0.5)
            j = self.call("/api/listen/captions/stream?since=0&lang=es")[1]  # no sign-in
        self.assertTrue(j["lines"][0]["text"].startswith("[es] The lord is my shepherd"))
        self.assertIn("speech", self.httpd.listen.captioners)  # made from the speech mics
        self.assertEqual(self.call("/api/listen/captions/main?since=0", token=self.login("Admin"))[0], 404)  # main has none

    def test_admin_can_close_it(self):
        a = self.login("Admin")
        c = self.call("/api/listen/config", token=a)[1]
        next(g for g in c["groups"] if g["id"] == "everyone")["open"] = False
        self.assertEqual(self.call("/api/listen/config", "PUT", {k: c[k] for k in ("sources", "groups", "rooms")}, token=a)[0], 200)
        self.assertEqual(self.call("/api/listen/mine")[0], 401)


class ListenPages(ListenTest):
    """One simple page per feed, at /listen or /listen/<id>: no sign-in, that page's feed only, in the building."""
    open_group = True

    def where(self, path):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.getheader("Location")

    def pages(self, a, pages):
        return self.call("/api/listen/pages", "PUT", {"pages": pages}, token=a)

    def page_bytes(self, sid, page, n=3000):
        r = urllib.request.urlopen("http://127.0.0.1:%d/listen/%s.mp3?page=%s" % (self.port, sid, page), timeout=8)
        got, t0 = b"", time.time()
        while len(got) < n and time.time() - t0 < 8:
            got += r.read(1024)
        r.close()
        return got

    def test_each_address_opens_its_page(self):
        self.assertEqual(self.where("/listen"), (302, "/player.html?p=Hear.dc.html&page="))
        self.assertEqual(self.where("/listen/"), (302, "/player.html?p=Hear.dc.html&page="))
        self.assertEqual(self.where("/listen/Kids"), (302, "/player.html?p=Hear.dc.html&page=kids"))
        self.assertEqual(self.where("/foyer"), (302, "/player.html?p=FoyerSimple.dc.html"))

    def test_main_page_plays_what_was_already_open_to_everyone(self):
        code, j = self.call("/api/listen/page?page=")
        self.assertEqual(code, 200)
        self.assertEqual((j["page"]["name"], j["page"]["on"], j["source"]["id"]), ("Hear the service", True, "stream"))
        self.assertGreater(len(self.page_bytes("stream", "")), 2000)
        with self.assertRaises(urllib.error.HTTPError) as e:  # only that page's feed
            urllib.request.urlopen("http://127.0.0.1:%d/listen/main.mp3?page=" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 403)

    def test_admin_adds_changes_turns_off_and_deletes(self):
        a = self.login("Admin")
        main = self.call("/api/listen/pages", token=a)[1]["pages"][0]
        kids = {"id": "kids", "name": "Kids church", "source": "speech", "words": True, "on": True}
        self.assertEqual(self.pages(a, [main, kids])[0], 200)
        j = self.call("/api/listen/page?page=kids")[1]
        self.assertEqual((j["page"]["name"], j["source"]["id"]), ("Kids church", "speech"))
        self.assertGreater(len(self.page_bytes("speech", "kids")), 2000)
        self.assertEqual(self.pages(a, [main, dict(kids, on=False)])[0], 200)  # off: the address stays, nothing plays
        self.assertFalse(self.call("/api/listen/page?page=kids")[1]["page"]["on"])
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/speech.mp3?page=kids" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.pages(a, [main])[0], 200)  # deleted
        self.assertEqual(self.call("/api/listen/page?page=kids"), (404, {"ok": False, "error": "There's no page here."}))

    def test_main_page_cant_be_deleted_and_bad_pages_refused(self):
        a = self.login("Admin")
        main = self.call("/api/listen/pages", token=a)[1]["pages"][0]
        self.assertIn("can't be deleted", self.pages(a, [])[1]["error"].replace("but not deleted", "can't be deleted"))
        for bad in ({"id": "Kids!", "name": "x", "source": "main", "words": False, "on": False},
                    {"id": "x", "name": "", "source": "main", "words": False, "on": False},
                    {"id": "x", "name": "X", "source": "nope", "words": False, "on": False},
                    {"id": "x", "name": "X", "source": "", "words": False, "on": True}):
            self.assertEqual(self.pages(a, [main, bad])[0], 400, bad)
        self.assertEqual(self.pages(a, [main, dict(main)])[0], 400)  # two pages at /listen

    def test_only_an_admin_changes_pages_or_makes_posters(self):
        f = self.login("FOH sound")
        self.assertEqual(self.call("/api/listen/pages", token=f)[0], 403)
        self.assertEqual(self.pages(f, [])[0], 403)
        self.assertEqual(self.call("/api/listen/qr.svg?text=http://x/listen&t=" + f)[0], 403)
        code, j = self.call("/api/listen/qr.svg?text=http%3A%2F%2F192.168.10.10%2Flisten%2Fkids&t=" + self.login("Admin"))
        self.assertEqual(code, 200)
        self.assertTrue(j["raw"].startswith(b"<svg"))

    def test_words_on_screen_follow_the_page(self):
        self.httpd.listen.cfg.update(captions_engine=FakeEngine)
        a = self.login("Admin")
        main = self.call("/api/listen/pages", token=a)[1]["pages"][0]
        self.assertEqual(self.pages(a, [dict(main, words=False)])[0], 200)
        self.assertEqual(self.call("/api/listen/captions/stream?since=0&page=")[0], 404)
        self.assertEqual(self.pages(a, [dict(main, words=True)])[0], 200)
        self.assertEqual(self.call("/api/listen/captions/stream?since=0&page=")[0], 200)

    def test_removing_a_feed_turns_its_page_off(self):
        a = self.login("Admin")
        main = self.call("/api/listen/pages", token=a)[1]["pages"][0]
        self.assertEqual(self.pages(a, [main, {"id": "foyer", "name": "Foyer", "source": "foyer", "words": False, "on": True}])[0], 200)
        c = self.call("/api/listen/config", token=a)[1]
        c["sources"] = [x for x in c["sources"] if x["id"] != "foyer"]
        for g in c["groups"]:
            g["sources"] = [x for x in g["sources"] if x != "foyer"]
        self.assertEqual(self.call("/api/listen/config", "PUT", {k: c[k] for k in ("sources", "groups", "rooms")}, token=a)[0], 200)
        p = next(x for x in self.call("/api/listen/pages", token=a)[1]["pages"] if x["id"] == "foyer")
        self.assertEqual((p["source"], p["on"]), ("", False))


class ListenPagesClosed(ListenTest):
    def test_main_page_off_when_nothing_was_open_to_everyone(self):
        j = self.call("/api/listen/page?page=")[1]
        self.assertFalse(j["page"]["on"])
        self.assertIsNone(j["source"])
        with self.assertRaises(urllib.error.HTTPError) as e:  # and its feed can't be heard without signing in
            urllib.request.urlopen("http://127.0.0.1:%d/listen/stream.mp3?page=" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 403)


class ListenPagesFromHome(ListenTest):
    open_group = True
    remote = True

    def test_pages_only_in_the_building(self):
        code, j = self.call("/api/listen/page?page=")
        self.assertTrue(code == 403 or not j["page"]["on"], (code, j))
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/stream.mp3?page=" % self.port, timeout=5)
        self.assertEqual(e.exception.code, 403)


class QRCodes(unittest.TestCase):
    def test_reads_back(self):
        import qr
        for text in ("http://192.168.10.10/listen", "http://192.168.10.10/listen/kids", "http://c3life.local/listen/overflow-hall-" + "x" * 90):
            m = qr.matrix(text)
            self.assertEqual(len(m) % 4, 1)  # 21, 25, 29... modules across
            try:
                import zxingcpp
                import numpy as np
            except ImportError:
                continue  # the reader isn't installed here: the shape check above still runs
            n, k = len(m), 6
            img = np.full(((n + 8) * k, (n + 8) * k), 255, np.uint8)
            for r in range(n):
                for c in range(n):
                    if m[r][c]:
                        img[(r + 4) * k:(r + 5) * k, (c + 4) * k:(c + 5) * k] = 0
            self.assertEqual(zxingcpp.read_barcodes(img)[0].text, text)


class AddFeed(unittest.TestCase):
    """Add a feed: pick where it comes from on Dante; the HP's channel is chosen for it, sharing one already coming in."""

    def test_plan_shares_what_is_already_coming_in(self):
        dvs = {"9": {"tx_device": "WING-ABC", "tx_channel": "12"}, "10": {"tx_device": "Tio-1", "tx_channel": "05"}}
        plan, err = L.plan_feed([{"dev": "wing-abc", "ch": "12"}], dvs, used={4, 5})
        self.assertEqual((err, plan[0]["dvs"], plan[0]["share"]), ("", 9, True))
        plan, err = L.plan_feed([{"dev": "Tio-2", "ch": "05"}, {"dev": "Tio-2", "ch": "06"}], dvs, used={4, 5, 6, 7, 8})
        self.assertEqual([(p["dvs"], p["share"]) for p in plan], [(11, False), (12, False)])  # side by side, not 1-3 (RTA, MD log)
        full = {str(k): {"tx_device": "X", "tx_channel": str(k)} for k in range(4, 65)}
        self.assertIn("no free channel", L.plan_feed([{"dev": "Y", "ch": "1"}], full, used=set())[1])
        self.assertEqual(L.slug("Choir mics!", {"choir-mics"}), "choir-mics-2")

    def setUp(self):
        import sys
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "dante.json")
        with open(self.state, "w") as f:
            json.dump({"subs": [{"rx_device": "C3LIFE", "rx_channel": "09", "rx_channel_number": 9, "tx_device": "WING-ABC", "tx_channel": "Speech"}],
                       "tx": {"WING-ABC": {"38": "Speech"}, "Tio-2": {"5": "CH5 Choir L", "6": "CH6 Choir R"}}}, f)
        os.environ["FAKE_DANTE"] = self.state
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<x-dc></x-dc>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), listen_input=FAKE_DVS, propresenter="",
                   dante_dvs_name="C3LIFE", netaudio=[sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_netaudio.py")])
        cfg["_nets"], cfg["_remote"] = [s.ipaddress.ip_network("127.0.0.0/8")], []
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.httpd.recorder.dante.refresh()
        self.a = ListenTest.login(self, "Admin")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    call = ListenTest.call

    def subs(self):
        with open(self.state) as f:
            return [(x["rx_channel_number"], x["tx_device"], x["tx_channel"]) for x in json.load(f)["subs"]]

    def test_sources_to_choose_from(self):
        code, j = self.call("/api/listen/feed/sources", token=self.a)
        self.assertEqual(code, 200)
        self.assertIn({"dev": "Tio-2", "ch": "CH5 Choir L", "num": 5}, j["sources"])
        self.assertEqual(self.call("/api/listen/feed/sources", token=ListenTest.login(self, "FOH sound"))[0], 403)

    def test_check_lists_the_changes_and_changes_nothing(self):
        body = {"name": "Choir mics", "chans": [{"dev": "Tio-2", "ch": "CH5 Choir L"}, {"dev": "Tio-2", "ch": "CH6 Choir R"}], "page": "choir", "check": True}
        code, j = self.call("/api/listen/feed", "POST", body, token=self.a)
        self.assertEqual(code, 200)
        self.assertEqual([p["dvs"] for p in j["plan"]], [10, 11])
        self.assertTrue(any("Send Tio-2 · CH5 Choir L" in c and "desk and the stage don't change" in c for c in j["changes"]))
        self.assertTrue(any("/listen/choir" in c for c in j["changes"]))
        self.assertEqual(len(self.subs()), 1)  # nothing routed yet
        self.assertNotIn("choir-mics", [x["id"] for x in self.httpd.listen.config()["sources"]])

    def test_add_routes_it_and_makes_the_page(self):
        body = {"name": "Choir mics", "chans": [{"dev": "Tio-2", "ch": "CH5 Choir L"}, {"dev": "Tio-2", "ch": "CH6 Choir R"}], "page": "choir", "words": False}
        code, j = self.call("/api/listen/feed", "POST", body, token=self.a)
        self.assertEqual(code, 200, j)
        self.assertIn((10, "Tio-2", "CH5 Choir L"), self.subs())
        self.assertIn((11, "Tio-2", "CH6 Choir R"), self.subs())
        src = next(x for x in self.httpd.listen.config()["sources"] if x["id"] == "choir-mics")
        self.assertEqual(src["ch"], [10, 11])
        self.assertEqual(self.httpd.listen.page_source("choir")["id"], "choir-mics")

    def test_shared_source_routes_nothing(self):
        code, j = self.call("/api/listen/feed", "POST", {"name": "Speech", "chans": [{"dev": "WING-ABC", "ch": "Speech"}], "words": True}, token=self.a)
        self.assertEqual(code, 200, j)
        self.assertEqual((j["plan"][0]["dvs"], j["plan"][0]["share"]), (9, True))
        self.assertEqual(len(self.subs()), 1)

    def test_bad_requests(self):
        for body in ({"name": "", "chans": [{"dev": "a", "ch": "b"}]}, {"name": "X", "chans": []}, {"name": "X", "chans": [{"dev": "a"}]},
                     {"name": "X", "chans": [{"dev": "Tio-2", "ch": "CH5 Choir L"}], "page": "Bad Word"},
                     {"name": "X", "chans": [{"dev": "Tio-2", "ch": "CH5 Choir L"}], "page": ""}):  # the main page is already there
            self.assertEqual(self.call("/api/listen/feed", "POST", body, token=self.a)[0], 400, body)
        self.assertEqual(self.call("/api/listen/feed", "POST", {"name": "X", "chans": [{"dev": "a", "ch": "b"}]}, token=ListenTest.login(self, "FOH sound"))[0], 403)


class NoSignInFromHome(ListenTest):
    open_group = True
    remote = True

    def test_not_from_home(self):
        self.assertEqual(self.call("/api/listen/mine")[0], 401)  # from home you still sign in; the public feed is for everyone else


class AccessLevels(ListenTest):
    def roles(self):
        return self.call("/api/access")[1]["roles"]

    def put(self, roles, tok, pw="c3life-admin"):
        return self.call("/api/access", "PUT", {"roles": roles, "password": pw}, token=tok)

    def test_everyone_sees_the_same_roles(self):
        r = self.roles()
        self.assertEqual([x["name"] for x in r][:2], ["Admin", "FOH sound"])
        self.assertEqual(len(r[0]["p"]), len(s.access_mod.AREAS))  # one level per area

    def test_listen_none_means_no_listening_for_that_role(self):
        a = self.login("Admin")
        self.assertEqual([x["id"] for x in self.call("/api/listen/mine", token=self.login("Foyer team"))[1]["sources"]], ["foyer", "main"])
        r = self.roles()
        next(x for x in r if x["name"] == "Foyer team")["p"][s.access_mod.LISTEN] = 0
        self.assertEqual(self.put(r, a)[0], 200)
        f = self.login("Foyer team")
        self.assertEqual(self.call("/api/listen/mine", token=f)[1]["sources"], [])
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/listen/main.mp3?t=%s" % (self.port, f), timeout=5)
        self.assertEqual(e.exception.code, 403)

    def test_monitor_mixes_follow_access_levels(self):
        a = self.login("Admin")
        k = self.login("Keys (musician)")
        self.assertEqual(self.call("/api/mixes", "PUT", {"mymix": {"bus": 4}}, token=k)[0], 200)
        r = self.roles()
        next(x for x in r if x["name"] == "Musician")["p"][12] = 1  # own monitor mix: view only
        self.put(r, a)
        self.assertEqual(self.call("/api/mixes", "PUT", {"mymix": {"bus": 4}}, token=k)[0], 403)
        self.assertEqual(self.call("/api/mixes", token=k)[0], 200)  # can still look
        self.assertEqual(self.call("/api/mixes", token=self.login("Foyer team"))[0], 403)  # none

    def test_only_an_admin_with_their_password(self):
        r = self.roles()
        self.assertEqual(self.put(r, self.login("FOH sound"))[0], 403)
        self.assertEqual(self.put(r, self.login("Admin"), pw="nope-nope1")[0], 401)

    def test_admin_keeps_everything(self):
        a = self.login("Admin")
        r = self.roles()
        r[0]["p"][0] = 0
        self.assertEqual(self.put(r, a)[0], 400)
        r = self.roles()
        r[1]["p"] = [3] * 14
        self.assertEqual(self.put(r, a)[0], 400)


class DanteNames(ListenTest):
    def setUp(self):
        super().setUp()
        import dante as D
        self.D = D

    def test_reads_what_feeds_each_dvs_channel(self):
        subs = [{"rx_channel": "04", "rx_channel_number": 4, "rx_device": "C3LIFE", "tx_channel": "FOH L", "tx_device": "WING-ABC123", "status": {"label": "Connected"}},
                {"rx_channel": "05", "rx_channel_number": 5, "rx_device": "C3LIFE", "tx_channel": "FOH R", "tx_device": "WING-ABC123", "status": {"label": "Connected"}},
                {"rx_channel": "01", "rx_channel_number": 1, "rx_device": "Tio1608-D", "tx_channel": "01", "tx_device": "WING-ABC123"}]
        p = os.path.join(self.tmp, "fake_netaudio.py")
        with open(p, "w") as fh:
            fh.write("import json\nprint(json.dumps(%s))\n" % json.dumps(subs))
        import sys
        r = self.D.DanteReader({"netaudio": [sys.executable, p], "dante_dvs_name": "c3life"}, self.httpd.listen.log)
        r.refresh()
        d = r.get()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["channels"]["4"], {"tx_device": "WING-ABC123", "tx_channel": "FOH L", "rx_channel": "04", "status": "Connected"})
        self.assertNotIn("1", d["channels"])  # the Tio's own subscription isn't ours

    def test_says_why_when_it_cant(self):
        import sys
        r = self.D.DanteReader({"netaudio": [sys.executable, "-c", "print('[]')"], "dante_dvs_name": "C3LIFE"}, self.httpd.listen.log)
        r.refresh()
        self.assertEqual(r.get()["status"], "unavailable")
        self.assertIn("No routes into c3life", r.get()["reason"])
        r2 = self.D.DanteReader({"netaudio": ["/nonexistent/netaudio"]}, self.httpd.listen.log)
        r2.refresh()
        self.assertIn("isn't installed", r2.get()["reason"])

    def test_keeps_the_last_good_read(self):
        import sys
        p = os.path.join(self.tmp, "n.py")
        with open(p, "w") as fh:
            fh.write('import json\nprint(json.dumps([{"rx_channel_number": 9, "rx_device": "C3LIFE", "tx_channel": "Speech", "tx_device": "WING"}]))\n')
        r = self.D.DanteReader({"netaudio": [sys.executable, p], "dante_dvs_name": "C3LIFE"}, self.httpd.listen.log)
        r.refresh()
        r.cfg["netaudio"] = [sys.executable, "-c", "print('[]')"]
        r.refresh()
        d = r.get()
        self.assertEqual((d["status"], d["channels"]["9"]["tx_channel"], d.get("stale")), ("unavailable", "Speech", True))

    def test_only_admins(self):
        self.assertEqual(self.call("/api/dante/dvs", token=self.login("FOH sound"))[0], 403)
        self.assertEqual(self.call("/api/dante/dvs", token=self.login("Admin"))[0], 200)


class Public(ListenTest):
    def setUp(self):
        super().setUp()
        # a stand-in for ffmpeg that writes down what it was asked to do, then waits
        self.rec = os.path.join(self.tmp, "args.json")
        fake = os.path.join(self.tmp, "fake_ffmpeg.py")
        with open(fake, "w") as fh:
            # written whole, then renamed, so the test never reads it half-written
            fh.write("import json,os,sys,time\njson.dump(sys.argv[1:],open(%r,'w'))\nos.replace(%r,%r)\ntime.sleep(60)\n" % (self.rec + ".tmp", self.rec + ".tmp", self.rec))
        self.fake = fake

    def tearDown(self):
        self.httpd.listen.public.stop()
        super().tearDown()

    def test_admin_sets_it_up_and_the_password_never_comes_back(self):
        a = self.login("Admin")
        self.assertEqual(self.call("/api/listen/public", token=self.login("FOH sound"))[0], 403)
        code, j = self.call("/api/listen/public", "PUT", {"host": "stream.example.com", "port": 8000, "mount": "church", "password": "s3cret!",
                                                          "link": "https://stream.example.com/church"}, token=a)
        self.assertEqual(code, 200, j)
        j = self.call("/api/listen/public", token=a)[1]
        self.assertTrue(j["has_password"])
        self.assertNotIn("s3cret", json.dumps(j))
        self.assertEqual(j["mount"], "/church")

    def test_link_can_be_changed_any_time(self):
        a = self.login("Admin")
        self.call("/api/listen/public", "PUT", {"host": "stream.example.com", "password": "pw", "link": "https://old.example.com/a"}, token=a)
        self.httpd.listen.public.command = lambda src: [self.fake_py(), self.fake]
        self.assertEqual(self.call("/api/listen/public/start", "POST", {}, token=a)[0], 200)
        j = self.call("/api/listen/public", "PUT", {"link": "https://new.example.com/b"}, token=a)[1]
        self.assertFalse(j["restart_needed"])  # the link is just what people are given
        self.assertEqual(self.call("/api/listen/mine", token=a)[1]["public_link"], "https://new.example.com/b")
        j = self.call("/api/listen/public", "PUT", {"host": "other.example.com"}, token=a)[1]
        self.assertTrue(j["restart_needed"])
        self.assertEqual(self.call("/api/listen/public", "PUT", {"link": "javascript:alert(1)"}, token=a)[0], 400)

    def fake_py(self):
        import sys
        return sys.executable

    def test_bad_settings_refused(self):
        a = self.login("Admin")
        for bad in ({"port": 0}, {"port": "80"}, {"host": "evil.com; rm"}, {"mount": "/a\nb"}, {"source": "nope"}):
            self.assertEqual(self.call("/api/listen/public", "PUT", bad, token=a)[0], 400, bad)

    def test_wont_start_without_settings_and_never_by_itself(self):
        a = self.login("Admin")
        self.assertFalse(self.call("/api/listen/public", token=a)[1]["on"])
        code, j = self.call("/api/listen/public/start", "POST", {}, token=a)
        self.assertEqual(code, 400)
        self.assertIn("service address", j["error"])

    def test_start_sends_to_icecast_and_stop(self):
        a = self.login("Admin")
        self.call("/api/listen/public", "PUT", {"host": "stream.example.com", "port": 8001, "mount": "/live", "user": "source", "password": "p@ss word"}, token=a)
        pub = self.httpd.listen.public
        real = pub.command
        pub.command = lambda src: [self.fake_py(), self.fake] + real(src)[1:]
        code, j = self.call("/api/listen/public/start", "POST", {"source": "main"}, token=a)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.call("/api/listen/public/start", "POST", {}, token=a)[0], 400)  # already on
        t0 = time.time()
        while not os.path.exists(self.rec) and time.time() - t0 < 5:
            time.sleep(0.1)
        args = json.load(open(self.rec))
        self.assertEqual(args[-1], "icecast://source:p%40ss%20word@stream.example.com:8001/live")
        self.assertIn("pan=stereo|c0=c3|c1=c4", args)  # main is DVS 4 and 5
        self.assertTrue(self.call("/api/listen/public", token=a)[1]["running"])
        j = self.call("/api/listen/public/stop", "POST", {}, token=a)[1]
        self.assertFalse(j["on"])

    def test_error_shown_without_the_password(self):
        pub = self.httpd.listen.public
        a = self.login("Admin")
        self.call("/api/listen/public", "PUT", {"host": "stream.example.com", "password": "hunter2"}, token=a)
        pub.command = lambda src: [self.fake_py(), "-c", "import sys; sys.stderr.write('icecast://source:hunter2@stream.example.com:8000/church: 401 Unauthorized\\n')"]
        self.call("/api/listen/public/start", "POST", {}, token=a)
        t0 = time.time()
        while not pub.error and time.time() - t0 < 5:
            time.sleep(0.1)
        j = self.call("/api/listen/public", token=a)[1]
        self.assertIn("401", j["error"])
        self.assertNotIn("hunter2", j["error"])


if __name__ == "__main__":
    unittest.main()


class NoSound(ListenTest):
    """No sound to stream: the phone is told why, in words, instead of a stream that never plays."""

    def test_ffmpeg_missing(self):
        self.httpd.listen.cfg["ffmpeg"] = os.path.join(self.tmp, "no-ffmpeg")
        tok = self.login("Admin")
        st, j = self.call("/listen/main.mp3?t=" + tok)
        self.assertEqual(st, 503)
        self.assertIn("ffmpeg isn't installed", j["error"])

    def test_no_sound_device(self):
        self.httpd.listen.cfg["listen_input"] = ["-f", "lavfi", "-i", "nosuchsource=x"]
        self.httpd.listen.cfg["listen_first_wait"] = 3
        tok = self.login("Admin")
        st, j = self.call("/listen/main.mp3?t=" + tok)
        self.assertEqual(st, 503)
        self.assertIn("No sound arrived", j["error"])
