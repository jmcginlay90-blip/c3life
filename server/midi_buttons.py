"""MIDI buttons: a pad or footswitch on a MIDI controller (plugged into a Mac or iPad on the network MIDI session, see
midi_in.py) does one thing in C3 Life: run a macro, the next or previous slide, Lights Go, Playback Play or Stop,
or a log marker. Kept in data/midi_buttons.json; set on Settings › MIDI buttons.

Nothing happens until "on" is set: until then each press is shown with what it would have done. Learn: the next
note or CC heard is taken for the new button. A note counts when it's pressed (velocity > 0); a CC when it goes
from below 64 to 64 or more (so a footswitch counts once per press, not on release).
A button press is someone choosing to do it, so it doesn't ask again on a screen (that's why "on" starts off).

An A/B switch ("do": "ab") is a button with two sides, each one of the macros (Macros): each press flips A <-> B and
runs that side's macro (for example A = mute the pastor's mic and the band stage layout, B = unmute it and the speaker
layout). "hold": B only while the button is held down. "light": the controller's button is lit on B (the same note or
CC sent back at 127, and 0 for A). "follow": a Wing channel: if someone mutes it on the desk while the switch is on B,
the switch shows A again (nothing is run). Undo is the macro's own (Macros › Undo).
"""

import json
import os
import re
import threading
import time

DOS = ("macro", "pp_next", "pp_prev", "lights_go", "play", "stop", "marker", "ab")
MSG = re.compile(r"^(note|cc):(\d{1,3})$")
LEARN_FOR = 20


def msg_name(m):
    k, n = m.split(":")
    return ("Note %s" if k == "note" else "CC %s") % n


