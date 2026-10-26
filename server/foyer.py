"""The foyer: its name, and which two Wing buses are its two mixes and which matrix feeds its speakers.

Kept on the C3 Life server so every device shows the same: the Foyer team's iPad, Show › Foyer and the menus.
The two mixes are called what they're called on the desk (the bus names come from the Wing, when it's connected).

data/foyer.json: {"name": "Foyer", "mix_a": 7, "mix_b": 8, "mtx": 1, "max_db": -5,
                   "a_label": "", "b_label": "", "a_from": "", "b_from": ""}
The simple Foyer page (/foyer) shows each mix as a big button: called what the desk calls it, unless an Admin gave
it its own name (a_label, b_label), with a line in plain words saying where it comes from (a_from, b_from).

On the Wing (Control, from the Foyer screens):
  which mix the foyer hears  each bus's send to the foyer matrix (/bus/<b>/send/<mtx>/on): the one chosen on, the other
                             off (the new one goes on first, so there's no gap)
  volume                     the matrix's fader (/mtx/<m>/fdr), never above max_db
  mute                       the matrix's mute (/mtx/<m>/mute)
  balance                    each bus's send level to the matrix (/bus/<b>/send/<mtx>/lvl), so switching doesn't jump
                             (Foyer setup control, like choosing the buses)
In Watch only it shows what the desk is doing and changes nothing.
"""

import json
import os
import threading

DEFAULT = {"name": "Foyer", "mix_a": 7, "mix_b": 8, "mtx": 1, "max_db": -5, "a_label": "", "b_label": "", "a_from": "", "b_from": ""}
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
    for k, most in (("a_label", 30), ("b_label", 30), ("a_from", 60), ("b_from", 60)):
        v = d.get(k, "")
        if not isinstance(v, str) or len(v.strip()) > most or any(ord(c) < 32 for c in v):
            return "A button's name is up to 30 characters, and where it comes from up to 60."
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
        d = {k: d.get(k, DEFAULT[k]) for k in DEFAULT}
        for k in ("name", "a_label", "b_label", "a_from", "b_from"):
            d[k] = d[k].strip()
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
        out["a_title"], out["b_title"] = out["a_label"] or out["mix_a_name"], out["b_label"] or out["mix_b_name"]
        out["live"] = self.live(wing, st if wing is not None else None)
        return out

    # ---- what the foyer hears now, and changing it ----
    def live(self, wing, st=None):
        """{"mode", "connected", "source": "a"|"b"|"both"|"none"|None, "a_on", "b_on", "fdr", "mute"} from the desk"""
        d = self.get()
        if wing is None:
            return {"mode": "off", "connected": False, "source": None, "a_on": None, "b_on": None, "a_lvl": None, "b_lvl": None, "fdr": None, "mute": None}
        st = st or wing.state()
        ok = st["status"] == "connected"
        if ok:  # asked every time someone looks; the answer is there for the next look (a second later)
            wing.ask_mtx(d["mix_a"], d["mtx"])
            wing.ask_mtx(d["mix_b"], d["mtx"])
        sa, sb = wing.mtx_send(d["mix_a"], d["mtx"]), wing.mtx_send(d["mix_b"], d["mtx"])
        a, b = sa.get("on"), sb.get("on")
        src = None if a is None or b is None else ("both" if a and b else "a" if a else "b" if b else "none")
        m = st.get("strips", {}).get("mtx", {}).get(str(d["mtx"]), {}) if ok else {}
        return {"mode": st.get("mode", "off"), "connected": ok, "source": src, "a_on": a, "b_on": b,
                "a_lvl": sa.get("lvl"), "b_lvl": sb.get("lvl"), "fdr": m.get("fdr"), "mute": m.get("mute")}

    def do(self, wing, body, who, log=None):
        """A button on a Foyer screen: {"do": "source", "to": "a"|"b"} | {"do": "volume", "db": n} | {"do": "mute", "on": bool}.
        -> None, or what's wrong in words"""
        if wing is None:
            return "No sound desk is set up."
        if not isinstance(body, dict):
            return "Not understood."
        d, what = self.get(), body.get("do")
        if what == "source":
            to = body.get("to")
            if to not in ("a", "b"):
                return "The foyer hears one of its two mixes."
            new, old = (d["mix_a"], d["mix_b"]) if to == "a" else (d["mix_b"], d["mix_a"])
            err = wing.set("bus", new, "mtxsend/%d/on" % d["mtx"], True) or wing.set("bus", old, "mtxsend/%d/on" % d["mtx"], False)
            msg = "switched %s to bus %d" % (d["name"], new)
        elif what == "volume":
            db = body.get("db")
            if not isinstance(db, (int, float)) or isinstance(db, bool) or not -144 <= db <= 10:
                return "Volume is in dB, from −∞."
            if db > d["max_db"]:
                return "The %s's volume can't go above %d dB (an Admin sets the maximum)." % (d["name"], d["max_db"])
            err = wing.set("mtx", d["mtx"], "fdr", float(db))
            msg = None  # not every step of a slider
        elif what == "balance":
            which, db = body.get("mix"), body.get("db")
            if which not in ("a", "b") or not isinstance(db, (int, float)) or isinstance(db, bool) or not -144 <= db <= 10:
                return "Not understood."
            err = wing.set("bus", d["mix_a"] if which == "a" else d["mix_b"], "mtxsend/%d/lvl" % d["mtx"], float(db))
            msg = None
        elif what == "mute":
            on = bool(body.get("on"))
            err = wing.set("mtx", d["mtx"], "mute", on)
            msg = "%s %s" % ("muted" if on else "unmuted", d["name"])
        else:
            return "Not understood."
        if not err and msg and log:
            log.info("foyer: %s %s", who, msg)
        return err
