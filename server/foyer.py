"""The foyer: its name, and which two Wing buses are its two mixes and which matrix feeds its speakers.

Kept on the C3 Life server so every device shows the same: the Foyer team's iPad, Show › Foyer and the menus.
The two mixes are called what they're called on the desk (the bus names come from the Wing, when it's connected).

data/foyer.json: {"name": "Foyer", "mix_a": 7, "mix_b": 8, "mtx": 1, "max_db": -5}
"""

import json
import os
import threading

DEFAULT = {"name": "Foyer", "mix_a": 7, "mix_b": 8, "mtx": 1, "max_db": -5}
MAX_DBS = (0, -5, -10, -15)
# What the two mixes are called when the desk isn't connected (and on a new Wing, whose buses have no names yet).
FALLBACK = {"mix_a": "Spotify", "mix_b": "Live service"}


def valid(d):
    if not isinstance(d, dict):
        return "Foyer settings not understood."
    n = d.get("name")
    if not isinstance(n, str) or not n.strip() or len(n.strip()) > 30 or any(ord(c) < 32 for c in n):
        return "The foyer's name is 1 to 30 characters."
    for k in ("mix_a", "mix_b"):
        v = d.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 16:
            return "The foyer's mixes are buses 1 to 16."
    if d["mix_a"] == d["mix_b"]:
        return "The two foyer mixes need to be different buses."
    m = d.get("mtx")
    if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 8:
        return "The foyer's matrix is 1 to 8."
    if d.get("max_db") not in MAX_DBS:
        return "Maximum volume is 0, −5, −10 or −15 dB."
    return None


class Foyer:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "foyer.json")
        self.lock = threading.Lock()
        self.d = dict(DEFAULT)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = dict(DEFAULT, **json.load(f))
            if not valid(d):
                self.d = d
        except (OSError, ValueError, TypeError):
            pass

    def get(self):
        with self.lock:
            return dict(self.d)

    def save(self, d):
        d = {k: d[k] for k in DEFAULT}
        d["name"] = d["name"].strip()
        with self.lock:
            self.d = d
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path)

    def view(self, wing=None):
        """The settings, plus what the desk calls each bus and matrix (empty when it isn't connected)."""
        out = self.get()
        names = {"bus": {}, "mtx": {}}
        live = False
        if wing is not None:
            st = wing.state()
            live = st["status"] == "connected"
            if live:
                for k in names:
                    names[k] = {n: v.get("name", "") for n, v in st["strips"].get(k, {}).items() if v.get("name")}
        out["names"], out["from_desk"] = names, live
        for k in ("mix_a", "mix_b"):
            out[k + "_name"] = names["bus"].get(str(out[k])) or FALLBACK[k]
        out["mtx_name"] = names["mtx"].get(str(out["mtx"])) or ""
        return out