class Buttons:
    def __init__(self, data_dir, log, actions=None, describe=None):
        self.path = os.path.join(data_dir, "midi_buttons.json")
        self.log = log
        self.actions = actions or {}       # do -> fn(arg, who) -> error or None (the server fills this in)
        self.describe = describe or (lambda do, arg: do)
        self.lock = threading.Lock()
        self.d = {"on": False, "maps": []}
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if not self.valid(d):
                self.d = {"on": d["on"], "maps": d["maps"]}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.learning = 0.0
        self.learned = ""
        self.cc = {}   # cc number -> last value, for the rising edge
        self.side = {}  # an A/B switch's msg -> "A" or "B"
        self.send = None  # fn([status, data1, data2]): back to the controller (the server sets it: midi.send)

    @staticmethod
    def valid(d):
        if not isinstance(d, dict) or not isinstance(d.get("on"), bool) or not isinstance(d.get("maps"), list) or len(d["maps"]) > 64:
            return "Bad request"
        seen = set()
        for m in d["maps"]:
            if not isinstance(m, dict) or not isinstance(m.get("msg"), str) or not MSG.match(m["msg"]) or int(m["msg"].split(":")[1]) > 127:
                return "Each button needs a note or CC (press Learn, then the button)."
            if m["msg"] in seen:
                return "%s is on two buttons." % msg_name(m["msg"])
            seen.add(m["msg"])
            if m.get("do") not in DOS or not isinstance(m.get("label", ""), str) or len(m.get("label", "")) > 40:
                return "Bad request"
            if m["do"] == "macro" and not (isinstance(m.get("arg"), str) and m["arg"]):
                return "Pick which macro."
            if m["do"] == "ab":
                if not all(isinstance(m.get(k, ""), str) for k in ("a", "b")) or not (m.get("a") or m.get("b")):
                    return "An A/B switch needs a macro for A or B (or both)."
                if not all(isinstance(m.get(k, False), bool) for k in ("hold", "light")):
                    return "Bad request"
                f = m.get("follow", 0)
                if not isinstance(f, int) or isinstance(f, bool) or not 0 <= f <= 40:
                    return "Bad request"
        return None

    def save(self, d, who):
        err = self.valid(d)
        if err:
            return err
        def clean(m):
            x = {"msg": m["msg"], "label": m.get("label", ""), "do": m["do"], "arg": m.get("arg", "")}
            if m["do"] == "ab":
                x.update(a=m.get("a", ""), b=m.get("b", ""), hold=m.get("hold", False), light=m.get("light", False), follow=m.get("follow", 0))
            return x
        d = {"on": d["on"], "maps": [clean(m) for m in d["maps"]]}
        with self.lock:
            self.d = d
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        os.replace(self.path + ".tmp", self.path)
        self.log.info("midi buttons: %s saved %d buttons (%s)", who, len(d["maps"]), "on" if d["on"] else "off")
        return None

    def learn(self):
        self.learning, self.learned = time.time() + LEARN_FOR, ""

    def handle(self, kind, data):
        """One note or CC from the network MIDI session. -> None if it's not a button press, else (what, did, sent)."""
        up = False  # a note off, or a CC going back below 64 (only an A/B switch held down cares)
        if kind == 0x90 and len(data) == 2 and data[1] > 0:
            m = "note:%d" % data[0]
        elif (kind == 0x80 or kind == 0x90) and len(data) == 2:
            m, up = "note:%d" % data[0], True
        elif kind == 0xB0 and len(data) == 2:
            was = self.cc.get(data[0], 0)
            self.cc[data[0]] = data[1]
            m = "cc:%d" % data[0]
            if was < 64 <= data[1]:
                pass
            elif was >= 64 > data[1]:
                up = True
            else:
                return None
        else:
            return None
        if self.learning > time.time() and not up:
            self.learning, self.learned = 0.0, m
            return msg_name(m), "learned for the new button", False
        with self.lock:
            on = self.d["on"]
            b = next((x for x in self.d["maps"] if x["msg"] == m), None)
        if not b or (up and not (b["do"] == "ab" and b.get("hold"))):
            return None  # not a button (Lights may use it), or a release that means nothing
        if b["do"] == "ab":
            return self._ab(b, m, up, on)
        desc = (b["label"] + ": " if b["label"] else "") + self.describe(b["do"], b.get("arg", ""))
        if not on:
            return msg_name(m), desc + " (not sent: MIDI buttons are off)", False
        fn = self.actions.get(b["do"])
        err = fn(b.get("arg", ""), "MIDI button") if fn else "That can't be done here."
        if not err:
            self.log.info("midi buttons: %s: %s", msg_name(m), desc)
        return msg_name(m), desc + ("" if not err else " (not sent: %s)" % err), not err

    def _ab(self, b, m, up, on):
        cur = self.side.get(m, "A")
        new = ("A" if up else "B") if b.get("hold") else ("B" if cur == "A" else "A")
        mid = b.get("b" if new == "B" else "a", "")
        desc = (b["label"] + ": " if b["label"] else "A/B switch: ") + new + (" · " + self.describe("macro", mid) if mid else " · nothing set for " + new)
        if not on:
            return msg_name(m), desc + " (not sent: MIDI buttons are off)", False
        err = self.actions["macro"](mid, "MIDI A/B switch") if mid and "macro" in self.actions else None
        if err:
            return msg_name(m), desc + " (not sent: %s)" % err, False
        self.side[m] = new
        self._light(b, m, new)
        self.log.info("midi buttons: %s: %s", msg_name(m), desc)
        return msg_name(m), desc, True

    def _light(self, b, m, side):
        if not (b.get("light") and self.send):
            return
        k, n = m.split(":")
        v = 127 if side == "B" else 0
        try:
            self.send([0x90 if k == "note" else 0xB0, int(n), v] if (k != "note" or v) else [0x80, int(n), 0])
        except Exception:  # noqa: BLE001 - a controller that can't be lit still switches
            pass

    def follow(self, strips):
        """The desk changed: an A/B switch on B whose Wing channel was muted there goes back to A (nothing is run)."""
        with self.lock:
            maps = [x for x in self.d["maps"] if x["do"] == "ab" and x.get("follow")]
        for b in maps:
            if self.side.get(b["msg"]) == "B" and (strips.get(str(b["follow"])) or {}).get("mute") in (1, True):
                self.side[b["msg"]] = "A"
                self._light(b, b["msg"], "A")
                self.log.info("midi buttons: %s went back to A (Ch %d was muted on the Wing)", b["label"] or msg_name(b["msg"]), b["follow"])

    def view(self):
        with self.lock:
            d = json.loads(json.dumps(self.d))
        return {"settings": d, "learning": self.learning > time.time(), "learned": self.learned, "sides": dict(self.side)}
