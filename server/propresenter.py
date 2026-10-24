"""ProPresenter, live: what's on screen now, what's next, the slide pictures, Next / Previous / go to a slide / clear,
and ProPresenter's own timers. Everything goes through ProPresenter's network API (ProPresenter > Settings > Network
> Enable Network), at the address in "propresenter" (the network scan finds it and follows it).

  GET /v1/presentation/slide_index        which presentation and slide is up
  GET /v1/presentation/<uuid>             its groups and slides (names, labels, text), kept for a minute
  GET /v1/status/slide                    the current and next slide's text
  GET /v1/presentation/<uuid>/thumbnail/<i>?quality=<px>   a slide's picture (JPEG), kept for a few minutes
  GET /v1/trigger/next | previous, /v1/presentation/<uuid>/<i>/trigger, /v1/clear/layer/<layer>
  GET /v1/timers/current, /v1/timers; GET /v1/timer/<uuid>/start|stop|reset; PUT /v1/timer/<uuid> (a new duration)

**Not tried with our ProPresenter yet**: the timer changes (+/- and Set) follow ProPresenter's API documentation.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LAYERS = ("slide", "media", "props", "messages", "announcements", "audio", "video_input")


def _clock(secs):
    secs = int(round(secs))
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    return "%s%d:%02d" % (sign, secs // 60, secs % 60) if secs < 3600 else "%s%d:%02d:%02d" % (sign, secs // 3600, secs // 60 % 60, secs % 60)


def parse_time(t):
    """ProPresenter's "00:05:30" (or "-00:00:12") -> seconds."""
    if isinstance(t, (int, float)):
        return float(t)
    t = str(t or "").strip()
    neg = t.startswith("-")
    parts = t.lstrip("-").split(":")
    try:
        v = 0.0
        for p in parts:
            v = v * 60 + float(p)
    except ValueError:
        return 0.0
    return -v if neg else v


