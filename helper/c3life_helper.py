"""The C3 Life helper: a small program on the Mac mini, next to Spotify and ProPresenter.

The C3 Life server (on the HP) can't reach apps on another computer, so this runs on the Mac that plays the music and
does what the server asks: Spotify's play, pause, next, previous, volume, a playlist and fades, and opening ProPresenter.
It uses the Mac's own AppleScript (osascript), the same way Spotify's own remote apps do, so it needs no Spotify account
key and works with Spotify in Offline mode.

    python3 c3life_helper.py            starts it on port 8099 and shows its key (paste the key into the C3 Life server)
    python3 c3life_helper.py --new-key  makes a new key (the old one stops working)

Only the C3 Life server should talk to it: every call but GET /hello needs the key (header X-C3Life-Key), and only the
booth network may connect. Kept in helper.json next to this file: {"key", "name", "port", "allowed_networks"}.

The first time the server asks it to control Spotify or ProPresenter, macOS asks once: "Terminal wants access to control
Spotify" (or Python). Click OK. To check later: System Settings > Privacy & Security > Automation.

  GET  /hello                 who it is (no key needed; the network scan uses it)
  GET  /spotify               {"running", "state": "playing|paused|stopped", "volume", "position", "track", "artist",
                               "album", "duration", "id", "fading"}
  POST /spotify               {"do": "play|pause|next|previous"}, {"do": "volume", "value": 0-100},
                              {"do": "playlist", "uri": "spotify:playlist:..."} (or an open.spotify.com link),
                              {"do": "fade", "to": 0-100, "seconds": 1-60, "then": "pause" | ""}
  GET  /propresenter          {"running"}
  POST /propresenter/open     opens ProPresenter (macOS's `open -a ProPresenter`)
"""

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = 1
PORT = 8099
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = {"key": "", "name": "", "port": PORT, "allowed_networks": ["192.168.10.0/24", "127.0.0.0/8"]}
MAX_FADE = 60
STEP = 0.2  # seconds between volume steps in a fade
PROPRESENTER_APP = "ProPresenter"
# Spotify's own ids are 22 letters and numbers; nothing else ever goes into the AppleScript.
URI = re.compile(r"^spotify:(playlist|album|track|artist):[A-Za-z0-9]{10,40}$")
LINK = re.compile(r"^https://open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|track|artist)/([A-Za-z0-9]{10,40})(?:[/?#].*)?$")
NOT_MAC = "The helper controls Spotify and ProPresenter on a Mac. This computer isn't a Mac."


def spotify_uri(s):
    """A Spotify URI from what someone pasted (spotify:playlist:… or a Share › Copy link), or None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if URI.match(s):
        return s
    m = LINK.match(s)
    return "spotify:%s:%s" % m.groups() if m else None


def osascript(script, timeout=6):
    """Runs AppleScript; -> (ok, output or the error in words)."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, NOT_MAC
    except subprocess.TimeoutExpired:
        return False, "The Mac didn't answer in time. If it's asking for permission, click OK on the Mac."
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err or "Not authorized" in err:
            return False, "The Mac hasn't allowed the helper to control this app. On the Mac: System Settings › Privacy & Security › Automation › allow Terminal (or Python) to control Spotify and ProPresenter."
        return False, err or "AppleScript failed."
    return True, (r.stdout or "").strip()


STATE_SCRIPT = '''if application "Spotify" is running then
tell application "Spotify"
set out to (player state as string) & tab & (sound volume as string) & tab & (player position as string)
try
set t to current track
set out to out & tab & (name of t) & tab & (artist of t) & tab & (album of t) & tab & ((duration of t) as string) & tab & (id of t)
end try
return out
end tell
else
return "not running"
end if'''


def _num(s):
    try:
        return float(str(s).replace(",", "."))  # AppleScript writes decimals the Mac's way (1,5 in some regions)
    except ValueError:
        return 0.0


