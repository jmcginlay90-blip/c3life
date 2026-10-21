"""Planning Center: signing in and out, this Sunday's times as timers, and local time.

Signing in uses a Personal Access Token (api.planningcenteronline.com > Personal Access Tokens): an Admin
types its two halves in the app, the C3 Life server checks them with Planning Center, and keeps them in
data\\pco.json (never sent back to phones). Signing out forgets them, so another account can be used.

Timers: the plan's service times count down, and each item's length becomes a timer that can be started,
stopped and nudged like ProPresenter's. Changing an item's length is sent to Planning Center (the app
asks first). Timer state lives here so every device shows the same.

Time: Planning Center gives times in UTC. They're shown in the C3 Life server's own time zone (the
booth network's), whatever region the Planning Center account is set to; if that differs, it says so.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PCO = "https://api.planningcenteronline.com"


def local_tz():
    now = datetime.now().astimezone()
    off = now.utcoffset().total_seconds() / 3600
    name = now.tzname() or ""
    return {"name": name, "offset": ("UTC%+g" % off).replace("+0", "") if off else "UTC"}


def to_local(iso):
    """'2026-10-18T09:00:00Z' -> datetime in the C3 Life server's time zone (None if not a time)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def hhmm(dt):
    return dt.strftime("%H:%M") if dt else ""


