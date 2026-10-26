"""Monitor mixes: which Wing buses are monitor mixes, whose mix is whose, and the lock.

Kept here, not on each phone, so the C3 Life server can check that a musician only changes the mix they may:
their own bus, plus any extra ones FOH has given them. FOH sound and Admins may change every monitor mix.
When FOH locks monitor mixes (for a quiet moment), musicians can see their mix but not change it.

data/monitors.json: {"buses": [1, 2, 4], "people": {"Keys (musician)": {"own": 4, "extra": [1, 2], "me": 5}}, "locked": false}
("me" is the person's own channel on the Wing, for Me and More me in My mix; None until FOH chooses it.)

The check: for My mix to work, each monitor bus needs every send Pre (so FOH faders don't change it) and pan
link off (so musicians can pan for themselves). check() lists what isn't; the app fixes it only after Yes.
"""

import json
import os
import threading

DEFAULT = {"buses": [1, 2, 4], "people": {"Keys (musician)": {"own": 4, "extra": [1, 2]}}, "locked": False}


def valid(d, bus_count=16):
    if not isinstance(d, dict):
        return "Monitor mixes not understood."
    b = d.get("buses")
    if not isinstance(b, list) or not all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= bus_count for x in b) or len(set(b)) != len(b):
        return "Monitor mixes are buses 1 to %d." % bus_count
    p = d.get("people")
    if not isinstance(p, dict) or len(p) > 100:
        return "People not understood."
    for name, v in p.items():
        if not isinstance(name, str) or not isinstance(v, dict):
            return "People not understood."
        if v.get("own") is not None and v.get("own") not in b:
            return "%s's own mix must be one of the monitor mixes." % name
        if not isinstance(v.get("extra", []), list) or not all(x in b for x in v.get("extra", [])):
            return "%s's extra mixes must be monitor mixes." % name
        me = v.get("me")
        if me is not None and (not isinstance(me, int) or isinstance(me, bool) or not 1 <= me <= 40):
            return "%s's own channel is a Wing channel, 1 to 40." % name
    if not isinstance(d.get("locked", False), bool):
        return "Locked is yes or no."
    return None


class Monitors:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "monitors.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.d = d if not valid(d) else json.loads(json.dumps(DEFAULT))
        except (OSError, ValueError):
            self.d = json.loads(json.dumps(DEFAULT))

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.d))

    def save(self, d):
        with self.lock:
            self.d = {"buses": d["buses"], "people": {k: {"own": v.get("own"), "extra": list(v.get("extra", [])), "me": v.get("me")} for k, v in d["people"].items()},
                      "locked": bool(d.get("locked", False))}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, indent=1)
            os.replace(tmp, self.path)

    def set_locked(self, locked):
        d = self.get()
        d["locked"] = bool(locked)
        self.save(d)

    def mine(self, name):
        """The mixes this person may open: their own first."""
        with self.lock:
            p = self.d["people"].get(name) or {}
            own = p.get("own")
            return ([own] if own else []) + [b for b in p.get("extra", []) if b != own]

    def me(self, name):
        """This person's own channel on the Wing (Me in My mix), or None."""
        with self.lock:
            return (self.d["people"].get(name) or {}).get("me")

    def may_change(self, name, role_level_mixer, bus):
        """Mixer control (FOH, Admin): any monitor mix. Otherwise only your own mixes, and not while locked."""
        with self.lock:
            if bus not in self.d["buses"]:
                return "Bus %d isn't a monitor mix." % bus
            if role_level_mixer >= 2:
                return None
            if self.d["locked"]:
                return "The sound desk has locked monitor mixes for now."
        if bus not in self.mine(name):
            return "That isn't one of your mixes."
        return None

    def check(self, sends):
        """sends: bus -> channel -> {mode, plink, ...}. -> [{"bus", "post": [channels], "linked": [channels]}]"""
        out = []
        for b in self.get()["buses"]:
            s = sends.get(b, {})
            post = sorted(n for n, v in s.items() if v.get("mode") not in (None, "PRE"))
            linked = sorted(n for n, v in s.items() if v.get("plink"))
            out.append({"bus": b, "post": post, "linked": linked, "read": len(s)})
        return out
