"""What's connected: every device C3 Life is set up to talk to, and whether it's answering. Every screen shows a warning
when one isn't (player.html asks GET /api/devices every 15 seconds).

Only what's set up counts: a Wing turned Off on the Mixer screen, or no Tio address, isn't a warning. A blip isn't
either: something has to be quiet for GRACE seconds first. ProPresenter is asked for its version once a minute (from the
scan's watch, scan.py), the helpers by helpers.check(); the Wing and the Tio say themselves; Lightkey (which doesn't
answer OSC) counts as there if the last scan saw its Bonjour announcement.

Each device names the access area that sees it, so a musician hears about the Wing (their monitor mix) but not about
ProPresenter, and where to go to look.
"""

import threading
import time
import urllib.request

GRACE = 15  # seconds quiet before it's a warning


class Health:
    def __init__(self, cfg, desk=None, tio=None, helpers=None, now=time.time, lights=None):
        self.cfg, self.desk, self.tio, self.helpers, self.now, self.lights = cfg, desk, tio, helpers, now, lights
        self.lock = threading.Lock()
        self.pp = None    # True / False / None (not asked yet)
        self.quiet = {}   # id -> since when it's been quiet

    # ---- asked once a minute, from the scan's watch ----
    def check(self):
        """Asks ProPresenter and the helpers. -> what's stopped answering, in words (for the watch)"""
        lost = []
        url = (self.cfg.get("propresenter") or "").rstrip("/")
        if url:
            try:
                with urllib.request.urlopen(url + "/version", timeout=2) as r:
                    ok = r.status == 200
            except OSError:
                ok = False
            with self.lock:
                self.pp = ok
            if not ok:
                lost.append("ProPresenter")
        if self.helpers:
            lost += self.helpers.check()
        return lost

    # ---- what phones see ----
    def devices(self):
        """[{"id", "name", "ok", "area", "where", "why", "since"}] for everything that's set up"""
        out = []
        for d, did, area, where in ((self.desk, "desk", "Mixer", "Wing.dc.html"), (self.tio, "tio", "Mixer", "Dante.dc.html")):
            if not d:
                continue
            st = d.status()
            if st["status"] in ("off", "no address"):
                continue
            name = "The Tio" if did == "tio" else "The " + getattr(d, "NAME", "sound desk").replace("Behringer ", "")
            out.append({"id": did, "name": name,
                        "ok": st["status"] == "connected", "area": area, "where": where,
                        "why": "No answer at %s. Is it on, and on the booth network?" % st["ip"]})
        if self.cfg.get("propresenter"):
            with self.lock:
                ok = self.pp
            if ok is not None:
                out.append({"id": "propresenter", "name": "ProPresenter", "ok": ok, "area": "Show", "where": "Show.dc.html",
                            "why": "No answer at %s. Is ProPresenter open, with Network turned on?" % self.cfg["propresenter"].split("//")[-1]})
        lk = self.lights
        if lk and lk.settings.get("ip") and lk.seen is not None:  # Lightkey: whether the last scan saw it (it doesn't answer OSC)
            out.append({"id": "lightkey", "name": "Lightkey", "ok": lk.seen, "area": "Lights", "where": "Lights.dc.html",
                        "why": "The last network scan didn't see it at %s. Is the lighting Mac on, with Lightkey open?" % lk.settings["ip"]})
        for h in (self.helpers.view() if self.helpers else []):
            if h["paired"] and h["ok"] is not None:
                out.append({"id": "helper:" + h["id"], "name": "The C3 Life helper on " + (h["name"] or h["address"]), "ok": h["ok"],
                            "area": "Music", "where": "Music.dc.html", "down": h.get("down_since") or 0,
                            "why": "Is that computer on, and logged in? The helper starts when someone logs in."})
        t = self.now()
        with self.lock:
            for d in out:
                if d["ok"]:
                    self.quiet.pop(d["id"], None)
                else:
                    self.quiet.setdefault(d["id"], d.pop("down", 0) or t)  # a helper knows when it stopped answering
                d.pop("down", None)
                d["since"] = self.quiet.get(d["id"], 0)
                d["warn"] = (not d["ok"]) and t - d["since"] >= GRACE
        return out