class Spotify:
    """Spotify on this Mac, through AppleScript. Nothing here starts Spotify except play and a playlist."""

    def __init__(self, run=osascript):
        self.run = run

    def state(self):
        ok, out = self.run(STATE_SCRIPT)
        if not ok:
            return {"running": False, "error": out}
        if out == "not running":
            return {"running": False, "error": "Spotify isn't open on the Mac."}
        p = out.split("\t")
        s = {"running": True, "state": p[0] if p[0] in ("playing", "paused", "stopped") else "stopped",
             "volume": int(round(_num(p[1]))) if len(p) > 1 else 0, "position": round(_num(p[2]), 1) if len(p) > 2 else 0.0,
             "track": "", "artist": "", "album": "", "duration": 0.0, "id": ""}
        if len(p) >= 8:
            s.update(track=p[3], artist=p[4], album=p[5], duration=round(_num(p[6]) / 1000.0, 1), id=p[7])
        return s

    def tell(self, what):
        ok, out = self.run('tell application "Spotify" to ' + what)
        return None if ok else out

    def play(self):
        return self.tell("play")

    def pause(self):
        return self.tell("pause")

    def next(self):
        return self.tell("next track")

    def previous(self):
        return self.tell("previous track")

    def volume(self, v):
        return self.tell("set sound volume to %d" % int(v))

    def play_uri(self, uri):
        if not URI.match(uri or ""):
            return "That isn't a Spotify link."
        return self.tell('play track "%s"' % uri)


class ProPresenter:
    def __init__(self, run=osascript, opener=None):
        self.run = run
        self.opener = opener or (lambda: subprocess.run(["open", "-a", PROPRESENTER_APP], capture_output=True, text=True, timeout=15))

    def running(self):
        ok, out = self.run('application "%s" is running' % PROPRESENTER_APP)
        return ok and out == "true"

    def open(self):
        try:
            r = self.opener()
        except FileNotFoundError:
            return NOT_MAC
        except subprocess.TimeoutExpired:
            return "The Mac didn't open ProPresenter in time."
        if r.returncode != 0:
            return "The Mac couldn't open ProPresenter: %s" % ((r.stderr or "").strip() or "is it installed?")
        return None


class Helper:
    def __init__(self, spotify=None, propresenter=None, sleep=time.sleep):
        self.spotify = spotify or Spotify()
        self.pp = propresenter or ProPresenter()
        self.sleep = sleep
        self.lock = threading.Lock()
        self.fade_id = 0
        self.fading = False

    def spotify_state(self):
        return dict(self.spotify.state(), fading=self.fading)

    def cancel_fade(self):
        with self.lock:
            self.fade_id += 1
            self.fading = False

    def spotify_do(self, body):
        """-> None, or what's wrong in words"""
        if not isinstance(body, dict):
            return "Not understood."
        d = body.get("do")
        if d in ("play", "pause", "next", "previous"):
            self.cancel_fade()
            return getattr(self.spotify, d)()
        if d == "volume":
            v = body.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100:
                return "Volume is 0 to 100."
            self.cancel_fade()
            return self.spotify.volume(v)
        if d == "playlist":
            uri = spotify_uri(body.get("uri"))
            if not uri:
                return "That isn't a Spotify link. In Spotify: ••• › Share › Copy link to playlist."
            self.cancel_fade()
            return self.spotify.play_uri(uri)
        if d == "fade":
            to, secs, then = body.get("to"), body.get("seconds"), body.get("then", "")
            if not isinstance(to, (int, float)) or isinstance(to, bool) or not 0 <= to <= 100:
                return "Fade to 0 to 100."
            if not isinstance(secs, (int, float)) or isinstance(secs, bool) or not 0 < secs <= MAX_FADE:
                return "A fade is 1 to %d seconds." % MAX_FADE
            if then not in ("", "pause"):
                return "After a fade it can pause, or carry on."
            return self.fade(int(to), float(secs), then)
        return "Not understood."

    def fade(self, to, secs, then):
        s = self.spotify.state()
        if not s.get("running"):
            return s.get("error") or "Spotify isn't open on the Mac."
        start = s.get("volume", 0)
        if s.get("state") != "playing" and to > 0:  # fading in: from silence
            start = 0
            err = self.spotify.volume(0) or self.spotify.play()
            if err:
                return err
        with self.lock:
            self.fade_id += 1
            me = self.fade_id
            self.fading = True

        def run():
            steps = max(1, int(round(secs / STEP)))
            for k in range(1, steps + 1):
                self.sleep(secs / steps)
                with self.lock:
                    if self.fade_id != me:
                        return  # someone pressed something else
                if self.spotify.volume(round(start + (to - start) * k / steps)):
                    break
            with self.lock:
                if self.fade_id != me:
                    return
                self.fading = False
            if then == "pause":
                self.spotify.pause()
                if to == 0:
                    self.spotify.volume(start)  # back to where it was, so the next Play isn't silent

        threading.Thread(target=run, daemon=True).start()
        return None


