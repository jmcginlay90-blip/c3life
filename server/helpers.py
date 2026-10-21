"""The C3 Life helpers: the small program (helper/c3life_helper.py) on each Mac or Windows PC in the booth.

Every helper the network scan finds is kept here by its own id (from GET /hello), so it's followed when its computer's
address changes. Nobody types an address. An Admin types each helper's key once (it's shown when the helper starts);
the key stays on the C3 Life server and is never sent to phones. A helper does nothing until someone presses a button.

What each one can do comes from the helper ("can"): a Mac does all of Spotify and opens ProPresenter; a Windows PC does
Spotify's Play/Pause, Next and Previous, and opens ProPresenter.

data/helpers.json: {"helpers": {id: {"id", "name", "url", "os", "can", "key", "seen"}}}

A helper set up by hand in config.json ("helper", "helper_key") is kept too, until a scan finds it and learns its id.

Through the day: check() asks every helper that has a key (with the key) once a minute, from the scan's watch (scan.py).
One that stops answering makes the scan run straight away, so a helper whose computer got a new address is followed
within about a minute. Each one's last answer shows on Music.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

FILE = "helpers.json"
CONFIG_ID = "config"  # the one from config.json, until its own id is known
NOT_PAIRED = "This computer's helper key isn't set. An Admin types it once on Music (it's shown when the helper starts)."


def hello(url, timeout=2):
    """-> the helper's /hello, or None"""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/hello", timeout=timeout) as r:
            j = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return j if isinstance(j, dict) and j.get("service") == "c3life-helper" else None


def ask(url, key, path, body=None, timeout=3):
    """-> (ok, json or what's wrong in words)"""
    if not key:
        return False, NOT_PAIRED
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST" if data is not None else "GET",
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
        return False, "The helper at %s didn't answer. Is that computer on, and the helper running?" % url


def _clean(s, n=60):
    return "".join(c for c in str(s or "") if c >= " ")[:n]


