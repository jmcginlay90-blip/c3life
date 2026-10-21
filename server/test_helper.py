"""Tests for the C3 Life helper (helper/c3life_helper.py) and Music on the C3 Life server (music.py).
Spotify and ProPresenter are fakes: the real ones need a Mac, and haven't been tried yet.
Run: python3 -m unittest -v test_helper.py"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helper"))
import c3life_helper as hp
import c3life_server as s
import music as mm

LIST = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"


class FakeSpotify:
    """Spotify, as the helper sees it through AppleScript."""

    CAN = ("spotify", "spotify_more")

    def __init__(self):
        self.st = {"running": True, "state": "paused", "volume": 50, "position": 12.0, "track": "Way Maker", "artist": "Sinach",
                   "album": "Way Maker", "duration": 300.0, "id": "spotify:track:abc"}
        self.calls = []
        self.lock = threading.Lock()

    def state(self):
        with self.lock:
            return dict(self.st)

    def _do(self, *c):
        with self.lock:
            self.calls.append(c)
            if c[0] == "play":
                self.st["state"] = "playing"
            elif c[0] == "pause":
                self.st["state"] = "paused"
            elif c[0] == "volume":
                self.st["volume"] = int(c[1])
            elif c[0] == "play_uri":
                self.st["state"] = "playing"
        return None

    def play(self):
        return self._do("play")

    def pause(self):
        return self._do("pause")

    def playpause(self):
        return self._do("playpause")

    def next(self):
        return self._do("next")

    def previous(self):
        return self._do("previous")

    def volume(self, v):
        return self._do("volume", v)

    def play_uri(self, uri):
        return self._do("play_uri", uri)


class FakePP:
    CAN = ("propresenter",)

    def __init__(self):
        self.up = False
        self.opened = 0

    def running(self):
        return self.up

    def open(self):
        self.opened += 1
        self.up = True
        return None


class Links(unittest.TestCase):
    def test_what_people_paste(self):
        self.assertEqual(hp.spotify_uri(LIST), LIST)
        self.assertEqual(hp.spotify_uri("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=7a1b"), LIST)
        self.assertEqual(hp.spotify_uri("https://open.spotify.com/intl-en/album/1ATL5GLyefJaxhQzSPVrLX"), "spotify:album:1ATL5GLyefJaxhQzSPVrLX")
        for bad in ('spotify:playlist:abc" & do shell script "rm', "https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M", "", None, 5,
                    "spotify:playlist:37i9dQZF1DXc BWIGoYBM5M"):
            self.assertIsNone(hp.spotify_uri(bad), bad)
            self.assertIsNone(mm.spotify_uri(bad), bad)
        # nothing but a checked URI ever goes into the AppleScript
        sp = hp.Spotify(run=lambda script: self.fail("ran " + script))
        self.assertTrue(sp.play_uri('spotify:playlist:x" & quit'))


class AppleScript(unittest.TestCase):
    def test_reading_spotify(self):
        out = {"v": "playing\t30\t72,5\tWay Maker\tSinach\tWay Maker\t300000\tspotify:track:abc"}
        sp = hp.Spotify(run=lambda script: (True, out["v"]))
        st = sp.state()
        self.assertEqual((st["state"], st["volume"], st["position"], st["track"], st["duration"]), ("playing", 30, 72.5, "Way Maker", 300.0))
        out["v"] = "not running"
        self.assertEqual(sp.state(), {"running": False, "error": "Spotify isn't open on the Mac."})
        sp = hp.Spotify(run=lambda script: (False, "The Mac hasn't allowed the helper to control this app."))
        self.assertIn("allowed", sp.state()["error"])

    def test_what_it_tells_spotify(self):
        said = []
        sp = hp.Spotify(run=lambda script: (said.append(script), (True, ""))[1])
        sp.play(); sp.pause(); sp.next(); sp.previous(); sp.volume(42); sp.play_uri(LIST)
        self.assertEqual(said, ['tell application "Spotify" to play', 'tell application "Spotify" to pause', 'tell application "Spotify" to next track',
                                'tell application "Spotify" to previous track', 'tell application "Spotify" to set sound volume to 42',
                                'tell application "Spotify" to play track "%s"' % LIST])

    @unittest.skipIf(sys.platform == "darwin", "this Mac has osascript")
    def test_not_a_mac(self):
        self.assertEqual(hp.osascript("return 1"), (False, hp.NOT_MAC))


class Fades(unittest.TestCase):
    def wait(self, h):
        for _ in range(200):
            if not h.fading:
                return
            time.sleep(0.01)

    def test_fade_out_then_pause_puts_the_volume_back(self):
        sp = FakeSpotify(); sp.st.update(state="playing", volume=30)
        h = hp.Helper(spotify=sp, propresenter=FakePP(), sleep=lambda t: None)
        self.assertIsNone(h.spotify_do({"do": "fade", "to": 0, "seconds": 2, "then": "pause"}))
        self.wait(h); time.sleep(0.05)
        vols = [c[1] for c in sp.calls if c[0] == "volume"]
        self.assertEqual(vols[:-1], sorted(vols[:-1], reverse=True))
        self.assertEqual((vols[-2], vols[-1]), (0, 30))
        self.assertEqual(sp.st["state"], "paused")
        self.assertEqual(len(vols) - 1, 10)  # 2 seconds in 0.2 second steps

    def test_fade_in_from_silence(self):
        sp = FakeSpotify()
        h = hp.Helper(spotify=sp, propresenter=FakePP(), sleep=lambda t: None)
        self.assertIsNone(h.spotify_do({"do": "fade", "to": 30, "seconds": 1}))
        self.wait(h); time.sleep(0.05)
        self.assertEqual(sp.calls[:2], [("volume", 0), ("play",)])
        self.assertEqual((sp.st["volume"], sp.st["state"]), (30, "playing"))

    def test_pressing_something_else_stops_a_fade(self):
        sp = FakeSpotify(); sp.st.update(state="playing", volume=60)
        go = threading.Event()
        h = hp.Helper(spotify=sp, propresenter=FakePP(), sleep=lambda t: go.wait(2))
        h.spotify_do({"do": "fade", "to": 0, "seconds": 5, "then": "pause"})
        self.assertTrue(h.spotify_state()["fading"])
        self.assertIsNone(h.spotify_do({"do": "volume", "value": 45}))
        go.set(); time.sleep(0.1)
        self.assertFalse(h.fading)
        self.assertEqual((sp.st["volume"], sp.st["state"]), (45, "playing"))

    def test_refused(self):
        h = hp.Helper(spotify=FakeSpotify(), propresenter=FakePP(), sleep=lambda t: None)
        for bad in (None, {}, {"do": "shuffle"}, {"do": "volume", "value": 101}, {"do": "volume", "value": True}, {"do": "playlist", "uri": "x"},
                    {"do": "fade", "to": 0, "seconds": 0}, {"do": "fade", "to": 0, "seconds": 61}, {"do": "fade", "to": -1, "seconds": 5},
                    {"do": "fade", "to": 0, "seconds": 5, "then": "stop"}):
            self.assertTrue(h.spotify_do(bad), bad)


def call(port, path, body=None, key=None, method=None, tok=None):
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["X-C3Life-Key"] = key
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class HelperServer(unittest.TestCase):
    def setUp(self):
        self.sp, self.pp = FakeSpotify(), FakePP()
        self.cfg = dict(hp.DEFAULT, key="k" * 32, name="Mac mini", _quiet=True)
        self.httpd = hp.serve(self.cfg, hp.Helper(spotify=self.sp, propresenter=self.pp, sleep=lambda t: None), port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def test_hello_needs_no_key_but_everything_else_does(self):
        code, j = call(self.port, "/hello")
        self.assertEqual((code, j["service"], j["name"], j["apps"]), (200, "c3life-helper", "Mac mini", ["Spotify", "ProPresenter"]))
        self.assertEqual(call(self.port, "/spotify")[0], 401)
        self.assertEqual(call(self.port, "/spotify", key="wrong")[0], 401)
        self.assertEqual(call(self.port, "/spotify", {"do": "play"}, key="wrong")[0], 401)
        self.assertEqual(call(self.port, "/propresenter/open", {}, key="")[0], 401)
        self.assertEqual((self.sp.calls, self.pp.opened), ([], 0))
        code, j = call(self.port, "/spotify", key="k" * 32)
        self.assertEqual((code, j["track"], j["fading"]), (200, "Way Maker", False))

    def test_commands(self):
        k = "k" * 32
        self.assertEqual(call(self.port, "/spotify", {"do": "playlist", "uri": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"}, key=k)[0], 200)
        self.assertEqual(self.sp.calls[-1], ("play_uri", LIST))
        code, j = call(self.port, "/spotify", {"do": "volume", "value": 500}, key=k)
        self.assertEqual((code, j["error"]), (409, "Volume is 0 to 100."))
        self.assertEqual(call(self.port, "/propresenter", key=k)[1]["running"], False)
        self.assertEqual(call(self.port, "/propresenter/open", {}, key=k)[0], 200)
        self.assertEqual((self.pp.opened, call(self.port, "/propresenter", key=k)[1]["running"]), (1, True))

    def test_only_the_booth_network(self):
        self.cfg["allowed_networks"] = ["192.168.10.0/24"]
        httpd = hp.serve(self.cfg, hp.Helper(spotify=self.sp, propresenter=self.pp), port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertEqual(call(httpd.server_address[1], "/hello")[0], 403)
            self.assertEqual(call(httpd.server_address[1], "/spotify", {"do": "play"}, key="k" * 32)[0], 403)
            self.assertEqual(self.sp.calls, [])
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_it_answers_the_networks_this_computer_is_on(self):
        nets = hp.my_networks()
        self.assertIn("127.0.0.0/8", nets)
        self.assertTrue(all(s.ipaddress.ip_network(n).is_private for n in nets))
        self.assertIsNone(hp.DEFAULT["allowed_networks"])  # nothing to set, unless helper.json says otherwise

    def test_show_key_shows_it_and_stops(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "helper.json")
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(hp.__file__), "c3life_helper.py"), "--config", path, "--show-key"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            c = hp.load(path)
            self.assertEqual(r.stdout.strip(), c["key"])
            self.assertEqual(len(c["id"]), 16)
            r2 = subprocess.run([sys.executable, os.path.join(os.path.dirname(hp.__file__), "c3life_helper.py"), "--config", path, "--show-key"],
                                capture_output=True, text=True, timeout=30)
            self.assertEqual((r2.stdout.strip(), hp.load(path)["id"]), (c["key"], c["id"]))  # the same next time
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_key_made_once_and_kept(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "helper.json")
            r = subprocess.run([sys.executable, "-c", "import sys; sys.argv=['x','--config',%r]; import c3life_helper as h; "
                                "h.serve=lambda c: (_ for _ in ()).throw(SystemExit(0)); h.main(sys.argv[1:])" % path],
                               cwd=os.path.dirname(hp.__file__), capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            key = hp.load(path)["key"]
            self.assertGreaterEqual(len(key), 30)
            self.assertIn(key, r.stdout)  # shown, to paste into the C3 Life server
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Api(unittest.TestCase):
    """The Music screen's calls on the C3 Life server, with the helper (and fake Spotify) behind it."""

    def setUp(self):
        self.sp, self.pp = FakeSpotify(), FakePP()
        self.hcfg = dict(hp.DEFAULT, key="secret-helper-key-123456", id="a1b2c3d4", name="Mac mini", _os="mac", _quiet=True)
        self.helper = hp.serve(self.hcfg, hp.Helper(spotify=self.sp, propresenter=self.pp, sleep=lambda t: None), port=0)
        threading.Thread(target=self.helper.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "ProPresenter", "FOH sound", "Lighting", "Foyer team", "Listener"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "" if n == "Listener" else "1234"}
            self.tok[n] = call(self.port, "/api/login", body)[1]["token"]
        # what a network scan hands over when it finds the helper; then an Admin types its key on Music
        self.hport = self.helper.server_address[1]
        self.assertEqual(len(self.httpd.helpers.found([{"kind": "helper", "ip": "127.0.0.1", "port": self.hport}])), 1)
        code, j = call(self.port, "/api/helpers/key", {"id": "a1b2c3d4", "key": self.hcfg["key"]}, tok=self.tok["Admin"])
        self.assertEqual(code, 200, j)

    def tearDown(self):
        for h in (self.httpd, self.helper):
            h.shutdown(); h.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def api(self, path, body=None, who="Admin", method=None):
        self.httpd.music.cache = (0.0, None)
        return call(self.port, path, body, tok=self.tok.get(who), method=method)

    def test_playlists_and_playing(self):
        code, j = self.api("/api/music")
        self.assertEqual((code, j["helper"]["ok"], j["spotify"]["track"], j["playlists"], j["propresenter"]["running"]), (200, True, "Way Maker", [], False))
        code, j = self.api("/api/music", {"playlists": [{"name": "Walk-in", "uri": "nope"}]}, method="PUT")
        self.assertEqual(code, 400); self.assertIn("isn't a Spotify link", j["error"])
        code, j = self.api("/api/music", {"playlists": [{"name": "Walk-in", "uri": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=1"},
                                                        {"name": "After service", "uri": "spotify:album:1ATL5GLyefJaxhQzSPVrLX"}], "fade_seconds": 8}, method="PUT")
        self.assertEqual((code, [p["uri"] for p in j["playlists"]], j["fade_seconds"]), (200, [LIST, "spotify:album:1ATL5GLyefJaxhQzSPVrLX"], 8))
        code, j = self.api("/api/music/do", {"do": "playlist", "name": "Walk-in"})
        self.assertEqual((code, j["now"], self.sp.calls[-1]), (200, "Walk-in", ("play_uri", LIST)))
        self.api("/api/music/do", {"do": "volume", "value": 25})
        self.api("/api/music/do", {"do": "next"})
        self.assertEqual(self.sp.calls[-2:], [("volume", 25), ("next",)])
        code, j = self.api("/api/music/do", {"do": "fade", "to": 0, "then": "pause"})
        self.assertEqual(code, 200)
        for _ in range(100):
            if self.sp.st["state"] == "paused":
                break
            time.sleep(0.02)
        self.assertEqual(sum(1 for c in self.sp.calls if c[0] == "volume"), 1 + 40 + 1)  # 8 seconds in 0.2 second steps, then back to 25
        code, j = self.api("/api/music/do", {"do": "playlist", "name": "Gone"})
        self.assertEqual((code, j["error"]), (409, "That playlist isn't on the list any more."))
        # kept after a restart
        m = mm.Music(self.cfg["data_dir"], self.cfg, s.log, self.httpd.helpers)
        self.assertEqual((m.get()["playlists"][0]["name"], m.now), ("Walk-in", "Walk-in"))

    def test_opening_propresenter(self):
        code, j = self.api("/api/music/propresenter", {}, who="ProPresenter")
        self.assertEqual((code, self.pp.opened, j["propresenter"]["running"]), (200, 1, True))

    def test_who_may(self):
        # Music is its own access area: FOH sound runs the walk-in music without also running slides
        code, j = self.api("/api/music", who="FOH sound")
        self.assertEqual((code, j["can"]), (200, True))
        self.assertEqual(self.api("/api/music/do", {"do": "play"}, who="FOH sound")[0], 200)
        self.assertEqual(self.api("/api/music/do", {"do": "pause"}, who="ProPresenter")[0], 200)
        code, j = self.api("/api/music", who="Lighting")  # Music: view
        self.assertEqual((code, j["can"]), (200, False))
        calls = list(self.sp.calls)
        for who in ("Lighting", "Foyer team", "Listener", None):
            self.assertEqual(self.api("/api/music/do", {"do": "play"}, who=who)[0], 403 if who else 401, who)
            self.assertEqual(self.api("/api/music/propresenter", {}, who=who)[0], 403 if who else 401, who)
            self.assertEqual(self.api("/api/music", {"playlists": []}, who=who, method="PUT")[0], 403 if who else 401, who)
        for who in ("Foyer team", "Listener"):
            self.assertEqual(self.api("/api/music", who=who)[0], 403, who)
        self.assertEqual((self.sp.calls, self.pp.opened), (calls, 0))

    def test_the_key_never_reaches_a_phone(self):
        for path in ("/api/music", "/api/health"):
            self.assertNotIn(self.hcfg["key"], json.dumps(self.api(path)[1]))
        self.assertNotIn(self.hcfg["key"], json.dumps(self.api("/api/music/do", {"do": "play"})[1]))

    def test_no_key_and_helper_down(self):
        hs = self.httpd.helpers
        hs.d["a1b2c3d4"]["key"] = ""
        code, j = self.api("/api/music")
        self.assertEqual((code, j["helper"]["ok"], j["helper"]["paired"]), (200, False, False)); self.assertIn("key isn't set", j["helper"]["error"])
        code, j = self.api("/api/helpers/key", {"id": "a1b2c3d4", "key": "not-the-right-key"})
        self.assertEqual(code, 409); self.assertIn("key is wrong", j["error"])
        self.assertEqual(hs.d["a1b2c3d4"]["key"], "")  # a wrong key isn't kept
        hs.d["a1b2c3d4"].update(key=self.hcfg["key"], url="http://127.0.0.1:9")
        self.assertIn("didn't answer", self.api("/api/music")[1]["helper"]["error"])
        self.assertEqual(self.sp.calls, [])

    def test_only_an_admin_types_a_key_or_forgets_one(self):
        for who in ("FOH sound", "ProPresenter"):
            self.assertEqual(self.api("/api/helpers/key", {"id": "a1b2c3d4", "key": self.hcfg["key"]}, who=who)[0], 403)
            self.assertEqual(self.api("/api/helpers/forget", {"id": "a1b2c3d4"}, who=who)[0], 403)
        self.assertEqual(self.api("/api/helpers/find", {}, who="Lighting")[0], 403)
        self.assertEqual(self.api("/api/helpers/find", {}, who="FOH sound")[0], 200)
        code, j = self.api("/api/helpers/forget", {"id": "a1b2c3d4"})
        self.assertEqual((code, j["helpers"], j["helper"]["found"]), (200, [], False))
        self.assertIn("No C3 Life helper found yet", j["helper"]["error"])

    def test_found_again_at_a_new_address_it_is_followed(self):
        hs = self.httpd.helpers
        self.helper.shutdown(); self.helper.server_close()
        self.helper = hp.serve(self.hcfg, hp.Helper(spotify=self.sp, propresenter=self.pp, sleep=lambda t: None), port=0)  # "the Mac got a new address"
        threading.Thread(target=self.helper.serve_forever, daemon=True).start()
        self.assertIn("didn't answer", self.api("/api/music")[1]["helper"]["error"])
        ch = hs.found([{"kind": "helper", "ip": "127.0.0.1", "port": self.helper.server_address[1]}])
        self.assertIn("moved", ch[0])
        code, j = self.api("/api/music")
        self.assertEqual((j["helper"]["ok"], j["helper"]["paired"], j["spotify"]["track"]), (True, True, "Way Maker"))  # its key came with it
        self.assertEqual(len(hs.view()), 1)
        h = hs.view()[0]
        self.assertEqual((h["name"], h["os"], h["can"]), ("Mac mini", "mac", ["spotify", "spotify_more", "propresenter"]))
        self.assertNotIn("key", h)


class Several(unittest.TestCase):
    """Helpers on several computers, Macs and Windows PCs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.servers, self.fakes = [], {}
        for hid, name, osn, sp in (("mac1", "Mac mini", "mac", FakeSpotify()), ("win1", "Foyer PC", "windows", None)):
            if sp is None:
                self.keys = []
                sp = hp.WinSpotify(running=lambda: True, key=lambda vk: self.keys.append(vk))
            pp = FakePP()
            cfg = dict(hp.DEFAULT, key="key-for-" + hid, id=hid, name=name, _os=osn, _quiet=True)
            h = hp.serve(cfg, hp.Helper(spotify=sp, propresenter=pp, sleep=lambda t: None), port=0)
            threading.Thread(target=h.serve_forever, daemon=True).start()
            self.servers.append(h)
            self.fakes[hid] = (sp, pp, h.server_address[1])
        self.hs = __import__("helpers").Helpers(self.tmp, {}, s.log)
        self.hs.found([{"kind": "helper", "ip": "127.0.0.1", "port": p} for _, _, p in self.fakes.values()])
        for hid in self.fakes:
            self.assertIsNone(self.hs.pair(hid, "key-for-" + hid, "Admin"))

    def tearDown(self):
        for h in self.servers:
            h.shutdown(); h.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_says_what_it_can_do(self):
        v = {h["id"]: h for h in self.hs.view()}
        self.assertEqual(v["win1"]["can"], ["spotify", "propresenter"])
        self.assertEqual((v["win1"]["os"], v["win1"]["name"], v["win1"]["paired"]), ("windows", "Foyer PC", True))
        # kept after a restart, keys and all
        hs2 = __import__("helpers").Helpers(self.tmp, {}, s.log)
        self.assertEqual({h["id"] for h in hs2.view() if h["paired"]}, {"mac1", "win1"})

    def test_the_minute_check_says_which_stopped_answering(self):
        self.assertEqual(self.hs.check(), [])
        self.servers[1].shutdown(); self.servers[1].server_close()  # the Foyer PC goes off
        self.assertEqual(self.hs.check(), ["the C3 Life helper on Foyer PC"])
        v = {h["id"]: h for h in self.hs.view()}
        self.assertEqual((v["mac1"]["ok"], v["win1"]["ok"]), (True, False))
        self.assertGreater(v["win1"]["last_ok"], 0)  # when it last answered
        self.servers.pop(1)

    def test_two_that_play_spotify_means_choosing_one(self):
        m = mm.Music(self.tmp, {}, s.log, self.hs)
        self.assertIsNone(m.helper())  # two could: it doesn't guess
        self.assertIn("No C3 Life helper", m.do({"do": "play"}, "FOH"))
        m.save(dict(m.get(), helper_id="win1"))
        self.assertEqual(m.helper()["id"], "win1")
        # on Windows, Play and Pause are the one media key; Next and Previous are their own
        self.assertIsNone(m.do({"do": "play"}, "FOH")); self.assertIsNone(m.do({"do": "next"}, "FOH"))
        self.assertEqual(self.keys, [0xB3, 0xB0])
        self.assertIn("only press Play/Pause", m.do({"do": "volume", "value": 20}, "FOH"))
        self.assertIn("only press Play/Pause", m.do({"do": "fade", "to": 0, "then": "pause"}, "FOH"))
        m.save(dict(m.get(), helper_id="mac1"))
        self.assertIsNone(m.do({"do": "play"}, "FOH"))
        # the first one used is kept: a second computer getting its key later doesn't leave it unsure
        m2 = mm.Music(tempfile.mkdtemp(dir=self.tmp), {}, s.log, self.hs)
        self.hs.d["win1"]["key"] = ""
        self.assertEqual(m2.helper()["id"], "mac1")
        self.hs.d["win1"]["key"] = "key-for-win1"
        self.assertEqual(m2.helper()["id"], "mac1")
        self.assertEqual(self.fakes["mac1"][0].calls[-1], ("play",))

    def test_propresenter_opens_on_its_own_computer(self):
        m = mm.Music(self.tmp, {"propresenter": "http://127.0.0.1:50001"}, s.log, self.hs)
        m.save(dict(m.get(), helper_id="win1"))
        self.assertIsNotNone(m.pp_helper())  # both are on 127.0.0.1 here; the one on ProPresenter's address is used
        self.hs.d["mac1"]["url"] = "http://10.0.0.5:%d" % self.fakes["mac1"][2]
        self.assertEqual(m.pp_helper()["id"], "win1")


class Windows(unittest.TestCase):
    def test_media_keys_only_while_spotify_is_open(self):
        keys, up = [], {"v": False}
        sp = hp.WinSpotify(running=lambda: up["v"], key=lambda vk: keys.append(vk))
        self.assertEqual(sp.state(), {"running": False, "error": "Spotify isn't open on this PC."})
        self.assertEqual(sp.playpause(), "Spotify isn't open on this PC.")
        up["v"] = True
        self.assertEqual(sp.state()["state"], "unknown")
        sp.playpause(); sp.next(); sp.previous()
        self.assertEqual(keys, [0xB3, 0xB0, 0xB1])
        h = hp.Helper(spotify=sp, propresenter=hp.WinProPresenter(running=lambda: False, paths=[]))
        self.assertEqual(h.can, ["spotify", "propresenter"])
        self.assertEqual(h.spotify_do({"do": "playlist", "uri": LIST}), hp.NO_WIN)
        self.assertIsNone(h.spotify_do({"do": "playpause"}))

    def test_opening_propresenter(self):
        opened = []
        tmp = tempfile.mkdtemp()
        try:
            exe = os.path.join(tmp, "ProPresenter.exe")
            pp = hp.WinProPresenter(running=lambda: False, start=opened.append, paths=[os.path.join(tmp, "missing.exe"), exe])
            self.assertIn("isn't installed", pp.open())
            open(exe, "w").close()
            self.assertIsNone(pp.open())
            self.assertEqual(opened, [exe])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_config_json_helper_is_kept_until_a_scan_learns_its_id(self):
        tmp = tempfile.mkdtemp()
        sp, pp = FakeSpotify(), FakePP()
        h = hp.serve(dict(hp.DEFAULT, key="k" * 20, id="mm1", _quiet=True), hp.Helper(spotify=sp, propresenter=pp), port=0)
        threading.Thread(target=h.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d" % h.server_address[1]
            hs = __import__("helpers").Helpers(tmp, {"helper": url, "helper_key": "k" * 20}, s.log)
            self.assertEqual([x["id"] for x in hs.view()], ["config"])
            self.assertTrue(hs.ask(hs.pick("spotify"), "/spotify")[0])  # works straight away
            hs.found([{"kind": "helper", "ip": "127.0.0.1", "port": h.server_address[1]}])
            self.assertEqual([(x["id"], x["paired"]) for x in hs.view()], [("mm1", True)])
        finally:
            h.shutdown(); h.server_close(); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
