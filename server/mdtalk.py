"""MD talk: every screen goes green while the MD (music director) is talking, and what they said is written down.

It watches the MD's channel on the Wing, a few times a second:
  mode 0  the channel is unmuted (the MD's mic button, or the mute lifted)
  mode 1  the MD is actually speaking: the channel's input level is over the threshold
  mode 2  the Wing's own talkback (TB) button is pressed: the Wing value at "tb_path" is on. The address the Wing uses
          for it isn't in Behringer's documents, so find it once with the hardware check kit
          (hwcheck.py --find-talkback: press TB and it prints what changed) and type it on MD talk.
It stays green HOLD seconds after they stop, so it doesn't flicker between words.

The words come from Listen's subtitles: pick the Listen source that carries the MD mic (one DVS channel, with
subtitles on) and the lines spoken while the MD was talking go in the log. With no source, the log still says when
and for how long. "Also show on the stage display" sends "MD is talking" as ProPresenter's stage message, and clears it.
The log is kept for this service (cleared when C3 Life starts) unless "keep" is "report" (kept in data/mdlog.json).

"""

import json
import re
import os
import threading
import time

HOLD = 1.5
KEEP = 200
MODES = (0, 1, 2)
TB_DEFAULT = "/cfg/talk/A/on"  # a guess until hwcheck.py --find-talkback has been run at the Wing
KEEPS = ("service", "report")


