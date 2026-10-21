"""Slide cues: audio changes when a ProPresenter slide goes live.

Each cue says WHEN (a presentation goes live, a slide group such as "Chorus", or a slide label), WHAT
(channels on or off, faders to a level; each with a fade time, so "on" fades in and "off" fades out, and
one channel fading out while another fades in is a crossfade) and HOW it runs:
  ask      - asks the sound desk, runs only when someone taps Run
  count3/5/10 - a countdown with Cancel (5 seconds unless the cue says otherwise)
  now      - runs straight away, with Undo (only an Admin can choose this)
The cues are kept here so every device shows the same ones. With the Wing connected and set to Control,
cues move the desk itself (the fades run on the C3 Life server); otherwise the app's mixer shows what
would happen.
"""

import json
import os
import threading
import urllib.parse
import urllib.request

HOW = ("ask", "count3", "count5", "count10", "now")
WHEN = ("presentation", "group", "label")
ACTS = ("on", "off", "fader")
FADES = (0, 1, 2, 3, 5, 10)

DEFAULT = {
    "enabled": True,
    "default_how": "count5",
    "cues": [
        {"id": "c1", "name": "Pastor mic on for the Message", "on": True, "when": {"kind": "presentation", "text": "Message"}, "how": "count5",
         "do": [{"ch": 15, "act": "on", "fade": 2}, {"ch": 1, "act": "fader", "db": -10.0, "fade": 3}]},
        {"id": "c2", "name": "Lead Vox up for the bridge", "on": True, "when": {"kind": "group", "text": "Bridge"}, "how": "count5",
         "do": [{"ch": 1, "act": "fader", "db": 2.0, "fade": 2}]},
        {"id": "c3", "name": "Walk-in music off at the Welcome", "on": True, "when": {"kind": "presentation", "text": "Welcome"}, "how": "ask",
         "do": [{"ch": 10, "act": "off", "fade": 3}]},
    ],
}


def valid(d):
    """'' if the cue settings are fine, else what's wrong."""
    if not isinstance(d, dict) or not isinstance(d.get("cues"), list) or len(d["cues"]) > 100:
        return "Cues not understood."
    if not isinstance(d.get("enabled"), bool) or d.get("default_how") not in HOW[:4]:
        return "Settings not understood."
    ids = set()
    for c in d["cues"]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] or len(c["id"]) > 40 or c["id"] in ids:
            return "Each cue needs its own id."
        ids.add(c["id"])
        if not isinstance(c.get("name"), str) or not c["name"].strip() or len(c["name"]) > 80:
            return "Each cue needs a name (up to 80 letters)."
        if not isinstance(c.get("on"), bool) or c.get("how") not in HOW:
            return "%s: how it runs wasn't understood." % c["name"]
        w = c.get("when")
        if not isinstance(w, dict) or w.get("kind") not in WHEN or not isinstance(w.get("text"), str) or not w["text"].strip() or len(w["text"]) > 80:
            return "%s: say which presentation, slide group or label sets it off." % c["name"]
        acts = c.get("do")
        if not isinstance(acts, list) or not 1 <= len(acts) <= 16:
            return "%s: add at least one channel change." % c["name"]
        for a in acts:
            if not isinstance(a, dict) or not isinstance(a.get("ch"), int) or isinstance(a.get("ch"), bool) or not 1 <= a["ch"] <= 48 or a.get("act") not in ACTS:
                return "%s: a channel change wasn't understood." % c["name"]
            if a["act"] == "fader":
                db = a.get("db")
                if not isinstance(db, (int, float)) or isinstance(db, bool) or not -90 <= db <= 10:
                    return "%s: a fader level must be between -90 and +10 dB." % c["name"]
            if a.get("fade", 0) not in FADES or isinstance(a.get("fade"), bool):  # on = fade in, off = fade out
                return "%s: a fade must be 0, 1, 2, 3, 5 or 10 seconds." % c["name"]
    return ""


class Cues:
    def __init__(self, data_dir):
        self.file = os.path.join(data_dir, "cues.json")
        self.lock = threading.Lock()

    def get(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            if not valid(d):
                return d
        except (OSError, ValueError):
            pass
        return json.loads(json.dumps(DEFAULT))

    def save(self, d):
        with self.lock:
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.file)

    @staticmethod
    def matches(cue, slide):
        """Does this slide (from ProPresenter: presentation name, slide group, slide label) set the cue off?"""
        w = cue["when"]
        have = {"presentation": slide.get("presentation"), "group": slide.get("group"), "label": slide.get("label")}.get(w["kind"]) or ""
        return cue["on"] and have.strip().lower() == w["text"].strip().lower()