class Planning:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.file = os.path.join(data_dir, "pco.json")
        self.tfile = os.path.join(data_dir, "pco-timers.json")
        self.lock = threading.Lock()
        self.saved = self._load(self.file)
        self.timers = self._load(self.tfile)
        if self.saved.get("app_id") and self.saved.get("secret"):
            cfg["pco_app_id"], cfg["pco_secret"] = self.saved["app_id"], self.saved["secret"]  # name tags use these too
        elif self.saved.get("signed_out"):
            cfg["pco_app_id"], cfg["pco_secret"] = "", ""
        if self.saved.get("service_type"):
            cfg["pco_service_type"] = self.saved["service_type"]

    @staticmethod
    def _load(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, path, d):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, path)

    # ---- talking to Planning Center ----
    def call(self, path, method="GET", body=None, app_id=None, secret=None):
        base = self.cfg.get("pco_base") or PCO
        a, s = app_id or self.cfg.get("pco_app_id"), secret or self.cfg.get("pco_secret")
        auth = base64.b64encode(("%s:%s" % (a, s)).encode()).decode()
        req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Basic " + auth, "Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else {}

    def signed_in(self):
        return bool(self.cfg.get("pco_app_id") and self.cfg.get("pco_secret"))

    def status(self):
        d = {"signed_in": self.signed_in(), "who": self.saved.get("who", ""), "org": self.saved.get("org", ""), "org_tz": self.saved.get("org_tz", ""),
             "since": self.saved.get("at", ""), "service_type": self.cfg.get("pco_service_type") or "Sunday Morning", "local_tz": local_tz(),
             "from_installer": self.signed_in() and not self.saved.get("app_id")}
        d["tz_differs"] = bool(d["org_tz"]) and d["org_tz"] != self.server_tz_name()
        return d

    @staticmethod
    def server_tz_name():
        # the IANA name when the system has one (TZ, /etc/localtime); otherwise the short name
        tz = os.environ.get("TZ", "")
        if tz:
            return tz
        try:
            link = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in link:
                return link.split("zoneinfo/", 1)[1]
        except OSError:
            pass
        return datetime.now().astimezone().tzname() or ""

    def sign_in(self, app_id, secret):
        """Check the token with Planning Center first; only a working one is kept."""
        app_id, secret = app_id.strip(), secret.strip()
        try:
            me = self.call("/people/v2/me", app_id=app_id, secret=secret)
        except urllib.error.HTTPError as e:
            return "Planning Center didn’t accept that token." if e.code in (401, 403) else "Planning Center said no (%d)." % e.code
        except (OSError, ValueError) as e:
            return "Couldn’t reach Planning Center (%s). Check the internet." % e.__class__.__name__
        org, org_tz = "", ""
        try:
            o = (self.call("/services/v2", app_id=app_id, secret=secret).get("data") or {}).get("attributes") or {}
            org, org_tz = o.get("name", ""), o.get("time_zone", "")
        except (OSError, ValueError):
            pass
        who = ((me.get("data") or {}).get("attributes") or {}).get("name", "")
        with self.lock:
            self.saved = {"app_id": app_id, "secret": secret, "who": who, "org": org, "org_tz": org_tz, "at": time.strftime("%Y-%m-%d %H:%M"),
                          "service_type": self.saved.get("service_type", "")}
            self._save(self.file, self.saved)
        self.cfg["pco_app_id"], self.cfg["pco_secret"] = app_id, secret
        self.forget()
        return ""

    def sign_out(self):
        with self.lock:
            self.saved = {"signed_out": True, "service_type": self.saved.get("service_type", "")}
            self._save(self.file, self.saved)
        self.cfg["pco_app_id"], self.cfg["pco_secret"] = "", ""
        self.forget()

    def service_types(self):
        return [t["attributes"]["name"] for t in (self.call("/services/v2/service_types?per_page=100").get("data") or [])]

    def set_service_type(self, name):
        if name not in self.service_types():
            return "No service type called %s in Planning Center." % name
        with self.lock:
            self.saved["service_type"] = name
            self._save(self.file, self.saved)
        self.cfg["pco_service_type"] = name
        return ""

    # ---- this Sunday's timers ----
    def plan(self):
        # read from Planning Center at most every 30 seconds; every phone polls the timers
        c = getattr(self, "_cache", None)
        if c and time.time() - c[0] < 30 and c[1] == (self.cfg.get("pco_service_type"), self.cfg.get("pco_app_id")):
            return c[2]
        v = self._plan()
        self._cache = (time.time(), (self.cfg.get("pco_service_type"), self.cfg.get("pco_app_id")), v)
        return v

    def forget(self):
        self._cache = None

    def _plan(self):
        name = self.cfg.get("pco_service_type") or "Sunday Morning"
        st = next((t for t in self.call("/services/v2/service_types?per_page=100").get("data") or [] if t["attributes"].get("name") == name), None)
        if not st:
            raise LookupError("No service type called %s in Planning Center." % name)
        today = time.strftime("%Y-%m-%d")
        past = self.call("/services/v2/service_types/%s/plans?filter=past&order=-sort_date&per_page=1" % st["id"]).get("data") or []
        plan = past[0] if past and str(to_local(past[0]["attributes"].get("sort_date")) or "")[:10] == today else None
        if not plan:
            fut = self.call("/services/v2/service_types/%s/plans?filter=future&order=sort_date&per_page=1" % st["id"]).get("data") or []
            plan = fut[0] if fut else (past[0] if past else None)
        if not plan:
            raise LookupError("No plans in %s." % name)
        times = self.call("/services/v2/service_types/%s/plans/%s/plan_times" % (st["id"], plan["id"])).get("data") or []
        items = self.call("/services/v2/service_types/%s/plans/%s/items?per_page=100" % (st["id"], plan["id"])).get("data") or []
        return st, plan, times, items

    def timer_list(self):
        st, plan, times, items = self.plan()
        now = time.time()
        out = []
        for t in sorted(times, key=lambda x: x["attributes"].get("starts_at") or ""):
            a = t["attributes"]
            dt = to_local(a.get("starts_at"))
            if not dt or a.get("time_type") not in ("service", "rehearsal"):
                continue
            out.append({"id": "time-" + t["id"], "kind": "countdown", "name": (a.get("name") or ("Service" if a.get("time_type") == "service" else "Rehearsal")) + " starts",
                        "at": hhmm(dt), "date": dt.strftime("%a %d %b"), "left": int(dt.timestamp() - now)})
        for it in sorted(items, key=lambda x: x["attributes"].get("sequence") or 0):
            a = it["attributes"]
            if a.get("item_type") == "header" or not a.get("length"):
                continue
            tid = "item-" + it["id"]
            state = self.timers.get(tid) or {}
            run = state.get("run", False)
            left = state.get("left", a["length"])
            if run:
                left -= int(now - state.get("stamp", now))
            out.append({"id": tid, "kind": "item", "item_id": it["id"], "name": a.get("title", ""), "length": a["length"], "left": left, "run": run})
        # Now and Up next: the item someone moved to with Next (none until then), and the one after it
        items_only = [t for t in out if t["kind"] == "item"]
        ids = [t["id"] for t in items_only]
        cur = self.timers.get("_current") if self.timers.get("_current") in ids else ""
        nxt = ids[ids.index(cur) + 1] if cur and ids.index(cur) + 1 < len(ids) else ("" if cur else (ids[0] if ids else ""))
        if cur:  # roughly when the current item ends and the next one starts
            t = next(x for x in items_only if x["id"] == cur)
            ends = now + max(0, t["left"])
            t["ends"] = hhmm(datetime.fromtimestamp(ends).astimezone())
            if nxt:
                next(x for x in items_only if x["id"] == nxt)["starts"] = t["ends"]
        a = plan["attributes"]
        return {"plan": {"id": plan["id"], "service_type_id": st["id"], "dates": a.get("dates", ""), "service_type": st["attributes"]["name"]}, "timers": out,
                "current": cur, "next": nxt, "local_tz": local_tz()}

    def move(self, action, tid=""):
        """Next, Previous, or straight to one item: the old one stops where it is, the new one starts.
        Only ever when someone taps; nothing moves on by itself."""
        d = self.timer_list()
        items = [t for t in d["timers"] if t["kind"] == "item"]
        ids = [t["id"] for t in items]
        cur = d["current"]
        if action == "next":
            to = d["next"]
            if not to:
                return "That was the last item."
        elif action == "prev":
            if not cur or ids.index(cur) == 0:
                return "That’s the first item."
            to = ids[ids.index(cur) - 1]
        elif action == "go":
            if tid not in ids:
                return "That timer isn’t in this week’s plan."
            to = tid
        elif action == "clear":
            to = ""
        else:
            return "Not understood."
        now = time.time()
        by = {t["id"]: t for t in items}
        with self.lock:
            if cur:  # the one we leave stops where it is
                self.timers[cur] = {"run": False, "left": by[cur]["left"], "stamp": now}
            if to:
                t = by[to]
                fresh = action == "next" or t["left"] == t["length"]
                self.timers[to] = {"run": True, "left": t["length"] if fresh else t["left"], "stamp": now}
            self.timers["_current"] = to
            self._save(self.tfile, self.timers)
        return ""

    def change(self, tid, action, secs=0):
        """Start, stop, reset or nudge one item timer (shared by every device)."""
        if action in ("next", "prev", "go", "clear"):
            return self.move(action, tid)
        tl = {t["id"]: t for t in self.timer_list()["timers"]}
        t = tl.get(tid)
        if not t or t["kind"] != "item":
            return "That timer isn’t in this week’s plan."
        now = time.time()
        left = t["left"]
        st = {"run": t["run"], "left": left, "stamp": now}
        if action == "start":
            st["run"] = True
        elif action == "stop":
            st["run"] = False
        elif action == "reset":
            st = {"run": False, "left": t["length"], "stamp": now}
        elif action == "add":
            st["left"] = left + int(secs)
        elif action == "set":
            st["left"] = int(secs)
        else:
            return "Not understood."
        with self.lock:
            self.timers[tid] = st
            self._save(self.tfile, self.timers)
        return ""

    def set_length(self, item_id, secs):
        """Change an item's length in Planning Center itself."""
        tl = self.timer_list()
        p = tl["plan"]
        if not any(t.get("item_id") == item_id for t in tl["timers"]):
            return "That item isn’t in this week’s plan."
        try:
            self.call("/services/v2/service_types/%s/plans/%s/items/%s" % (p["service_type_id"], p["id"], item_id), "PATCH",
                      {"data": {"type": "Item", "id": item_id, "attributes": {"length": int(secs)}}})
        except urllib.error.HTTPError as e:
            return "Planning Center wouldn’t change it (%d): this account may only be able to view plans." % e.code
        except (OSError, ValueError) as e:
            return "Couldn’t reach Planning Center (%s)." % e.__class__.__name__
        with self.lock:
            self.timers.pop("item-" + item_id, None)
            self._save(self.tfile, self.timers)
        self.forget()
        return ""
