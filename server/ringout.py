"""Ring-Out: the feedback watch, on the room mic the C3 Life server already analyses (rta_live.py).

Feedback is one note that rings: a narrow peak, well above the sound either side of it, that stays at the same pitch
and doesn't die away. Music has peaks too, so a peak only counts once it has stood for a while:

  sensitivity  stands above its neighbours by  for at least
  low          20 dB                            2.0 s
  normal       15 dB                            1.4 s
  high         10 dB                            0.8 s

It's looked for between 80 Hz and 12 kHz, on the 4096-point spectrum (about 12 Hz apart), and the pitch is refined
between bins, so the cut lands on the note. A held note on a synth or a long sung note can still look like feedback on
High; that's why nothing changes on the desk until someone says so.

The cut (cuts()): a narrow cut (Q 8, −6 dB; again on the same note: −9, then −12) on a flat band (0 dB gain) of Main 1's
EQ on the Wing, so it never changes a band FOH has set. Undo puts that band back as it was. Kept in data/ringout.json.
"""

import json
import math
import os
import threading
import time

SENS = {"low": (20.0, 10), "normal": (15.0, 7), "high": (10.0, 4)}  # (dB above its neighbours, frames at 5 a second)
LO_HZ, HI_HZ = 80.0, 12000.0
FLOOR = -70.0      # dBFS: quieter than this isn't feedback anyone can hear
MATCH = 0.03       # the same note if within 3%
GONE = 5           # frames (1 s) without it: it's stopped
RECENT = 10
CUT_Q, CUT_DB, CUT_STEP, CUT_MAX = 8.0, -6.0, -3.0, -12.0
TARGET = ("main", 1)
BANDS = "654321"   # a free band, from the top: FOH tends to use the low ones


def _db(p):
    return 10 * math.log10(p) if p > 1e-14 else -140.0