def _pp(cfg, path):
    url = (cfg.get("propresenter") or "").rstrip("/") + path
    with urllib.request.urlopen(url, timeout=4) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def check(d, cfg, wing=None):
    """Run check: is everything the slide cues need there? Reads ProPresenter's playlist; changes nothing.
    Cues are only saved when nothing comes back False (None is for information: off, or the Wing watching only)."""
    lines, names = [], {"presentation": set(), "group": set(), "label": set()}
    shown = {"presentation": {}, "group": {}, "label": {}}
    try:
        a = _pp(cfg, "/v1/playlist/active") or {}
        pid = ((a.get("presentation") or {}).get("playlist") or {}).get("uuid") or ""
        if not pid:
            raise ValueError("no playlist")
        pl = _pp(cfg, "/v1/playlist/" + urllib.parse.quote(pid)) or {}
        for it in pl.get("items") or []:
            nm = (it.get("id") or {}).get("name", "")
            if it.get("type") == "presentation":
                names["presentation"].add(nm.lower()); shown["presentation"][nm.lower()] = nm
                uid = (it.get("presentation_info") or {}).get("presentation_uuid")
                if uid:
                    pres = (_pp(cfg, "/v1/presentation/" + urllib.parse.quote(uid)) or {}).get("presentation") or {}
                    for g in pres.get("groups") or []:
                        gn = g.get("name") or ""
                        if gn:
                            names["group"].add(gn.lower()); shown["group"].setdefault(gn.lower(), "%s (%s)" % (gn, nm))
                        for sl in g.get("slides") or []:
                            lb = sl.get("label") or ""
                            if lb:
                                names["label"].add(lb.lower()); shown["label"].setdefault(lb.lower(), "%s (%s)" % (lb, nm))
        lines.append({"ok": True, "what": "ProPresenter", "text": "Connected. Today’s playlist has %d presentations." % len(names["presentation"])})
        pp = True
    except (OSError, ValueError):
        lines.append({"ok": False, "what": "ProPresenter", "text": "Didn’t answer, so no slide can set a cue off. Check the Mac mini and ProPresenter › Settings › Network."})
        pp = False
    ws = wing.status() if wing else {"status": "off", "mode": "off"}
    if ws["status"] == "connected" and ws["mode"] == "control":
        lines.append({"ok": True, "what": "Wing", "text": "Connected and in control: cues move the desk itself."})
    elif ws["status"] == "connected":
        lines.append({"ok": None, "what": "Wing", "text": "Connected but watching only, so cues move the app’s mixer, not the desk. An Admin can switch the Wing to Control on the Mixer screen."})
    else:
        lines.append({"ok": None, "what": "Wing", "text": "Not connected, so cues move the app’s mixer only. Set the Wing’s address on the Mixer screen."})
    if not d.get("enabled"):
        lines.append({"ok": None, "what": "Slide cues", "text": "Switched off, so none will run."})
    kinds = {"presentation": "presentation", "group": "slide group", "label": "slide label"}
    for c in d.get("cues", []):
        w = c["when"]
        if not c["on"]:
            lines.append({"ok": None, "what": c["name"], "text": "Off, so it won’t run."})
            continue
        bad = [a for a in c["do"] if not 1 <= a["ch"] <= 48]
        if bad:
            lines.append({"ok": False, "what": c["name"], "text": "Channel %d doesn’t exist on the Wing." % bad[0]["ch"]})
        elif not pp:
            lines.append({"ok": None, "what": c["name"], "text": "Can’t check without ProPresenter."})
        elif w["text"].strip().lower() in names[w["kind"]]:
            lines.append({"ok": True, "what": c["name"], "text": "Will run when %s goes live." % shown[w["kind"]][w["text"].strip().lower()]})
        else:
            near = [v for k, v in shown[w["kind"]].items() if k[:3] == w["text"].strip().lower()[:3]]
            lines.append({"ok": False, "what": c["name"], "text": "No %s called “%s” in today’s playlist%s." % (kinds[w["kind"]], w["text"], (". Did you mean " + near[0] + "?") if near else "")})
    return {"ok_all": all(x["ok"] is not False for x in lines), "lines": lines}
