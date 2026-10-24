"""Macros: the Sunday run sheet. Each macro is a list of steps that run in order when someone presses Run, and only then:
nothing here runs by itself.

A step is {"dev", "do", "label", ...}. The server offers the steps it can run (choices()), named from what's set up:
the playlists on Music, the foyer's two mixes, the cues and looks on Lights, ProPresenter's macros, the Wing's names.

  wing          mute | unmute | fader (db) | fade (db, secs) on a channel, aux input, bus, main, matrix or DCA;
                send (a channel's or aux input's send to a bus: db)
  music         playlist (name) | play | pause | fade (to %, seconds, then pause)
  foyer         source (a | b) | volume (db) | mute (on)
  lights        cue (id) | look (id) | go | back | release | master (0-1) | blackout (on)
  propresenter  macro (id) | next | previous | clear
  playback      play | stop | next | previous: notes sent to Playback over network MIDI (midi_in.send)
  wait          secs (up to 60)

A run stops at the first step that doesn't work and says which and why. Stop stops it between steps (and during a
wait). Undo, for 10 minutes after a run, puts back what can be put back, in reverse: the Wing's mutes, faders and sends,
the foyer, Lights' master and blackout. Music, cues, ProPresenter and Playback can't be put back, and Undo says so.

data/macros.json: {"macros": [{"id", "name", "color", "steps": [...]}], "playback": {"channel": 16, "notes": {...}}}
"""

import copy
import json
import os
import re
import threading
import time
import urllib.parse

KINDS = {"ch": "Ch", "aux": "Aux", "bus": "Bus", "main": "Main", "mtx": "Mtx", "dca": "DCA"}
SENDERS = ("ch", "aux")
DEVS = ("wing", "music", "foyer", "lights", "propresenter", "playback", "wait")
PLAYBACK = {"channel": 16, "notes": {"play": 60, "stop": 61, "next": 62, "previous": 63}}
UNDO_FOR = 600
MAX_STEPS, MAX_MACROS = 40, 40
COLORS = ("#F5C518", "#B48CF2", "#6CB4F5", "#FF7A45", "#7FD1C1", "#1ED760", "#E58FB8", "#A3ACB8")
STARTERS = [  # what a new server starts with: the mock-up's four, made of steps it can run
    {"id": "walkin", "name": "Walk-in", "color": "#F5C518", "steps": [
        {"dev": "foyer", "do": "source", "to": "a", "label": "Foyer: Spotify"},
        {"dev": "foyer", "do": "volume", "db": -10, "label": "Foyer volume: Normal (−10 dB)"},
        {"dev": "wait", "secs": 2, "label": "Wait 2s"}]},
    {"id": "worship", "name": "Worship start", "color": "#B48CF2", "steps": [
        {"dev": "music", "do": "fade", "to": 0, "seconds": 10, "then": "pause", "label": "Fade out over 10s"},
        {"dev": "foyer", "do": "source", "to": "b", "label": "Foyer: Live service"},
        {"dev": "playback", "do": "play", "label": "Play"}]},
    {"id": "message", "name": "Message", "color": "#6CB4F5", "steps": [
        {"dev": "playback", "do": "stop", "label": "Stop"},
        {"dev": "lights", "do": "go", "label": "Go"}]},
    {"id": "end", "name": "End of service", "color": "#FF7A45", "steps": [
        {"dev": "playback", "do": "stop", "label": "Stop"},
        {"dev": "foyer", "do": "source", "to": "a", "label": "Foyer: Spotify"}]},
]


