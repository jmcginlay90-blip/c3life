"""The C3 Life helper: a small program on any Mac or Windows PC in the booth, next to Spotify and ProPresenter.

The C3 Life server (on the HP) can't reach apps on another computer, so this runs on each computer that has them and
does what the server asks. What it can do depends on the computer, and it says so ("can" in /hello):
  Mac      Spotify: play, pause, next, previous, volume, a playlist, fades, and what's playing (AppleScript, the same way
           Spotify's own remote apps do, so no Spotify account key and it works in Offline mode). Opens ProPresenter.
  Windows  Spotify: Play/Pause, Next and Previous only, as the keyboard's media keys (Windows has no way to ask Spotify
           what's playing, or to pick a playlist or set its volume from outside). Opens ProPresenter.

    python3 c3life_helper.py            starts it on port 8099 and shows its key (type the key on the C3 Life server once)
    (Windows: py c3life_helper.py)
    python3 c3life_helper.py --new-key  makes a new key (the old one stops working)
    python3 c3life_helper.py --show-key shows the key (and makes one the first time), then stops

Install-Helper.command (Mac) and Install-Helper.cmd (Windows), next to this file, install it so it starts by itself
whenever someone logs in and restarts if it stops: no Terminal window to keep open.

Only the C3 Life server should talk to it: every call but GET /hello needs the key (header X-C3Life-Key), and only
private networks may connect (10.x, 172.16-31.x, 192.168.x and the computer itself: whatever the church's network
uses), never the internet. helper.json "allowed_networks" narrows it if wanted. Kept in helper.json next to this file: {"key", "id", "name", "port", "allowed_networks"}.
The C3 Life server finds the helper by itself (its network scan asks every address on port 8099 for /hello), and
follows it by its "id" if the Mac's address changes. Only the key has to be typed in, once (Music on the server).

The first time the server asks it to control Spotify or ProPresenter, macOS asks once: "Terminal wants access to control
Spotify" (or Python). Click OK. To check later: System Settings > Privacy & Security > Automation.

  GET  /hello                 who it is: {"service", "id", "name", "os", "version", "apps", "can"} (no key; the scan uses it)
  GET  /status                the same, with the key: the server checks a key someone typed
  GET  /spotify               {"running", "state": "playing|paused|stopped", "volume", "position", "track", "artist",
                               "album", "duration", "id", "fading"}
  POST /spotify               {"do": "play|pause|playpause|next|previous"}, {"do": "volume", "value": 0-100},
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
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]
DEFAULT = {"key": "", "id": "", "name": "", "port": PORT, "allowed_networks": None}  # None: any private network (PRIVATE)
MAX_FADE = 60
STEP = 0.2  # seconds between volume steps in a fade
PROPRESENTER_APP = "ProPresenter"
# Spotify's own ids are 22 letters and numbers; nothing else ever goes into the AppleScript.
URI = re.compile(r"^spotify:(playlist|album|track|artist):[A-Za-z0-9]{10,40}$")
LINK = re.compile(r"^https://open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|track|artist)/([A-Za-z0-9]{10,40})(?:[/?#].*)?$")
NOT_MAC = "The helper controls Spotify and ProPresenter on a Mac or a Windows PC. This computer is neither."
OS = "mac" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "other"
# What each part can do (the server shows only the buttons a computer can do).
#   spotify: play/pause, next, previous   spotify_more: state, play and pause apart, volume, playlists, fades
#   propresenter: open it, and say whether it's open
NO_WIN = "On a Windows PC the helper can only press Play/Pause, Next and Previous for Spotify."


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

    CAN = ("spotify", "spotify_more")

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

    def playpause(self):
        return self.tell("playpause")

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
    CAN = ("propresenter",)

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


# ---- Windows ----

VK = {"playpause": 0xB3, "next": 0xB0, "previous": 0xB1}  # the keyboard's media keys


def tasklist(exe):
    """Is this program running? (Windows' own tasklist)"""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/NH"], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return exe.lower() in (r.stdout or "").lower()


def media_key(vk):
    """Presses a media key, the way a keyboard does. Windows sends it to the app playing (Spotify). -> None or error"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(vk, 0, 1, 0)      # KEYEVENTF_EXTENDEDKEY, down
        u.keybd_event(vk, 0, 1 | 2, 0)  # and up
    except (ImportError, AttributeError, OSError) as e:
        return "Windows didn't take the key (%s)." % e.__class__.__name__
    return None


class WinSpotify:
    """Spotify on a Windows PC: the media keys only. It can't tell what's playing, so Play and Pause are one button."""

    CAN = ("spotify",)

    def __init__(self, running=lambda: tasklist("Spotify.exe"), key=media_key):
        self.is_running, self.key = running, key

    def state(self):
        if not self.is_running():
            return {"running": False, "error": "Spotify isn't open on this PC."}
        return {"running": True, "state": "unknown", "volume": None, "position": 0.0, "track": "", "artist": "", "album": "",
                "duration": 0.0, "id": ""}

    def press(self, what):
        if not self.is_running():
            return "Spotify isn't open on this PC."
        return self.key(VK[what])

    def playpause(self):
        return self.press("playpause")

    def next(self):
        return self.press("next")

    def previous(self):
        return self.press("previous")


PP_WINDOWS = [r"C:\Program Files\Renewed Vision\ProPresenter\ProPresenter.exe",
              r"C:\Program Files (x86)\Renewed Vision\ProPresenter\ProPresenter.exe"]


class WinProPresenter:
    CAN = ("propresenter",)

    def __init__(self, running=lambda: tasklist("ProPresenter.exe"), start=None, paths=PP_WINDOWS):
        self.is_running, self.paths = running, paths
        self.start = start or (lambda p: os.startfile(p))  # noqa: S606 (a fixed path, never from the network)

    def running(self):
        return self.is_running()

    def open(self):
        p = next((p for p in self.paths if os.path.exists(p)), None)
        if not p:
            return "ProPresenter isn't installed where Windows usually puts it (%s)." % self.paths[0]
        try:
            self.start(p)
        except OSError as e:
            return "Windows couldn't open ProPresenter (%s)." % e
        return None


class Nothing:
    """Neither a Mac nor a Windows PC: it answers, but can't control anything."""

    CAN = ()

    def state(self):
        return {"running": False, "error": NOT_MAC}

    def running(self):
        return False

    def open(self):
        return NOT_MAC


def for_this_computer():
    if OS == "mac":
        return Spotify(), ProPresenter()
    if OS == "windows":
        return WinSpotify(), WinProPresenter()
    return Nothing(), Nothing()


class Helper:
    def __init__(self, spotify=None, propresenter=None, sleep=time.sleep):
        here = for_this_computer() if spotify is None or propresenter is None else (None, None)
        self.spotify = spotify or here[0]
        self.pp = propresenter or here[1]
        self.can = list(getattr(self.spotify, "CAN", ())) + list(getattr(self.pp, "CAN", ()))
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
        if "spotify" not in self.can:
            return NOT_MAC
        if d in ("playpause", "next", "previous") or (d in ("play", "pause") and "spotify_more" in self.can):
            self.cancel_fade()
            return getattr(self.spotify, d)()
        if d in ("play", "pause", "volume", "playlist", "fade") and "spotify_more" not in self.can:
            return NO_WIN
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


def my_networks():
    """The private networks this computer is on (each address's /24), and itself. The booth network, in practice."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; it only picks the address this computer would use
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(a for a in socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    nets = {"127.0.0.0/8"}
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local:
            nets.add(str(ipaddress.ip_network(ip + "/24", strict=False)))
    return sorted(nets)


def make_handler(cfg, helper):
    allowed = cfg.get("allowed_networks")
    nets = [ipaddress.ip_network(n, strict=False) for n in (allowed if allowed is not None else PRIVATE)]

    def hello():
        return {"service": "c3life-helper", "id": cfg.get("id", ""), "name": cfg.get("name") or socket.gethostname(),
                "os": cfg.get("_os") or OS, "version": VERSION, "can": helper.can,
                "apps": [a for a, c in (("Spotify", "spotify"), ("ProPresenter", "propresenter")) if c in helper.can]}

    class H(BaseHTTPRequestHandler):
        server_version = "C3LifeHelper/%d" % VERSION

        def log_message(self, fmt, *a):
            if cfg.get("_quiet"):
                return
            if sys.stderr:  # none when it runs hidden (pythonw on Windows)
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
                self.send_json(200, hello())
                return
            if not self.keyed():
                return
            if self.path == "/status":
                self.send_json(200, dict(hello(), ok=True))
            elif self.path == "/spotify":
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
    ap = argparse.ArgumentParser(description="C3 Life helper (Spotify and ProPresenter on this Mac or Windows PC)")
    ap.add_argument("--config", default=os.path.join(HERE, "helper.json"))
    ap.add_argument("--new-key", action="store_true", help="make a new key (the old one stops working)")
    ap.add_argument("--show-key", action="store_true", help="show the key (making one the first time), then stop")
    a = ap.parse_args(argv)
    cfg = load(a.config)
    if not cfg.get("key") or a.new_key or not cfg.get("id"):
        if not cfg.get("key") or a.new_key:
            cfg["key"] = secrets.token_urlsafe(24)
        cfg["id"] = cfg.get("id") or secrets.token_hex(8)  # this helper, wherever the Mac's address moves
        save(a.config, cfg)
    if a.show_key:
        print(cfg["key"])
        return
    if OS == "other":
        print(NOT_MAC + " It will answer, but can't control anything here.")
    elif OS == "windows":
        print("Windows: Spotify's Play/Pause, Next and Previous, and opening ProPresenter. (" + NO_WIN + ")")
    print("Key for the C3 Life server: %s" % cfg["key"])
    print("An Admin types it once on the C3 Life server: Show > Music > Computers with the C3 Life helper. The server finds this computer by itself.")
    print("(Kept in %s.)" % a.config)
    httpd = serve(cfg)
    allowed = cfg.get("allowed_networks")
    print("C3 Life helper on port %d, answering %s (this computer is on %s). Stop with Ctrl+C." % (
        cfg["port"], ", ".join(allowed) if allowed is not None else "private networks only", ", ".join(n for n in my_networks() if not n.startswith("127."))))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