def peaks(p, df, prom_db):
    """Narrow peaks in one spectrum (bin powers): [(Hz, dBFS, dB above its neighbours)]."""
    lo, hi = max(2, int(LO_HZ / df)), min(len(p) - 3, int(HI_HZ / df))
    out = []
    d = None
    for k in range(lo, hi):
        if not (p[k] > p[k - 1] and p[k] >= p[k + 1]):
            continue
        lvl = _db(p[k])
        if lvl < FLOOR:
            continue
        # the sound either side: a third of an octave each way, leaving out the peak's own few bins
        w = max(4, int(k * 0.26))
        side = p[max(1, k - w):max(1, k - 2)] + p[k + 3:k + w + 1]
        if not side:
            continue
        side = sorted(side)
        med = _db(side[len(side) // 2])
        if lvl - med < prom_db:
            continue
        # between bins: a parabola through the three dB values
        a, b, c = _db(p[k - 1]), lvl, _db(p[k + 1])
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
        out.append((round((k + max(-0.5, min(0.5, off))) * df, 1), round(lvl, 1), round(lvl - med, 1)))
    return out


class Watch:
    """Follows the peaks from one spectrum to the next and says which are ringing."""

    def __init__(self, data_dir, df, log=None, now=time.time):
        self.path = os.path.join(data_dir, "ringout.json")
        self.df, self.log, self.now = df, log, now
        self.lock = threading.Lock()
        self.on, self.sens = True, "normal"
        self.tracks = []    # {"f", "db", "first_db", "hits", "seen", "since", "prom", "rang"}
        self.recent = []    # what rang lately: {"f", "db", "at"}
        self.cuts = []      # {"id", "kind", "n", "band", "f", "g", "q", "prev", "at", "who"}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.on = bool(d.get("on", True))
            self.sens = d.get("sens") if d.get("sens") in SENS else "normal"
            self.cuts = [c for c in d.get("cuts", []) if isinstance(c, dict) and "band" in c]
        except (OSError, ValueError, AttributeError):
            pass
        self.frame = 0

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"on": self.on, "sens": self.sens, "cuts": self.cuts}, f, indent=1)
        os.replace(tmp, self.path)

    def settings(self, on=None, sens=None):
        with self.lock:
            if on is not None:
                self.on = bool(on)
            if sens is not None:
                if sens not in SENS:
                    return "Sensitivity is low, normal or high."
                self.sens = sens
            if not self.on:
                self.tracks = []
            self._write()
        return None

    def feed(self, p):
        """One spectrum of the room mic (bin powers, 0 dBFS = 1.0)."""
        if not self.on:
            return
        prom, need = SENS[self.sens]
        found = peaks(p, self.df, prom)
        t = self.now()
        with self.lock:
            self.frame += 1
            for f, lvl, pr in found:
                tr = next((x for x in self.tracks if abs(x["f"] - f) <= x["f"] * MATCH), None)
                if tr is None:
                    self.tracks.append({"f": f, "db": lvl, "first_db": lvl, "hits": 1, "seen": self.frame, "since": t, "prom": pr, "rang": False})
                else:
                    tr.update(f=f, db=lvl, prom=pr, seen=self.frame, hits=tr["hits"] + 1)
            self.tracks = [x for x in self.tracks if self.frame - x["seen"] <= GONE]
            for x in self.tracks:
                ringing = x["hits"] >= need and self.frame - x["seen"] <= 1 and x["db"] >= x["first_db"] - 3.0
                if ringing and not x["rang"]:
                    x["rang"] = True
                    self.recent = ([{"f": x["f"], "db": x["db"], "at": round(t, 1)}] + self.recent)[:RECENT]
                    if self.log:
                        self.log.info("ringout: ringing at %.0f Hz", x["f"])

    def ringing(self):
        with self.lock:
            prom, need = SENS[self.sens]
            return [{"f": x["f"], "db": x["db"], "prom": x["prom"], "since": round(x["since"], 1)}
                    for x in sorted(self.tracks, key=lambda x: -x["db"])
                    if x["rang"] and self.frame - x["seen"] <= 1]

    def view(self, offset=0.0):
        r = self.ringing()
        with self.lock:
            return {"on": self.on, "sens": self.sens,
                    "ringing": [dict(x, db=round(x["db"] + offset, 1)) for x in r],
                    "recent": [dict(x, db=round(x["db"] + offset, 1)) for x in self.recent],
                    "cuts": [{k: c[k] for k in ("id", "kind", "n", "band", "f", "g", "q", "at", "who")} for c in self.cuts],
                    "target": "%s %d" % (TARGET[0].capitalize(), TARGET[1])}

    # ---- the cut on the Wing (asked first on the screen; RTA settings and Mixer control) ----
    def cut(self, desk, f, who):
        """A narrow cut at f Hz on Main 1. -> (None, cut) or (what's wrong, None)"""
        if not isinstance(f, (int, float)) or isinstance(f, bool) or not LO_HZ <= f <= HI_HZ:
            return "Ring-Out cuts between 80 Hz and 12 kHz.", None
        kind, n = TARGET
        desk.ask_eq(kind, n)  # read it fresh: FOH may have changed it on the desk
        eq = {}
        for _ in range(15):
            time.sleep(0.1)
            eq = desk.eq(kind, n)
            if len(eq) >= 20:
                break
        if not eq:
            return "C3 Life couldn't read Main %d's EQ from the Wing. Is it connected?" % n, None
        if not eq.get("eq/on"):
            return "Main %d's EQ is turned off on the Wing. Turn it on there first (it may have other settings in it)." % n, None
        with self.lock:
            again = next((c for c in self.cuts if abs(c["f"] - f) <= c["f"] * MATCH), None)
        if again:  # the same note again: deeper
            if again["g"] <= CUT_MAX:
                return "That note is already cut by %d dB. Turn the system down a little, or look at the mic." % -CUT_MAX, None
            g = max(CUT_MAX, again["g"] + CUT_STEP)
            err = desk.set(kind, n, "eq/%sg" % again["band"], g)
            if err:
                return err, None
            with self.lock:
                again.update(g=g, at=round(self.now(), 1), who=who)
                self._write()
            return None, dict(again)
        used = {c["band"] for c in self.cuts}
        band = next((b for b in BANDS if b not in used and abs(eq.get("eq/%sg" % b, 1.0)) < 0.05), None)
        if band is None:
            return "Main %d has no flat EQ band left to cut with. Undo an older Ring-Out cut, or cut it on the Wing." % n, None
        prev = {k: eq.get("eq/%s%s" % (band, k)) for k in "gfq"}
        f = round(float(f), 1)
        for field, v in (("f", f), ("q", CUT_Q), ("g", CUT_DB)):  # the frequency first, so it never cuts the wrong note
            err = desk.set(kind, n, "eq/%s%s" % (band, field), v)
            if err:
                return err, None
        c = {"id": "%s%d-%s-%d" % (kind, n, band, int(self.now())), "kind": kind, "n": n, "band": band, "f": f, "g": CUT_DB, "q": CUT_Q,
             "prev": prev, "at": round(self.now(), 1), "who": who}
        with self.lock:
            self.cuts.append(c)
            self._write()
        if self.log:
            self.log.info("ringout: %s cut %.0f Hz by %d dB on %s %d band %s", who, f, -CUT_DB, kind, n, band)
        return None, dict(c)

    def undo(self, desk, cid, who):
        with self.lock:
            c = next((x for x in self.cuts if x["id"] == cid), None)
        if not c:
            return "That cut isn't on the list any more."
        for field in "gfq":  # the gain back first, so nothing else is cut on the way
            v = c["prev"].get(field)
            if v is not None:
                err = desk.set(c["kind"], c["n"], "eq/%s%s" % (c["band"], field), v)
                if err:
                    return err
        with self.lock:
            self.cuts = [x for x in self.cuts if x["id"] != cid]
            self._write()
        if self.log:
            self.log.info("ringout: %s undid the %.0f Hz cut on %s %d band %s", who, c["f"], c["kind"], c["n"], c["band"])
        return None