def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def check_step(st):
    """None if C3 Life can run this step, else why not (in words)."""
    if not isinstance(st, dict) or st.get("dev") not in DEVS:
        return "That step isn't one C3 Life can run yet."
    if not isinstance(st.get("label"), str) or not st["label"].strip() or len(st["label"]) > 80:
        return "Each step needs a name."
    dev, do = st["dev"], st.get("do")
    if dev == "wait":
        return None if _num(st.get("secs"), 0.1, 60) else "A wait is up to 60 seconds."
    if dev == "wing":
        if st.get("kind") not in KINDS or not isinstance(st.get("n"), int) or isinstance(st.get("n"), bool) or not 1 <= st["n"] <= 40:
            return "Which channel, bus or DCA?"
        if do in ("mute", "unmute"):
            return None
        if do in ("fader", "fade"):
            if not _num(st.get("db"), -144, 10):
                return "A fader goes from −∞ to +10 dB."
            return None if do == "fader" or _num(st.get("secs"), 0.1, 60) else "A fade is up to 60 seconds."
        if do == "send":
            if st["kind"] not in SENDERS or not isinstance(st.get("bus"), int) or not 1 <= st["bus"] <= 16 or not _num(st.get("db"), -144, 10):
                return "A send is from a channel or aux input to a bus, at a level."
            return None
        return "That Wing step isn't one C3 Life can run yet."
    if dev == "music":
        if do == "playlist":
            return None if isinstance(st.get("name"), str) and st["name"] else "Which playlist?"
        if do in ("play", "pause"):
            return None
        if do == "fade":
            return None if _num(st.get("to"), 0, 100) and _num(st.get("seconds"), 1, 60) and st.get("then", "") in ("", "pause") else "Fade to 0-100% over 1-60 seconds."
        return "That music step isn't one C3 Life can run yet."
    if dev == "foyer":
        if do == "source":
            return None if st.get("to") in ("a", "b") else "Which of the foyer's mixes?"
        if do == "volume":
            return None if _num(st.get("db"), -144, 10) else "Volume is in dB."
        if do == "mute":
            return None if isinstance(st.get("on"), bool) else "Mute or unmute?"
        return "That foyer step isn't one C3 Life can run yet."
    if dev == "lights":
        if do in ("cue", "look"):
            return None if isinstance(st.get("id"), str) and st["id"] else "Which cue?"
        if do in ("go", "back", "release"):
            return None
        if do == "master":
            return None if _num(st.get("value"), 0, 1) else "The master is 0 to 100%."
        if do == "blackout":
            return None if isinstance(st.get("on"), bool) else "Bad request"
        return "That lights step isn't one C3 Life can run yet."
    if dev == "propresenter":
        if do == "macro":
            return None if isinstance(st.get("id"), str) and st["id"] else "Which ProPresenter macro?"
        return None if do in ("next", "previous", "clear") else "That ProPresenter step isn't one C3 Life can run yet."
    if dev == "playback":
        return None if do in PLAYBACK["notes"] else "That Playback step isn't one C3 Life can run yet."
    return "That step isn't one C3 Life can run yet."


def valid(d):
    if not isinstance(d, dict) or not isinstance(d.get("macros"), list) or len(d["macros"]) > MAX_MACROS:
        return "Macros not understood."
    ids = set()
    for m in d["macros"]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", m["id"]) or m["id"] in ids:
            return "Each macro needs its own id."
        ids.add(m["id"])
        if not isinstance(m.get("name"), str) or not m["name"].strip() or len(m["name"]) > 40:
            return "Each macro needs a name, up to 40 characters."
        if not isinstance(m.get("color"), str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", m["color"]):
            return "%s: colour not understood." % m["name"]
        if not isinstance(m.get("steps"), list) or len(m["steps"]) > MAX_STEPS:
            return "%s: up to %d steps." % (m["name"], MAX_STEPS)
        for i, st in enumerate(m["steps"]):
            err = check_step(st)
            if err:
                return "%s, step %d: %s" % (m["name"], i + 1, err)
    pb = d.get("playback", PLAYBACK)
    if not isinstance(pb, dict) or not isinstance(pb.get("channel"), int) or not 1 <= pb["channel"] <= 16 or \
            not isinstance(pb.get("notes"), dict) or set(pb["notes"]) != set(PLAYBACK["notes"]) or \
            not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 127 for v in pb["notes"].values()) or \
            len(set(pb["notes"].values())) != len(pb["notes"]):
        return "Playback: a channel 1-16 and four different notes 0-127."
    return None


# a step's device -> its id on the Devices screen, so a left-out device's steps are skipped
LEFT_OUT = {"wing": "desk", "lights": "lightkey", "propresenter": "propresenter"}