class Helpers:
    def __init__(self, data_dir, cfg, log):
        self.path = os.path.join(data_dir, FILE)
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.d = {}
        self.health = {}  # id -> {"ok": bool, "at": when it last answered}; not saved
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f).get("helpers", {})
            if isinstance(d, dict):
                self.d = {k: v for k, v in d.items() if isinstance(v, dict) and v.get("url")}
        except (OSError, ValueError, AttributeError):
            pass
        url, key = (cfg.get("helper") or "").rstrip("/"), cfg.get("helper_key") or ""
        if url and not any(h["url"] == url for h in self.d.values()):
            self.d[CONFIG_ID] = {"id": CONFIG_ID, "name": "", "url": url, "os": "", "can": ["spotify", "spotify_more", "propresenter"],
                                 "key": key, "seen": 0}

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"helpers": self.d}, f, indent=1)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---- what the network scan found ----
    def found(self, finds, hellos=None):
        """The scan's finds (kind "helper"). Adds new helpers, and follows known ones to a new address. -> changes in words"""
        hellos = hellos or {}
        changes = []
        for f in finds:
            if f.get("kind") != "helper":
                continue
            url = "http://%s:%d" % (f["ip"], f["port"])
            j = hellos.get(url) or hello(url)
            if not j or not j.get("id"):
                continue  # not a helper after all, or one from before helpers had ids (restarting it makes one)
            hid = _clean(j["id"], 40)
            with self.lock:
                h = self.d.get(hid)
                old = self.d.get(CONFIG_ID)
                if not h and old and old["url"] == url:  # the one in config.json: now we know its id
                    h = self.d.pop(CONFIG_ID)
                    h["id"] = hid
                    self.d[hid] = h
                if not h:
                    h = self.d[hid] = {"id": hid, "url": url, "key": "", "seen": 0}
                    changes.append("Found a C3 Life helper on %s (%s) at %s. An Admin types its key on Music." % (_clean(j.get("name")) or "a computer", j.get("os") or "?", f["ip"]))
                elif h["url"] != url:
                    changes.append("The C3 Life helper on %s moved from %s to %s, so C3 Life followed it" % (_clean(j.get("name")) or h.get("name") or "a computer", h["url"].split("//")[-1].split(":")[0], f["ip"]))
                h.update(url=url, name=_clean(j.get("name")), os=_clean(j.get("os"), 10),
                         can=[_clean(c, 20) for c in j.get("can", [])][:10] if isinstance(j.get("can"), list) else [], seen=time.time())
                self._write()
        return changes

    # ---- keys (Admin) ----
    def pair(self, hid, key, who):
        """Checks the key with the helper, then keeps it. -> None or what's wrong"""
        key = (key or "").strip()
        if not 8 <= len(key) <= 100 or any(c <= " " for c in key):
            return "That doesn't look like a helper key. Copy it from the helper's window on that computer."
        with self.lock:
            h = self.d.get(hid)
            url = h and h["url"]
        if not h:
            return "That helper isn't on the list any more. Scan again."
        ok, j = ask(url, key, "/status")
        if not ok:
            return j
        with self.lock:
            h = self.d.get(hid)
            if not h:
                return "That helper isn't on the list any more. Scan again."
            h["key"] = key
            if j.get("can") is not None:
                h["can"] = [_clean(c, 20) for c in j.get("can", [])][:10]
            self._write()
        self.log.info("helpers: %s set the key for the helper on %s (%s)", who, h.get("name") or "a computer", url)
        return None

    def forget(self, hid, who):
        with self.lock:
            h = self.d.pop(hid, None)
            if h:
                self._write()
        if not h:
            return "That helper isn't on the list."
        self.log.info("helpers: %s forgot the helper on %s (%s)", who, h.get("name") or "a computer", h["url"])
        return None

    # ---- using them ----
    def get(self, hid):
        with self.lock:
            h = self.d.get(hid)
            return dict(h) if h else None

    def pick(self, need, prefer=""):
        """The helper to use for something ("spotify", "propresenter"): `prefer` if it can, else the only one that can."""
        with self.lock:
            hs = [dict(h) for h in self.d.values() if need in (h.get("can") or [])]
        p = next((h for h in hs if h["id"] == prefer), None)
        if p:
            return p
        paired = [h for h in hs if h.get("key")]
        return (paired or hs)[0] if len(paired or hs) == 1 else None

    def at_host(self, host, need):
        """The helper on the computer with this address (where ProPresenter is set up, say)."""
        with self.lock:
            return next((dict(h) for h in self.d.values() if need in (h.get("can") or []) and urllib.parse.urlparse(h["url"]).hostname == host), None)

    def ask(self, h, path, body=None, timeout=3):
        if not h:
            return False, "No C3 Life helper found yet. Start it on the computer with Spotify; the network scan finds it within 5 minutes (or tap Find it now)."
        ok, j = ask(h["url"], h.get("key"), path, body, timeout)
        if h.get("key"):
            self._heard(h["id"], ok)
        return ok, j

    def _heard(self, hid, ok):
        with self.lock:
            was = self.health.get(hid, {})
            t = time.time()
            self.health[hid] = {"ok": ok, "at": t if ok else was.get("at", 0),
                                "down": 0 if ok else (was.get("down") or t)}  # when it stopped answering

    def check(self):
        """Asks every helper with a key whether it's there. -> the names of the ones that didn't answer"""
        with self.lock:
            hs = [dict(h) for h in self.d.values() if h.get("key")]
        lost = []
        for h in hs:
            ok, _ = ask(h["url"], h["key"], "/status", timeout=2)
            self._heard(h["id"], ok)
            if not ok:
                lost.append("the C3 Life helper on %s" % (h.get("name") or h["url"]))
        return lost

    def view(self):
        """For phones: never the keys."""
        with self.lock:
            hs = [{"id": h["id"], "name": h.get("name") or "", "url": h["url"], "address": h["url"].split("//")[-1],
                   "os": h.get("os") or "", "can": h.get("can") or [], "paired": bool(h.get("key")), "seen": h.get("seen", 0),
                   "ok": self.health.get(h["id"], {}).get("ok"), "last_ok": self.health.get(h["id"], {}).get("at", 0),
                   "down_since": self.health.get(h["id"], {}).get("down", 0)}
                  for h in self.d.values()]
        return sorted(hs, key=lambda h: (h["name"].lower(), h["url"]))
