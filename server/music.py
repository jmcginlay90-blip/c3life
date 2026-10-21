"""Music: Spotify on the Mac mini, through the C3 Life helper there (helper/c3life_helper.py).

The C3 Life server keeps the playlists (so every device shows the same list) and passes each button press to the helper
with its key, which phones never see. The helper does the rest with the Mac's own AppleScript, including fades, so a
fade finishes even if the phone locks. It can also open ProPresenter on that Mac.

config.json: "helper" (the Mac mini, default http://192.168.10.30:8099) and "helper_key" (shown when the helper starts).
data/music.json: {"playlists": [{"name", "uri"}], "fade_seconds": 10, "walk_in_volume": 30}
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

# the same check as the helper's (helper/c3life_helper.py, which runs on its own on the Mac)
URI = re.compile(r"^spotify:(playlist|album|track|artist):[A-Za-z0-9]{10,40}$")
LINK = re.compile(r"^https://open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|track|artist)/([A-Za-z0-9]{10,40})(?:[/?#].*)?$")
MAX_FADE = 60


def spotify_uri(s):
    """A Spotify URI from what someone pasted (spotify:playlist:… or a Share › Copy link), or None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if URI.match(s):
        return s
    m = LINK.match(s)
    return "spotify:%s:%s" % m.groups() if m else None


DEFAULT = {"playlists": [], "fade_seconds": 10, "walk_in_volume": 30}
MAX_PLAYLISTS = 30
NO_KEY = "The helper's key isn't set. Start the helper on the Mac mini, then put its key in config.json (\"helper_key\") or run Install-C3Life.ps1 -HelperKey."
CACHE = 1.0  # seconds: many phones looking at once ask the Mac once


def valid(d):
    if not isinstance(d, dict):
        return "Music settings not understood."
    pl = d.get("playlists")
    if not isinstance(pl, list) or len(pl) > MAX_PLAYLISTS:
        return "Up to %d playlists." % MAX_PLAYLISTS
    names = set()
    for p in pl:
        if not isinstance(p, dict):
            return "Playlist not understood."
        n = p.get("name")
        if not isinstance(n, str) or not n.strip() or len(n.strip()) > 40 or any(ord(c) < 32 for c in n):
            return "Each playlist needs a name, up to 40 characters."
        if n.strip().lower() in names:
            return "Two playlists are called %s." % n.strip()
        names.add(n.strip().lower())
        if not spotify_uri(p.get("uri")):
            return "%s: that isn't a Spotify link. In Spotify: ••• › Share › Copy link to playlist." % n.strip()
    f = d.get("fade_seconds")
    if not isinstance(f, int) or isinstance(f, bool) or not 1 <= f <= MAX_FADE:
        return "A fade is 1 to %d seconds." % MAX_FADE
    v = d.get("walk_in_volume")
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
        return "Volume is 0 to 100."
    return None


class Music:
    def __init__(self, data_dir, cfg, log):
        self.path = os.path.join(data_dir, "music.json")
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.d = dict(DEFAULT)
        self.now = None  # the playlist last started from C3 Life
        self.cache = (0.0, None)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = dict(DEFAULT, **json.load(f))
            if not valid(d):
                self.d = d
                self.now = d.get("now")
        except (OSError, ValueError, TypeError):
            pass

    # talking to the helper
    def url(self):
        return (self.cfg.get("helper") or "http://192.168.10.30:8099").rstrip("/")

    def ask(self, path, body=None, timeout=3):
        """-> (ok, json or the error in words)"""
        key = self.cfg.get("helper_key") or ""
        if not key:
            return False, NO_KEY
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url() + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"X-C3Life-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read() or b"{}")
            except ValueError:
                j = {}
            return False, j.get("error") or "The helper said no (%d)." % e.code
        except (urllib.error.URLError, OSError, ValueError):
            return False, "The helper on the Mac mini didn't answer (%s). Is the Mac on, and the helper running?" % self.url()

    # what phones see
    def get(self):
        with self.lock:
            return {k: (list(self.d[k]) if k == "playlists" else self.d[k]) for k in DEFAULT}

    def view(self):
        t, v = self.cache
        if v is None or time.time() - t > CACHE:
            ok, sp = self.ask("/spotify")
            okp, pp = self.ask("/propresenter") if ok else (False, None)
            v = {"helper": {"ok": ok, "error": "" if ok else sp, "url": self.url()},
                 "spotify": sp if ok else {"running": False},
                 "propresenter": {"running": bool(okp and pp.get("running"))} if ok else {"running": None}}
            self.cache = (time.time(), v)
        with self.lock:
            now = self.now
        return dict(self.get(), now=now, **v)

    # changes
    def save(self, d):
        d = {"playlists": [{"name": p["name"].strip(), "uri": spotify_uri(p["uri"])} for p in d["playlists"]],
             "fade_seconds": d["fade_seconds"], "walk_in_volume": d["walk_in_volume"]}
        with self.lock:
            self.d = d
            if self.now and self.now not in [p["name"] for p in d["playlists"]]:
                self.now = None
            self._write()

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(self.d, now=self.now), f, indent=2)
        os.replace(tmp, self.path)

    def do(self, body, who):
        """A button on the Music screen. -> None, or what's wrong in words"""
        if not isinstance(body, dict):
            return "Not understood."
        d = body.get("do")
        if d == "playlist":
            name = body.get("name")
            with self.lock:
                p = next((p for p in self.d["playlists"] if p["name"] == name), None)
            if not p:
                return "That playlist isn't on the list any more."
            send = {"do": "playlist", "uri": p["uri"]}
        elif d in ("play", "pause", "next", "previous"):
            send = {"do": d}
        elif d == "volume":
            send = {"do": "volume", "value": body.get("value")}
        elif d == "fade":
            send = {"do": "fade", "to": body.get("to"), "seconds": body.get("seconds", self.get()["fade_seconds"]), "then": body.get("then", "")}
        else:
            return "Not understood."
        ok, j = self.ask("/spotify", send)
        self.cache = (0.0, None)
        if not ok:
            return j
        if d == "playlist":
            with self.lock:
                self.now = name
                self._write()
        if d != "volume":
            self.log.info("music: %s: %s", who, " ".join(str(send[k]) for k in send if k != "uri") + (" " + name if d == "playlist" else ""))
        return None

    def open_propresenter(self, who):
        ok, j = self.ask("/propresenter/open", {}, timeout=20)
        self.cache = (0.0, None)
        if not ok:
            return j
        self.log.info("music: %s opened ProPresenter on the Mac mini", who)
        return None
