"""Where the RTA's two signals come from: the room mic, and the desk reference (what the desk sends, to compare against).

Each one says two things:
  from: where the sound starts. The mic: any Wing input ("LCL/8", "A/3", "CRD/5", ...), a Tio1608-D input sent straight
        onto Dante ("TIO/1"), or another Dante device ("DANTE"). The reference: a Wing main, bus or matrix ("MAIN/1", "BUS/5").
  via + ch: how it reaches the C3 Life server: Dante Virtual Soundcard channel, AES67 channel, or Wing USB channel.
  stereo: two channels, left and right. The right one has its own from2 and ch2, so it can come from anywhere too.
        A main, bus or matrix is stereo on the desk: "MAIN/1" (or "MAIN/1/L") is its left side and "MAIN/1/R" its right.
  subs (the desk reference only): the subwoofer feed, added to the reference so the room is compared with mains and
        subs together. Always stereo: {"on": true, "from", "ch", "from2", "ch2"}, any main, bus or matrix, by the same
        way in as the reference.
Choosing these doesn't change anything on the desk or on Dante: it's which channel C3 Life listens to, and what it
expects to find there. The RTA screen shows the steps for the desk and Dante Controller, and the route Dante really has.

data/rta_inputs.json: {"mic": {"from": "LCL/8", "via": "dvs", "ch": 1}, "ref": {"from": "MAIN/1", "via": "dvs", "ch": 2, "stereo": true, "from2": "MAIN/1/R", "ch2": 3}}
"""

import json
import os
import threading

DEFAULT = {"mic": {"from": "LCL/8", "via": "dvs", "ch": 1}, "ref": {"from": "MAIN/1", "via": "dvs", "ch": 2}}
VIA = {"dvs": 64, "aes67": 8, "usb": 48}  # how many channels each way in has
MIC_FROM = {"LCL": 8, "AUX": 8, "A": 48, "B": 48, "C": 48, "CRD": 64, "USB": 48, "TIO": 16}
REF_FROM = {"MAIN": 4, "BUS": 16, "MTX": 8}


FIELDS = ("from", "via", "ch", "stereo", "from2", "ch2")


def _src_ok(v, groups):
    if v == "DANTE" and groups is MIC_FROM:
        return True
    if not isinstance(v, str) or "/" not in v:
        return False
    p = v.split("/")
    if len(p) == 3 and p[0] in REF_FROM and p[2] in ("L", "R"):  # a side of a stereo main, bus or matrix
        p = p[:2]
    if len(p) != 2:
        return False
    g, n = p
    return g in groups and n.isdigit() and 1 <= int(n) <= groups[g]


SUB_FIELDS = ("on", "from", "ch", "from2", "ch2")


def channels(x):
    """Every channel this one uses: [ch], [ch, ch2], plus the subs' two when they're on."""
    out = [x["ch"], x["ch2"]] if x.get("stereo") else [x["ch"]]
    if (x.get("subs") or {}).get("on"):
        out += [x["subs"]["ch"], x["subs"]["ch2"]]
    return out


def tidy(x):
    out = {f: x[f] for f in FIELDS if f in x} if x.get("stereo") else {f: x[f] for f in ("from", "via", "ch")}
    if (x.get("subs") or {}).get("on"):
        out["subs"] = {f: x["subs"][f] for f in SUB_FIELDS}
    return out


def valid(d):
    if not isinstance(d, dict):
        return "RTA inputs not understood."
    for k, groups, what in (("mic", MIC_FROM, "The room mic"), ("ref", REF_FROM, "The desk reference")):
        x = d.get(k)
        if not isinstance(x, dict):
            return "RTA inputs not understood."
        if not _src_ok(x.get("from"), groups):
            return what + " can't come from there."
        if x.get("via") not in VIA:
            return what + " comes in by Dante Virtual Soundcard, AES67 or Wing USB."
        for f in ("ch", "ch2") if x.get("stereo") else ("ch",):
            c = x.get(f)
            if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= VIA[x["via"]]:
                return "%s: channel 1 to %d." % (what, VIA[x["via"]])
        if not isinstance(x.get("stereo", False), bool):
            return "Mono or stereo not understood."
        if x.get("stereo"):
            if not _src_ok(x.get("from2"), groups):
                return what + "'s right side can't come from there."
            if x["ch2"] == x["ch"]:
                return what + ": left and right need different channels."
        sub = x.get("subs")
        if sub is not None:
            if k != "ref" or not isinstance(sub, dict) or not isinstance(sub.get("on"), bool):
                return "Only the desk reference can have subs added."
            if sub["on"]:
                for f in ("from", "from2"):
                    if not _src_ok(sub.get(f), REF_FROM):
                        return "The subs come from a main, bus or matrix."
                for f in ("ch", "ch2"):
                    c = sub.get(f)
                    if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= VIA[x["via"]]:
                        return "The subs: channel 1 to %d." % VIA[x["via"]]
    used = [(d[k]["via"], c) for k in ("mic", "ref") for c in channels(d[k])]
    if len(set(used)) != len(used):
        return "The room mic and the desk reference can't use the same channel."
    return None


class RtaInputs:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "rta_inputs.json")
        self.lock = threading.Lock()
        self.d = json.loads(json.dumps(DEFAULT))
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not valid(d):
                self.d = {k: tidy(d[k]) for k in DEFAULT}
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.d))

    def save(self, d):
        d = {k: tidy(d[k]) for k in DEFAULT}
        with self.lock:
            self.d = d
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path)

    def view(self, dante=None, wing=None):
        """The settings, what Dante really routes into those DVS channels, and the desk's names for the sources."""
        out = self.get()
        dv = dante.get() if dante is not None else {"status": "unavailable", "channels": {}}
        out["dante"] = {"status": dv.get("status", ""), "reason": dv.get("reason", ""), "device": dv.get("device", ""), "at": dv.get("at", "")}
        for k in DEFAULT:
            x = out[k]
            x["route"] = dv.get("channels", {}).get(str(x["ch"])) if x["via"] == "dvs" else None
            if x.get("stereo"):
                x["route2"] = dv.get("channels", {}).get(str(x["ch2"])) if x["via"] == "dvs" else None
            if (x.get("subs") or {}).get("on"):
                for f, r in (("ch", "route"), ("ch2", "route2")):
                    x["subs"][r] = dv.get("channels", {}).get(str(x["subs"][f])) if x["via"] == "dvs" else None
        names = {}
        if wing is not None:
            st = wing.state()
            if st["status"] == "connected":
                for kind in ("main", "bus", "mtx"):
                    for n, v in st["strips"].get(kind, {}).items():
                        if v.get("name"):
                            names[kind.upper() + "/" + n] = v["name"]
                for n, v in st["strips"].get("ch", {}).items():  # a channel fed by an input lends the input its name
                    key = "%s/%s" % (v["src_grp"], v["src_in"]) if v.get("src_grp") and v.get("src_in") else None
                    if key and v.get("name"):
                        names.setdefault(key, v["name"])
        out["names"] = names
        return out
