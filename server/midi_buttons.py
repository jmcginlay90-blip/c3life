"""MIDI buttons: a pad or footswitch on a MIDI controller (plugged into a Mac or iPad on the network MIDI session, see
midi_in.py) does one thing in C3 Life: run a macro, the next or previous slide, Lights Go, Playback Play or Stop,
or a log marker. Kept in data/midi_buttons.json; set on Settings › MIDI buttons.

Nothing happens until "on" is set: until then each press is shown with what it would have done. Learn: the next
note or CC heard is taken for the new button. A note counts when it's pressed (velocity > 0); a CC when it goes
from below 64 to 64 or more (so a footswitch counts once per press, not on release).
A button press is someone choosing to do it, so it doesn't ask again on a screen (that's why "on" starts off).
"""

import json
import os
import re
import threading
import time

DOS = ("macro", "pp_next", "pp_prev", "lights_go", "play", "stop", "marker")
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
        return None

    def save(self, d, who):
        err = self.valid(d)
        if err:
            return err
        d = {"on": d["on"], "maps": [{"msg": m["msg"], "label": m.get("label", ""), "do": m["do"], "arg": m.get("arg", "")} for m in d["maps"]]}
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
        """One note-on or CC from the network MIDI session. -> None if it's not a button press, else (what, did, sent)."""
        if kind == 0x90 and len(data) == 2 and data[1] > 0:
            m = "note:%d" % data[0]
        elif kind == 0xB0 and len(data) == 2:
            was = self.cc.get(data[0], 0)
            self.cc[data[0]] = data[1]
            if not (was < 64 <= data[1]):
                return None
            m = "cc:%d" % data[0]
        else:
            return None
        if self.learning > time.time():
            self.learning, self.learned = 0.0, m
            return msg_name(m), "learned for the new button", False
        with self.lock:
            on = self.d["on"]
            b = next((x for x in self.d["maps"] if x["msg"] == m), None)
        if not b:
            return None  # not a button: Lights may use it
        desc = (b["label"] + ": " if b["label"] else "") + self.describe(b["do"], b.get("arg", ""))
        if not on:
            return msg_name(m), desc + " (not sent: MIDI buttons are off)", False
        fn = self.actions.get(b["do"])
        err = fn(b.get("arg", ""), "MIDI button") if fn else "That can't be done here."
        if not err:
            self.log.info("midi buttons: %s: %s", msg_name(m), desc)
        return msg_name(m), desc + ("" if not err else " (not sent: %s)" % err), not err

    def view(self):
        with self.lock:
            d = json.loads(json.dumps(self.d))
        return {"settings": d, "learning": self.learning > time.time(), "learned": self.learned}