def load(path):
    cfg = dict(DEFAULT)
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def make_handler(cfg, helper):
    nets = [ipaddress.ip_network(n, strict=False) for n in cfg.get("allowed_networks") or []]

    class H(BaseHTTPRequestHandler):
        server_version = "C3LifeHelper/%d" % VERSION

        def log_message(self, fmt, *a):
            if cfg.get("_quiet"):
                return
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

        def send_json(self, code, obj):
            b = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def allowed(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                ip = None
            if ip is None or not any(ip in n for n in nets):
                self.send_json(403, {"ok": False, "error": "Only the booth network may use the helper."})
                return False
            return True

        def keyed(self):
            k = self.headers.get("X-C3Life-Key", "")
            if not cfg.get("key") or not hmac.compare_digest(k.encode("utf-8"), cfg["key"].encode("utf-8")):
                self.send_json(401, {"ok": False, "error": "The helper's key is wrong. Copy it again from the Mac."})
                return False
            return True

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 4096:
                return None
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return None

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == "/hello":
                mac = sys.platform == "darwin" or cfg.get("_any_os")
                self.send_json(200, {"service": "c3life-helper", "name": cfg.get("name") or socket.gethostname(),
                                     "version": VERSION, "apps": ["Spotify", "ProPresenter"] if mac else []})
                return
            if not self.keyed():
                return
            if self.path == "/spotify":
                self.send_json(200, dict(helper.spotify_state(), ok=True))
            elif self.path == "/propresenter":
                self.send_json(200, {"ok": True, "running": helper.pp.running()})
            else:
                self.send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self):
            if not self.allowed() or not self.keyed():
                return
            if self.path == "/spotify":
                err = helper.spotify_do(self.body())
            elif self.path == "/propresenter/open":
                err = helper.pp.open()
            else:
                self.send_json(404, {"ok": False, "error": "Not found"})
                return
            if err:
                self.send_json(409, {"ok": False, "error": err})
            else:
                self.send_json(200, {"ok": True})

    return H


def serve(cfg, helper=None, port=None):
    httpd = ThreadingHTTPServer(("0.0.0.0" if port is None else "127.0.0.1", cfg["port"] if port is None else port),
                                make_handler(cfg, helper or Helper()))
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="C3 Life helper (Spotify and ProPresenter on this Mac)")
    ap.add_argument("--config", default=os.path.join(HERE, "helper.json"))
    ap.add_argument("--new-key", action="store_true", help="make a new key (the old one stops working)")
    a = ap.parse_args(argv)
    cfg = load(a.config)
    if not cfg.get("key") or a.new_key:
        cfg["key"] = secrets.token_urlsafe(24)
        save(a.config, cfg)
    if sys.platform != "darwin":
        print(NOT_MAC + " It will answer, but can't control anything here.")
    print("Key for the C3 Life server: %s" % cfg["key"])
    print("(Kept in %s.)" % a.config)
    httpd = serve(cfg)
    print("C3 Life helper on port %d. Stop with Ctrl+C." % cfg["port"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