class MDTalk:
    def __init__(self, data_dir, cfg, log, desk, listen=None, pp=None, now=time.time):
        self.cfg, self.log, self.desk, self.listen, self.pp, self.now = cfg, log, desk, listen, pp, now
        self.path = os.path.join(data_dir, "mdtalk.json")
        self.lock = threading.Lock()
        self.settings = {"ch": 12, "mode": 1, "thresh": -35, "source": "", "write": True, "stage": False, "keep": "service", "tb_path": TB_DEFAULT}
        self.lines = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            for k in self.settings:
                if isinstance(d.get(k), type(self.settings[k])):
                    self.settings[k] = d[k]
            if self.settings["keep"] == "report" and isinstance(d.get("lines"), list):
                self.lines = d["lines"][:KEEP]
        except (OSError, ValueError, AttributeError):
            pass
        self.talking, self.since, self.last_on, self.test_until = False, 0.0, 0.0, 0.0
        self.cap_from = 0      # the last subtitle line seen, so only new ones are taken
        self.words = []        # what was said while talking
        self.next_id = max([x.get("id", 0) for x in self.lines] + [0]) + 1
        self.stop_ev = threading.Event()

    # ---- settings ----
    def set(self, b, who):
        """-> error or None"""
        if not isinstance(b, dict):
            return "Bad request"
        kinds = getattr(self.desk, "KINDS", {}) or {"ch": 40}
        new = dict(self.settings)
        for k, v in b.items():
            if k == "ch" and isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= kinds.get("ch", 40):
                new[k] = v
            elif k == "mode" and v in MODES and not isinstance(v, bool):
                new[k] = v
            elif k == "thresh" and isinstance(v, (int, float)) and not isinstance(v, bool) and -80 <= v <= 0:
                new[k] = round(float(v), 1)
            elif k == "source" and isinstance(v, str) and len(v) <= 60:
                new[k] = v
            elif k in ("write", "stage") and isinstance(v, bool):
                new[k] = v
            elif k == "keep" and v in KEEPS:
                new[k] = v
            elif k == "tb_path" and isinstance(v, str) and re.match(r"^(/[A-Za-z0-9$_.-]+){1,6}$", v):
                new[k] = v
            else:
                return "Bad request"
        with self.lock:
            self.settings = new
        self._save()
        self.log.info("mdtalk: %s changed MD talk (%s)", who, "Wing talkback at " + new["tb_path"] if new["mode"] == 2 else
                      "channel %d, %s" % (new["ch"], "unmuted" if new["mode"] == 0 else "speaking over %s dB" % new["thresh"]))
        return None

    def _save(self):
        with self.lock:
            d = dict(self.settings, lines=self.lines if self.settings["keep"] == "report" else [])
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, self.path)

    def clear(self, who):
        with self.lock:
            self.lines = []
        self._save()
        self.log.info("mdtalk: %s cleared the MD log", who)

    def test(self, who, secs=5):
        """Every screen goes green for a few seconds (the Test button). Changes nothing on any device."""
        self.test_until = self.now() + secs
        self.log.info("mdtalk: %s tested the MD box", who)

    # ---- watching ----
    def level(self):
        return self.desk.input_level(self.settings["ch"]) if hasattr(self.desk, "input_level") else None

    def raw(self):
        """Is the MD talking this instant (before the hold)?"""
        st = self.settings
        if st["mode"] == 2:
            v = self.desk.watch(st["tb_path"]) if hasattr(self.desk, "watch") else None
            return v not in (None, 0, 0.0, False, "", "0", "OFF", "off")
        if st["mode"] == 0:
            strip = (self.desk.state().get("strips", {}).get("ch", {}) or {}).get(str(st["ch"]), {})
            return strip.get("mute") in (0, False)
        lv = self.level()
        return lv is not None and lv > st["thresh"]

    def tick(self):
        t = self.now()
        on = False
        try:
            on = self.raw()
        except Exception as e:  # noqa: BLE001 - never stop watching
            self.log.debug("mdtalk: %s", e)
        if on:
            self.last_on = t
        talking = on or (self.talking and t - self.last_on < HOLD)
        self._words()
        if talking and not self.talking:
            self.talking, self.since, self.words = True, t, []
            self._stage("MD is talking")
        elif not talking and self.talking:
            self.talking = False
            self._stage("")
            self._line(t)

    def _words(self):
        src = self.settings["source"]
        if not (self.listen and src and self.settings["write"]):
            return
        s = next((x for x in self.listen.config()["sources"] if x["id"] == src), None)
        if not s:
            return
        got = self.listen.captions(s, self.cap_from)
        for ln in got.get("lines") or []:
            self.cap_from = max(self.cap_from, ln["id"])
            if self.talking or self.now() - self.last_on < HOLD + 2:  # the last words arrive a moment after they stop
                self.words.append(ln["text"])

    def _line(self, t):
        if not self.settings["write"]:
            return
        names = self.desk.state().get("strips", {}).get("ch", {}) if self.desk else {}
        ch = self.settings["ch"]
        name = (names.get(str(ch)) or {}).get("name") or ""
        line = {"id": self.next_id, "at": time.strftime("%H:%M", time.localtime(self.since)), "t": self.since,
                "dur": round(t - self.since - HOLD, 1) if t - self.since > HOLD else round(t - self.since, 1),
                "ch": "Wing talkback" if self.settings["mode"] == 2 else "Ch %02d%s" % (ch, " · " + name if name else ""), "text": " ".join(self.words).strip(),
                "words": bool(self.settings["source"])}
        self.next_id += 1
        with self.lock:
            self.lines.insert(0, line)
            del self.lines[KEEP:]
        if self.settings["keep"] == "report":
            self._save()

    def _stage(self, text):
        if not (self.settings["stage"] and self.pp):
            return
        try:
            self.pp._req("/v1/stage/message", method="PUT", body=text)
        except (OSError, ValueError):
            pass

    def run(self):
        while not self.stop_ev.wait(0.2):
            self.tick()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    # ---- what phones see ----
    def view(self):
        t = self.now()
        with self.lock:
            st = dict(self.settings)
            lines = list(self.lines)
        lv = None
        try:
            lv = self.level()
        except Exception:  # noqa: BLE001
            pass
        return {"settings": st, "talking": self.talking or t < self.test_until, "test": t < self.test_until and not self.talking,
                "since": self.since if self.talking else 0, "level": lv, "lines": lines}

    def live(self):
        """The small answer every screen asks for (the green box)."""
        t = self.now()
        return {"talking": self.talking or t < self.test_until, "test": t < self.test_until and not self.talking,
                "since": self.since if self.talking else 0, "secs": round(t - self.since) if self.talking else 0, "ch": self.settings["ch"]}