class ProPresenter:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.pres = {}            # uuid -> (time, {"name", "slides": [{"group", "label", "text"}]})
        self.thumbs = {}          # (uuid, index, size) -> (time, bytes)
        self.cache = (0.0, None)  # the last status, for a second, so many phones don't each ask ProPresenter

    # ---- talking to ProPresenter ----
    def url(self):
        u = (self.cfg.get("propresenter") or "").rstrip("/")
        if not u:
            raise OSError("ProPresenter hasn't been found on the network yet. Open it with Settings › Network › Enable Network turned on; the network scan finds it.")
        return u

    def _req(self, path, method="GET", body=None, raw=False, timeout=3):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url() + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
        if raw:
            return out
        return json.loads(out) if out.strip() else None

    @staticmethod
    def _why(e):
        if isinstance(e, urllib.error.HTTPError):
            return "ProPresenter said no (%s)." % e.code
        return "ProPresenter didn't answer (%s)." % (getattr(e, "reason", None) or e)

    def presentation(self, uuid):
        t, p = self.pres.get(uuid, (0, None))
        if p and time.time() - t < 60:
            return p
        d = (self._req("/v1/presentation/" + urllib.parse.quote(uuid)) or {}).get("presentation") or {}
        slides = []
        for g in d.get("groups") or []:
            for s in g.get("slides") or []:
                slides.append({"group": g.get("name") or "", "label": s.get("label") or "", "text": (s.get("text") or "").strip()})
        p = {"name": (d.get("id") or {}).get("name") or "", "slides": slides}
        self.pres[uuid] = (time.time(), p)
        return p

    # ---- what's up ----
    def status(self):
        t, c = self.cache
        if c and time.time() - t < 1.0:
            return c
        out = {"connected": False, "error": "", "presentation": "", "uuid": "", "index": None, "count": 0, "current": None, "next": None, "after": None}
        try:
            si = (self._req("/v1/presentation/slide_index") or {}).get("presentation_index") or {}
            pid = si.get("presentation_id") or {}
            out["connected"] = True
            if pid.get("uuid") and isinstance(si.get("index"), int):
                p = self.presentation(pid["uuid"])
                i = si["index"]
                out.update(presentation=p["name"] or pid.get("name") or "", uuid=pid["uuid"], index=i, count=len(p["slides"]))
                for k, j in (("current", i), ("next", i + 1), ("after", i + 2)):
                    if 0 <= j < len(p["slides"]):
                        out[k] = dict(p["slides"][j], index=j)
                try:  # the text as ProPresenter shows it now (it may have been edited since)
                    st = self._req("/v1/status/slide") or {}
                    if out["current"] is not None and (st.get("current") or {}).get("text") is not None:
                        out["current"]["text"] = (st["current"]["text"] or "").strip()
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            out["error"] = self._why(e)
        self.cache = (time.time(), out)
        return out

    def thumb(self, uuid, index, size=320):
        key = (uuid, index, size)
        t, b = self.thumbs.get(key, (0, None))
        if b and time.time() - t < 300:
            return b
        b = self._req("/v1/presentation/%s/thumbnail/%d?quality=%d" % (urllib.parse.quote(uuid), index, size), raw=True, timeout=4)
        with self.lock:
            if len(self.thumbs) > 300:
                self.thumbs.clear()
            self.thumbs[key] = (time.time(), b)
        return b

    # ---- changing what's up (Show control) ----
    def trigger(self, do, index=None, layer=None, who=""):
        try:
            if do in ("next", "previous"):
                self._req("/v1/trigger/" + do)
            elif do == "go":
                st = self.status()
                if not st["uuid"]:
                    return "Nothing is up in ProPresenter to move within."
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < max(st["count"], 1):
                    return "There's no slide %s." % index
                self._req("/v1/presentation/%s/%d/trigger" % (urllib.parse.quote(st["uuid"]), index))
            elif do == "clear":
                layers = LAYERS if layer == "all" else (layer,)
                if layer != "all" and layer not in LAYERS:
                    return "That can't be cleared."
                for x in layers:
                    self._req("/v1/clear/layer/" + x)
            else:
                return "Bad request"
        except (OSError, ValueError) as e:
            return self._why(e)
        self.cache = (0.0, None)
        if self.log:
            self.log.info("propresenter: %s %s%s", who, do, " " + str(index if do == "go" else layer) if do in ("go", "clear") else "")
        return None

    # ---- ProPresenter's timers ----
    def timers(self):
        """[{"id", "name", "kind", "secs", "time", "state", "running"}], or raises OSError/ValueError."""
        cur = self._req("/v1/timers/current") or []
        conf = {}
        try:
            for t in self._req("/v1/timers") or []:
                conf[(t.get("id") or {}).get("uuid")] = t
        except (OSError, ValueError):
            pass
        out = []
        for t in cur:
            tid = (t.get("id") or {}).get("uuid")
            if not tid:
                continue
            c = conf.get(tid) or {}
            kind = "Countdown" if "countdown" in c else ("Count down to a time" if "count_down_to_time" in c else ("Elapsed" if "elapsed" in c else "Timer"))
            secs = parse_time(t.get("time"))
            state = str(t.get("state") or "")
            out.append({"id": tid, "name": (t.get("id") or {}).get("name") or "Timer", "kind": kind, "secs": secs, "time": _clock(secs),
                        "state": state, "running": state.lower() in ("running", "overrunning", "overran")})
        return out

    def timer(self, tid, do, secs=0, who=""):
        if not isinstance(tid, str) or not tid or "/" in tid:
            return "Which timer?"
        q = urllib.parse.quote(tid)
        try:
            if do in ("start", "stop", "reset"):
                self._req("/v1/timer/%s/%s" % (q, do))
            elif do in ("add", "set"):
                if not isinstance(secs, (int, float)) or isinstance(secs, bool) or abs(secs) > 24 * 3600:
                    return "Bad request"
                cur = next((t for t in self.timers() if t["id"] == tid), None)
                if not cur:
                    return "That timer isn't in ProPresenter any more."
                conf = self._req("/v1/timer/" + q) or {}
                if "countdown" not in conf:
                    return "Only a countdown timer can be changed from here; change this one in ProPresenter."
                new = max(0, int(round(cur["secs"] + secs if do == "add" else secs)))
                conf["countdown"] = dict(conf.get("countdown") or {}, duration=new)
                self._req("/v1/timer/" + q, method="PUT", body=conf)
                if cur["running"]:
                    self._req("/v1/timer/%s/start" % q)
            else:
                return "Bad request"
        except (OSError, ValueError) as e:
            return self._why(e)
        if self.log:
            self.log.info("propresenter: %s timer %s %s %s", who, tid, do, secs if do in ("add", "set") else "")
        return None
