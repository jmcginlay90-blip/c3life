"""Music: Spotify through a C3 Life helper (helper/c3life_helper.py) on the computer that plays it, usually the Mac mini.

The C3 Life server keeps the playlists (so every device shows the same list) and passes each button press to the helper
with its key, which phones never see. The helpers are found by the network scan and kept in helpers.py. On a Mac the
helper does everything with AppleScript, including fades, so a fade finishes even if the phone locks; on a Windows PC
it can only press Play/Pause, Next and Previous. "Open ProPresenter" uses the helper on the computer ProPresenter is
set up on ("propresenter" in config.json), or else the music one.

data/music.json: {"playlists": [{"name", "uri"}], "fade_seconds": 10, "walk_in_volume": 30, "helper_id": ""}
("helper_id": which computer plays the music; "" = the only one that can)
"""

import json
import os
import re
import threading
import time
import urllib.parse

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


DEFAULT = {"playlists": [], "fade_seconds": 10, "walk_in_volume": 30, "helper_id": ""}
MAX_PLAYLISTS = 30
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
    if not isinstance(d.get("helper_id"), str) or len(d["helper_id"]) > 40:
        return "Which computer plays the music isn't understood."
    return None


class Music:
    def __init__(self, data_dir, cfg, log, helpers):
        self.path = os.path.join(data_dir, "music.json")
        self.cfg, self.log, self.helpers = cfg, log, helpers
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

    # which helper
    def helper(self):
        """The helper that plays the music, or None. The first one used is kept, so adding a second computer later
        doesn't leave C3 Life unsure which one plays."""
        hid = self.get()["helper_id"]
        h = self.helpers.pick("spotify", hid)
        if h and not hid and h.get("key"):
            with self.lock:
                self.d["helper_id"] = h["id"]
                self._write()
        return h

    def pp_helper(self):
        host = urllib.parse.urlparse(self.cfg.get("propresenter") or "").hostname
        h = self.helpers.at_host(host, "propresenter") if host else None
        return h or self.helpers.pick("propresenter", (self.helper() or {}).get("id", ""))

    # what phones see
    def get(self):
        with self.lock:
            return {k: (list(self.d[k]) if k == "playlists" else self.d[k]) for k in DEFAULT}

    def view(self):
        t, v = self.cache
        if v is None or time.time() - t > CACHE:
            h, p = self.helper(), self.pp_helper()
            ok, sp = self.helpers.ask(h, "/spotify")
            if not h and any("spotify" in x["can"] for x in self.helpers.view()):
                sp = "More than one computer has the C3 Life helper. Choose which one plays the music (below), and an Admin types its key."
            okp, pp = self.helpers.ask(p, "/propresenter") if p else (False, None)
            about = lambda x: {"id": x["id"], "name": x.get("name") or "", "address": x["url"].split("//")[-1], "os": x.get("os") or "",
                               "can": x.get("can") or [], "paired": bool(x.get("key"))} if x else None
            v = {"helper": dict(about(h) or {}, ok=ok, error="" if ok else sp, found=bool(h)),
                 "spotify": sp if ok else {"running": False},
                 "propresenter": {"running": bool(pp.get("running")) if okp else None, "helper": about(p)},
                 "helpers": self.helpers.view()}
            self.cache = (time.time(), v)
        with self.lock:
            now = self.now
        return dict(self.get(), now=now, **v)

    # changes
    def save(self, d):
        d = {"playlists": [{"name": p["name"].strip(), "uri": spotify_uri(p["uri"])} for p in d["playlists"]],
             "fade_seconds": d["fade_seconds"], "walk_in_volume": d["walk_in_volume"], "helper_id": d["helper_id"]}
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
            h = self.helper()
            more = h and "spotify_more" in (h.get("can") or [])
            send = {"do": d if more or d in ("next", "previous") else "playpause"}  # Windows: one Play/Pause key
        elif d == "volume":
            send = {"do": "volume", "value": body.get("value")}
        elif d == "fade":
            send = {"do": "fade", "to": body.get("to"), "seconds": body.get("seconds", self.get()["fade_seconds"]), "then": body.get("then", "")}
        else:
            return "Not understood."
        ok, j = self.helpers.ask(self.helper(), "/spotify", send)
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
        p = self.pp_helper()
        if not p:
            return "No C3 Life helper that can open ProPresenter has been found yet."
        ok, j = self.helpers.ask(p, "/propresenter/open", {}, timeout=20)
        self.cache = (0.0, None)
        if not ok:
            return j
        self.log.info("music: %s opened ProPresenter on %s", who, p.get("name") or p["url"])
        return None