class Macros:
    def __init__(self, data_dir, cfg, log, desk=None, music=None, foyer=None, lights=None, midi=None):
        self.left_out = lambda: set()  # the Devices screen's left-out ids (the server sets this to health.left_out)
        self.path = os.path.join(data_dir, "macros.json")
        self.cfg, self.log = cfg, log
        self.desk, self.music, self.foyer, self.lights, self.midi = desk, music, foyer, lights, midi
        self.lock = threading.Lock()
        self.d = {"macros": copy.deepcopy(STARTERS), "playback": copy.deepcopy(PLAYBACK)}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = dict(self.d, **json.load(f))
            if not valid(d):
                self.d = d
        except (OSError, ValueError, TypeError):
            pass
        self.run_state = None   # {"id", "name", "by", "step", "of", "started", "done", "stopped", "error", "log": [...], "undo": [...]}
        self.stop_ev = threading.Event()
        self.pp_macros = (0.0, [])

    # ---- the list ----
    def get(self):
        with self.lock:
            return copy.deepcopy(self.d)

    def save(self, d, who):
        d = {"macros": [{k: m[k] for k in ("id", "name", "color", "steps")} for m in d["macros"]], "playback": d.get("playback", PLAYBACK)}
        with self.lock:
            self.d = d
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path)
        self.log.info("macros: %s saved %d macros", who, len(d["macros"]))

    # ---- what can be added (from what's set up) ----
    def _pp(self, path):
        url = (self.cfg.get("propresenter") or "").rstrip("/")
        if not url:
            raise OSError("ProPresenter hasn't been found on the network yet.")
        import urllib.request
        with urllib.request.urlopen(url + path, timeout=4) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def propresenter_macros(self):
        t, v = self.pp_macros
        if time.time() - t < 30:
            return v
        try:
            out = [{"id": m["id"]["uuid"], "name": m["id"]["name"]} for m in (self._pp("/v1/macros") or []) if (m.get("id") or {}).get("uuid")]
        except (OSError, ValueError, KeyError, TypeError):
            out = v
        self.pp_macros = (time.time(), out)
        return out

    def choices(self):
        """{dev: [step, ...]} the screen offers, each with its label, plus the Wing's names for its own picker."""
        c = {d: [] for d in DEVS}
        if self.music:
            for p in self.music.get()["playlists"]:
                c["music"].append({"dev": "music", "do": "playlist", "name": p["name"], "label": "Play playlist: " + p["name"]})
        c["music"] += [{"dev": "music", "do": "play", "label": "Play"}, {"dev": "music", "do": "pause", "label": "Pause"},
                       {"dev": "music", "do": "fade", "to": 0, "seconds": 10, "then": "pause", "label": "Fade out over 10s"},
                       {"dev": "music", "do": "fade", "to": 30, "seconds": 5, "label": "Fade in to 30%"}]
        if self.foyer:
            fy = self.foyer.view(self.desk)
            c["foyer"] = [{"dev": "foyer", "do": "source", "to": "a", "label": "%s: %s" % (fy["name"], fy["mix_a_name"])},
                          {"dev": "foyer", "do": "source", "to": "b", "label": "%s: %s" % (fy["name"], fy["mix_b_name"])},
                          {"dev": "foyer", "do": "volume", "db": -20, "label": "%s volume: Quiet (−20 dB)" % fy["name"]},
                          {"dev": "foyer", "do": "volume", "db": -10, "label": "%s volume: Normal (−10 dB)" % fy["name"]},
                          {"dev": "foyer", "do": "mute", "on": True, "label": "%s: Mute" % fy["name"]},
                          {"dev": "foyer", "do": "mute", "on": False, "label": "%s: Unmute" % fy["name"]}]
        if self.lights:
            show = self.lights.show
            c["lights"] = [{"dev": "lights", "do": "cue", "id": x["id"], "label": "Fire cue: " + x["name"]} for x in show["cues"]] + \
                          [{"dev": "lights", "do": "look", "id": x["id"], "label": "Look: " + x["name"]} for x in show["looks"]] + \
                          [{"dev": "lights", "do": "go", "label": "Go"}, {"dev": "lights", "do": "back", "label": "Back"},
                           {"dev": "lights", "do": "release", "label": "Release"},
                           {"dev": "lights", "do": "master", "value": 0.4, "label": "Master to 40%"},
                           {"dev": "lights", "do": "master", "value": 1.0, "label": "Master to 100%"},
                           {"dev": "lights", "do": "blackout", "on": True, "label": "Fade to black"},
                           {"dev": "lights", "do": "blackout", "on": False, "label": "Lights back up"}]
        c["propresenter"] = [{"dev": "propresenter", "do": "macro", "id": m["id"], "label": "Run macro: " + m["name"]} for m in self.propresenter_macros()] + \
                            [{"dev": "propresenter", "do": "next", "label": "Next slide"}, {"dev": "propresenter", "do": "previous", "label": "Previous slide"},
                             {"dev": "propresenter", "do": "clear", "label": "Clear slide"}]
        c["playback"] = [{"dev": "playback", "do": d, "label": l} for d, l in (("play", "Play"), ("stop", "Stop"), ("next", "Next song"), ("previous", "Previous song"))]
        c["wait"] = [{"dev": "wait", "secs": n, "label": "Wait %ds" % n} for n in (1, 2, 5, 10)]
        names = {}
        if self.desk:
            st = self.desk.state()
            names = {k: {n: v.get("name", "") for n, v in st.get("strips", {}).get(k, {}).items()} for k in KINDS}
        return {"steps": c, "wing": {"kinds": {k: getattr(self.desk, "KINDS", {}).get(k, 0) for k in KINDS}, "names": names}}

    # ---- running ----
    def view(self):
        with self.lock:
            r = copy.deepcopy(self.run_state)
        if r:
            r["can_undo"] = r["done"] and time.time() - r["done_at"] < UNDO_FOR and any(u["can"] for u in r["undo"]) and not r.get("undone")
            r["undo"] = [{"label": u["label"], "can": u["can"]} for u in r["undo"]]
        return {"macros": self.get()["macros"], "playback": self.get()["playback"], "run": r}

    def run(self, mid, who):
        """Starts a macro. -> None, or what's wrong"""
        with self.lock:
            if self.run_state and not self.run_state["done"]:
                return "%s is still running. Stop it first." % self.run_state["name"]
            m = next((x for x in self.d["macros"] if x["id"] == mid), None)
            if not m:
                return "That macro isn't on the list any more."
            if not m["steps"]:
                return "%s has no steps yet." % m["name"]
            self.stop_ev.clear()
            self.run_state = {"id": m["id"], "name": m["name"], "by": who, "step": 0, "of": len(m["steps"]), "started": time.time(),
                              "done": False, "done_at": 0, "stopped": False, "error": "", "log": [], "undo": []}
            steps = copy.deepcopy(m["steps"])
        self.log.info("macros: %s ran %s", who, m["name"])
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()
        return None

    def stop(self, who):
        with self.lock:
            if not self.run_state or self.run_state["done"]:
                return "Nothing is running."
        self.stop_ev.set()
        self.log.info("macros: %s stopped %s", who, self.run_state["name"])
        return None

    def _run(self, steps):
        for i, st in enumerate(steps):
            if self.stop_ev.is_set():
                break
            with self.lock:
                self.run_state["step"] = i + 1
            if LEFT_OUT.get(st["dev"]) in self.left_out():  # left out on the Devices screen: skipped, not a failure
                with self.lock:
                    self.run_state["log"].append({"label": st["label"], "ok": True, "error": "", "skipped": True})
                continue
            undo, err = self.step(st)
            with self.lock:
                self.run_state["log"].append({"label": st["label"], "ok": not err, "error": err or ""})
                if undo is not None:
                    self.run_state["undo"].append(undo)
            if err:
                with self.lock:
                    self.run_state["error"] = "Stopped at step %d (%s): %s" % (i + 1, st["label"], err)
                self.log.warning("macros: %s", self.run_state["error"])
                break
        with self.lock:
            self.run_state.update(done=True, done_at=time.time(), stopped=self.stop_ev.is_set() and len(self.run_state["log"]) < len(steps))

    def step(self, st):
        """Runs one step. -> (undo or None, error or None). An undo is {"label", "can", "run": callable}."""
        dev, do = st["dev"], st.get("do")
        label = st["label"]
        if dev == "wait":
            self.stop_ev.wait(float(st["secs"]))
            return None, None
        if dev == "wing":
            d = self.desk
            if d is None:
                return None, "No sound desk is set up."
            kind, n = st["kind"], st["n"]
            s0 = d.state().get("strips", {}).get(kind, {}).get(str(n), {})
            if do in ("mute", "unmute"):
                was = s0.get("mute")
                err = d.set(kind, n, "mute", do == "mute")
                return ({"label": label, "can": was is not None, "run": lambda: d.set(kind, n, "mute", bool(was))}, err) if not err else (None, err)
            if do in ("fader", "fade"):
                was = s0.get("fdr")
                err = d.set(kind, n, "fdr", float(st["db"])) if do == "fader" else d.fade([{"kind": kind, "n": n, "to": float(st["db"]), "secs": float(st["secs"])}])
                return ({"label": label, "can": was is not None, "run": lambda: d.set(kind, n, "fdr", float(was))}, err) if not err else (None, err)
            if do == "send":
                bus = st["bus"]
                cur = (d.bus_sends(bus) or {}).get(str(n) if kind == "ch" else "a%d" % n, {})
                was_lvl, was_on = cur.get("lvl"), cur.get("on")
                err = d.set(kind, n, "send/%d/on" % bus, True) or d.set(kind, n, "send/%d/lvl" % bus, float(st["db"]))
                def back():
                    return (d.set(kind, n, "send/%d/lvl" % bus, float(was_lvl)) if was_lvl is not None else None) or \
                           (d.set(kind, n, "send/%d/on" % bus, bool(was_on)) if was_on is not None else None)
                return ({"label": label, "can": was_lvl is not None, "run": back}, err) if not err else (None, err)
        if dev == "music":
            if not self.music:
                return None, "Music isn't set up."
            body = {k: v for k, v in st.items() if k not in ("dev", "label")}
            err = self.music.do(body, "macro")
            return ({"label": label, "can": False}, err) if not err else (None, err)
        if dev == "foyer":
            if not self.foyer:
                return None, "The foyer isn't set up."
            lv = self.foyer.live(self.desk)
            body = {k: v for k, v in st.items() if k not in ("dev", "label")}
            err = self.foyer.do(self.desk, body, "macro", self.log)
            if err:
                return None, err
            if do == "source":
                back = {"do": "source", "to": lv["source"]} if lv.get("source") in ("a", "b") else None
            elif do == "volume":
                back = {"do": "volume", "db": lv["fdr"]} if lv.get("fdr") is not None else None
            else:
                back = {"do": "mute", "on": bool(lv["mute"])} if lv.get("mute") is not None else None
            return {"label": label, "can": back is not None, "run": lambda: self.foyer.do(self.desk, back, "undo", self.log)}, None
        if dev == "lights":
            if not self.lights:
                return None, "Lights isn't set up."
            was_master, was_black = self.lights.master, self.lights.blackout
            ok, err, _ = self.lights.do({k: v for k, v in st.items() if k not in ("dev", "label")}, "macro")
            if not ok:
                return None, err
            if do == "master":
                return {"label": label, "can": True, "run": lambda: (lambda r: None if r[0] else r[1])(self.lights.do({"do": "master", "value": was_master}, "undo"))}, None
            if do == "blackout":
                return {"label": label, "can": True, "run": lambda: (lambda r: None if r[0] else r[1])(self.lights.do({"do": "blackout", "on": was_black}, "undo"))}, None
            return {"label": label, "can": False}, None
        if dev == "propresenter":
            try:
                if do == "macro":
                    self._pp("/v1/macro/%s/trigger" % urllib.parse.quote(st["id"]))
                elif do == "next":
                    self._pp("/v1/trigger/next")
                elif do == "previous":
                    self._pp("/v1/trigger/previous")
                else:
                    self._pp("/v1/clear/layer/slide")
            except (OSError, ValueError) as e:
                return None, "ProPresenter didn't answer (%s)." % (getattr(e, "reason", None) or e)
            return {"label": label, "can": False}, None
        if dev == "playback":
            if not self.midi:
                return None, "Network MIDI isn't running."
            pb = self.get()["playback"]
            ch, note = pb["channel"] - 1, pb["notes"][do]
            err = self.midi.send([0x90 | ch, note, 100]) or self.midi.send([0x80 | ch, note, 0])
            return ({"label": label, "can": False}, err) if not err else (None, err)
        return None, "That step isn't one C3 Life can run yet."

    def undo(self, who):
        """Puts back what the last run changed, in reverse. -> None, or what's wrong"""
        with self.lock:
            r = self.run_state
            if not r or not r["done"]:
                return "Nothing to undo yet." if not r else "Wait for it to finish, or Stop it first."
            if r.get("undone"):
                return "That's already been undone."
            if time.time() - r["done_at"] > UNDO_FOR:
                return "Undo is only for 10 minutes after a run."
            todo = [u for u in reversed(r["undo"]) if u["can"]]
            r["undone"] = True
        errs = []
        for u in todo:
            e = u["run"]()
            if e:
                errs.append("%s: %s" % (u["label"], e))
        self.log.info("macros: %s undid %s", who, r["name"])
        return "Some of it couldn't be put back. " + " ".join(errs) if errs else None
