"""Lights: Lightkey on the lighting Mac, run from C3 Life.

Lightkey stays the lighting desk: its fixture library, its Art-Net / sACN output, its cues and its Preset Palette all
live on the Mac. C3 Life sends Lightkey OSC messages (UDP, port 21600, Lightkey › Settings › External Control ›
Enable OSC) so anyone with Lights control can fire cues, palette presets and looks from a phone or iPad, set the
master, tap the tempo, and program the moving heads (pan, tilt, colour, gobo, shutter, zoom...) without the Mac.

What C3 Life keeps (data/lights.json), because Lightkey doesn't send anything back over OSC:
  cues      Lightkey cues, by page and name, in running order (Go / Back)
  presets   Lightkey Preset Palette items, by their group path ("Movers/Colors/Indigo")
  fixtures  Lightkey fixtures, by short name ("F4"), with what they are and where they hang (for the stage picture)
  looks     programmed here: a list of fixture settings, sent as Lightkey fixture overrides
and data/lightkey.json for the link: the Mac's address, Off or Control, which page, how Go works.

Addresses are the ones in the Lightkey User Guide (chapter 15, "OSC addresses"). Nothing has been sent to a real
Lightkey yet: check each kind once with Lightkey's Window › External Control Log open.
"""

import json
import os
import re
import socket
import threading
import time
import uuid

import wing as wing_mod  # its OSC encoder

PORT = 21600
KINDS = ("mover", "wash", "par", "dimmer", "hazer")
SETTINGS = {"ip": "", "port": PORT, "mode": "off", "page": "selected", "go": "app", "replace": True, "mac": "Lighting Mac"}
EMPTY = {"cues": [], "presets": [], "fixtures": [], "looks": []}
LIMITS = {"cues": 200, "presets": 200, "fixtures": 128, "looks": 100}
MAX_FADE = 60  # seconds, for the programmer and looks
STEP = 0.05    # a fade sends the new value 20 times a second

# Fixture properties C3 Life can set, as Lightkey names them, with what each value may be.
# ("n", lo, hi) a number; ("i", lo, hi) a whole number; ("s", options) one of those words; "hex" an RGB colour.
SHUTTER = ("open", "closed", "strobe", "randomStrobe", "pulseOpening", "pulseClosing", "lightningStrobe")
PRISMS = ("none", "threeFacet", "fiveFacet", "eightFacet", "frost")
PROPS = {
    "dimmer": ("n", 0, 1), "color": "hex", "colorTemperature": ("n", 1000, 12000),
    "color1": ("i", 0, 63), "gobo1": ("i", 0, 63), "gobo2": ("i", 0, 63),
    "panAngle": ("n", -720, 720), "tiltAngle": ("n", -360, 360),
    "zoomAngle": ("n", 1, 90), "focus": ("n", 0, 1), "irisSize": ("n", 0, 1), "frostAmount": ("n", 0, 1),
    "shutterState": ("s", SHUTTER), "strobeSpeed": ("n", 0, 30), "prismType1": ("s", PRISMS),
    "fogAmount": ("n", 0, 1), "on": ("s", ("on", "off")),
}
# These slide during a fade. Wheel slots, gobos, the shutter and the prism can't, so they change at the start.
FADES = {"dimmer", "color", "colorTemperature", "panAngle", "tiltAngle", "zoomAngle", "focus", "irisSize", "frostAmount", "fogAmount"}
# what to clear in Lightkey for each property (".../overrides/<name>/clear")
CLEARS = {"dimmer": "dimmer", "color": "color", "colorTemperature": "color", "color1": "colorWheel1", "gobo1": "goboWheel1",
          "gobo2": "goboWheel2", "panAngle": "position", "tiltAngle": "position", "zoomAngle": "zoom", "focus": "focus",
          "irisSize": "iris", "frostAmount": "frost", "shutterState": "shutter", "strobeSpeed": "shutter",
          "prismType1": "prismWheel1", "fogAmount": "fog", "on": "dimmer"}


def osc_part(name):
    """A Lightkey name as one part of an OSC address: spaces become _, and any character OSC can't carry
    (#*,/?[]{} and anything that isn't plain ASCII) becomes ?, which Lightkey matches to any one character."""
    return "".join("_" if c == " " else c if 33 <= ord(c) <= 126 and c not in "#*,/?[]{}" else "?" for c in name.strip())


def palette_addr(path):
    """"Movers/Colors/Indigo" -> /palette/Movers/Colors/Indigo. An empty part keeps Lightkey's // (any group below)."""
    parts = [p.strip() for p in path.strip().strip("/").split("/")] if not path.strip().startswith("//") else [""] + [p.strip() for p in path.strip()[2:].split("/")]
    return "/palette/" + "/".join(osc_part(p) if p else "" for p in parts)


def _txt(v, n):
    return isinstance(v, str) and 0 < len(v.strip()) <= n and not any(ord(c) < 32 for c in v)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid_value(prop, v, fx=None):
    """None if `v` is a good value for `prop` (on this fixture, for pan and tilt), else why not."""
    k = PROPS.get(prop)
    if k is None:
        return "Lightkey has no property %r here." % prop
    if k == "hex":
        return None if isinstance(v, str) and re.fullmatch(r"#?[0-9A-Fa-f]{6}", v) else "A colour is like #FF8800."
    if k[0] == "s":
        return None if v in k[1] else "%s is one of: %s." % (prop, ", ".join(k[1]))
    if not _num(v) or (k[0] == "i" and int(v) != v):
        return "%s needs a number." % prop
    lo, hi = k[1], k[2]
    if fx and prop in ("panAngle", "tiltAngle"):
        lo, hi = fx["pan" if prop == "panAngle" else "tilt"]
    if not lo <= v <= hi:
        return "%s is %g to %g." % (prop, lo, hi)
    return None


def valid_settings(d):
    if not isinstance(d, dict):
        return "Settings not understood."
    ip = d.get("ip")
    if not isinstance(ip, str) or (ip and not re.fullmatch(r"[A-Za-z0-9.\-:]{1,64}", ip)):
        return "The lighting Mac's address is an IP address like 192.168.10.32, or a name like events-mac.local."
    if not isinstance(d.get("port"), int) or isinstance(d.get("port"), bool) or not 1 <= d["port"] <= 65535:
        return "The port is a number (Lightkey uses 21600)."
    if d.get("mode") not in ("off", "control"):
        return "The Lightkey link is Off or Control."
    if d["mode"] == "control" and not ip:
        return "Put in the lighting Mac's address first."
    if not _txt(d.get("page"), 60):
        return "Which Live page in Lightkey? (selected = whichever page is showing on the Mac)"
    if d.get("go") not in ("app", "lightkey"):
        return "Go runs C3 Life's cue list, or Lightkey's own cuelist."
    if not isinstance(d.get("replace"), bool) or not _txt(d.get("mac"), 40):
        return "Settings not understood."
    return None


def valid_show(d):
    """None if this is a show C3 Life can keep, else why not."""
    if not isinstance(d, dict) or any(not isinstance(d.get(k), list) for k in EMPTY):
        return "Show not understood."
    for k, n in LIMITS.items():
        if len(d[k]) > n:
            return "Up to %d %s." % (n, k)
    ids = set()

    def new_id(x):
        if not isinstance(x, dict) or not isinstance(x.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", x["id"]) or x["id"] in ids:
            return False
        ids.add(x["id"])
        return True
    fxs = {}
    for f in d["fixtures"]:
        if not new_id(f) or not _txt(f.get("name"), 40):
            return "Each fixture needs its short name from Lightkey (like F4)."
        if f["name"] in fxs:
            return "Two fixtures are both called %s." % f["name"]
        if f.get("kind") not in KINDS:
            return "%s: mover, wash, par, dimmer or hazer." % f["name"]
        if not _txt(f.get("label", f["name"]), 40) or not _txt(f.get("group", "All"), 30):
            return "%s: its label or group isn't right." % f["name"]
        for a in ("x", "y"):
            if not _num(f.get(a)) or not 0 <= f[a] <= 100:
                return "%s: where it hangs is 0 to 100 across and down." % f["name"]
        for a, top in (("pan", 720), ("tilt", 360)):
            r = f.get(a)
            if not (isinstance(r, list) and len(r) == 2 and all(_num(x) for x in r) and -top <= r[0] < r[1] <= top):
                return "%s: its %s range is two angles, like -270 and 270." % (f["name"], a)
        for a in ("wheel", "gobos"):
            if not isinstance(f.get(a), list) or len(f[a]) > 32 or not all(_txt(x, 30) for x in f[a]):
                return "%s: up to 32 %s, each named." % (f["name"], "colours on the wheel" if a == "wheel" else "gobos")
        fxs[f["name"]] = f
    for c in d["cues"]:
        if not new_id(c) or not _txt(c.get("name"), 60):
            return "Each cue needs its name, as Lightkey has it."
        if not _txt(c.get("page", "selected"), 60):
            return "%s: which Live page is it on?" % c["name"]
        if c.get("fade") is not None and (not _num(c["fade"]) or not 0 <= c["fade"] <= 600):
            return "%s: the fade is 0 to 600 seconds, or blank for the cue's own." % c["name"]
        if c.get("color") not in (None, "") and not (isinstance(c["color"], str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", c["color"])):
            return "%s: its tile colour is like #FFB25B." % c["name"]
        if c.get("look") not in (None, "") and not isinstance(c["look"], str):
            return "%s: not understood." % c["name"]
    for p in d["presets"]:
        if not new_id(p) or not _txt(p.get("name"), 40) or not _txt(p.get("path"), 160):
            return "Each palette preset needs a name and its place in the Preset Palette (like Movers/Colors/Indigo)."
        if p.get("color") not in (None, "") and not (isinstance(p["color"], str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", p["color"])):
            return "%s: its swatch colour is like #4B0082." % p["name"]
        if not _txt(p.get("group", "Palette"), 30):
            return "%s: its group isn't right." % p["name"]
    for lk in d["looks"]:
        if not new_id(lk) or not _txt(lk.get("name"), 40):
            return "Each look needs a name."
        sets = lk.get("sets")
        if lk.get("fade") is not None and (not _num(lk["fade"]) or not 0 <= lk["fade"] <= MAX_FADE):
            return "%s: the fade is 0 to %d seconds." % (lk["name"], MAX_FADE)
        if not isinstance(sets, list) or not 0 < len(sets) <= 400:
            return "%s: a look has 1 to 400 settings." % lk["name"]
        for st in sets:
            if not isinstance(st, dict) or st.get("fx") not in fxs:
                return "%s: a setting is for a fixture that isn't in the list." % lk["name"]
            e = valid_value(st.get("prop"), st.get("value"), fxs[st["fx"]])
            if e:
                return "%s, %s: %s" % (lk["name"], st["fx"], e)
    looks = {lk["id"] for lk in d["looks"]}
    for c in d["cues"]:
        if c.get("look") and c["look"] not in looks:
            return "%s: that look has gone." % c["name"]
    return None


def _value_args(prop, v):
    if PROPS[prop] == "hex":
        return [v.lstrip("#").lower()]  # as a string, as Lightkey asks
    if PROPS[prop][0] == "i":
        return [int(v)]
    if PROPS[prop][0] == "s":
        return [v]
    return [float(v)]


def _between(prop, a, b, k):
    """The value a fraction k of the way from a to b."""
    if PROPS[prop] == "hex":
        x, y = int(a.lstrip("#"), 16), int(b.lstrip("#"), 16)
        return "#%02x%02x%02x" % tuple(round(((x >> s) & 255) + (((y >> s) & 255) - ((x >> s) & 255)) * k) for s in (16, 8, 0))
    return a + (b - a) * k


def _fade_of(act):
    f = act.get("fade", 0) if isinstance(act, dict) else 0
    return f if _num(f) and 0 <= f <= MAX_FADE else None


class Lightkey:
    def __init__(self, data_dir, cfg=None, log=None):
        self.dir = data_dir
        self.log = log
        self.lock = threading.Lock()
        self.settings = dict(SETTINGS, **self._read("lightkey.json", {}))
        if valid_settings(self.settings):
            self.settings = dict(SETTINGS)
        show = dict(EMPTY, **self._read("lights.json", {}))
        self.show = show if not valid_show(show) else dict(EMPTY)
        # What C3 Life last sent. Lightkey doesn't answer over OSC, so someone pressing a cue on the Mac won't show here.
        self.live = None
        self.master = 1.0
        self.blackout = False
        self.bpm = None
        self.overrides = {}  # fixture name -> {prop: value}
        self.presets_on = []
        self.sent = []
        self.fades = {}  # (fixture, prop) -> [from, to, start, seconds]
        self.fader = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # The lighting Mac as Bonjour names it, so the network scan can follow it to a new address; and whether the
        # last scan saw it (Lightkey doesn't answer OSC, so its Bonjour announcement is the only sign it's there).
        self.bonjour = self._read("lightkey-seen.json", {}).get("name", "")
        self.seen = None

    def found(self, finds):
        """The network scan's finds (kind "lightkey", from Bonjour). -> changes in words. Fills in the address when there's
        exactly one Lightkey and none is set (it stays Off: an Admin turns Control on, which asks first), and follows the one
        set up to a new address by its Bonjour name."""
        lk = [f for f in finds if f.get("kind") == "lightkey"]
        changes = []
        with self.lock:
            cur = self.settings
            at = next((f for f in lk if f["ip"] == cur["ip"]), None)
            same = [f for f in lk if self.bonjour and f.get("name") == self.bonjour]
            pick = None
            if at:
                pick = at
            elif cur["ip"] and len(same) == 1:
                pick = same[0]
                changes.append("Lightkey (%s) moved from %s to %s, so C3 Life followed it" % (pick["name"], cur["ip"], pick["ip"]))
            elif not cur["ip"] and len(lk) == 1:
                pick = lk[0]
                changes.append("Found Lightkey (%s) at %s. It's Off until an Admin turns Control on (Lights › Show setup)" % (pick["name"], pick["ip"]))
            self.seen = bool(at or pick) if cur["ip"] or pick else None
            if pick and (pick["ip"] != cur["ip"] or pick.get("name", "") != self.bonjour):
                self.settings = dict(cur, ip=pick["ip"])
                self.bonjour = pick.get("name", "")
                self._write("lightkey.json", self.settings)
                self._write("lightkey-seen.json", {"name": self.bonjour})
        for c in changes:
            if self.log:
                self.log.info("lights: %s", c)
        return changes

    def _read(self, name, dflt):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else dflt
        except (OSError, ValueError):
            return dflt

    def _write(self, name, d):
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(p + ".tmp", p)

    def save_settings(self, d):
        d = {k: d[k] for k in SETTINGS}
        with self.lock:
            self.settings = d
            self._write("lightkey.json", d)

    def save_show(self, d):
        d = {k: d[k] for k in EMPTY}
        with self.lock:
            self.show = d
            if self.live and not any(c["id"] == self.live for c in d["cues"]):
                self.live = None
            self._write("lights.json", d)

    def view(self):
        with self.lock:
            cues = self.show["cues"]
            i = next((n for n, c in enumerate(cues) if c["id"] == self.live), None)
            return {"settings": dict(self.settings), "show": json.loads(json.dumps(self.show)),
                    "state": {"live": self.live, "next": cues[i + 1]["id"] if i is not None and i + 1 < len(cues) else (cues[0]["id"] if cues and i is None else None),
                              "master": self.master, "blackout": self.blackout, "bpm": self.bpm,
                              "overrides": json.loads(json.dumps(self.overrides)), "presets_on": list(self.presets_on),
                              "fading": [{"fx": k[0], "prop": k[1], "ends": round(f[2] + f[3], 2)} for k, f in self.fades.items()]},
                    "sent": list(self.sent[-20:]), "on": self.settings["mode"] == "control"}

    # ---- the messages ---------------------------------------------------------------------------

    def _page(self, c):
        return osc_part(c.get("page") or self.settings["page"] or "selected")

    def _cue_msgs(self, c, on=True, fade="own"):
        f = c.get("fade") if fade == "own" else fade  # None: Lightkey's own fade time for it
        a = "/live/%s/cue/%s/%s" % (self._page(c), osc_part(c["name"]), "activate" if on else "deactivate")
        return [(a, [] if f is None else [float(f)])]

    def _look_msgs(self, look):
        return [("/fixture/%s/overrides/%s" % (osc_part(st["fx"]), st["prop"]), _value_args(st["prop"], st["value"])) for st in look["sets"]]

    def plan(self, act):
        """What doing `act` would send: (messages, what changes here, error). Nothing is sent."""
        do, show, st = act.get("do"), self.show, {}
        cues = show["cues"]
        fx_by = {f["name"]: f for f in show["fixtures"]}

        def cue_for(cid):
            return next((c for c in cues if c["id"] == cid), None)

        def fire(c):
            msgs = []
            prev = cue_for(self.live) if self.live else None
            if self.settings["replace"] and prev and prev["id"] != c["id"]:
                msgs += self._cue_msgs(prev, on=False, fade=c.get("fade"))  # it lets go as the next one comes up
            msgs += self._cue_msgs(c)
            look = next((lk for lk in show["looks"] if lk["id"] == c.get("look")), None) if c.get("look") else None
            ch = {"live": c["id"]}
            if look:
                msgs += self._look_msgs(look)
                ch.update(look=look, fade=look.get("fade") or 0)
            return msgs, ch, None

        if do == "cue":
            c = cue_for(act.get("id"))
            return fire(c) if c else ([], {}, "That cue has gone.")
        if do in ("go", "back"):
            if self.settings["go"] == "lightkey":
                return [("/live/nextCue" if do == "go" else "/live/previousCue", [])], {}, None
            if not cues:
                return [], {}, "There are no cues yet. Add Lightkey's cues in Show setup."
            i = next((n for n, c in enumerate(cues) if c["id"] == self.live), None)
            j = (0 if i is None else i + 1) if do == "go" else (0 if i is None else i - 1)
            if not 0 <= j < len(cues):
                return [], {}, "That's the end of the cue list." if do == "go" else "That's the first cue."
            return fire(cues[j])
        if do == "release":
            c = cue_for(self.live)
            return (self._cue_msgs(c, on=False) if c else []), {"live": None}, None
        if do == "preset":
            p = next((x for x in show["presets"] if x["id"] == act.get("id")), None)
            if not p:
                return [], {}, "That preset has gone."
            on = act.get("on", True)
            if not isinstance(on, bool):
                return [], {}, "Bad request"
            return [(palette_addr(p["path"]) + ("/activate" if on else "/deactivate"), [])], {"preset": (p["id"], on)}, None
        if do == "look":
            look = next((lk for lk in show["looks"] if lk["id"] == act.get("id")), None)
            if not look:
                return [], {}, "That look has gone."
            f = _fade_of(act) if "fade" in act else look.get("fade") or 0
            if f is None:
                return [], {}, "The fade is 0 to %d seconds." % MAX_FADE
            return self._look_msgs(look), {"look": look, "fade": f}, None
        if do == "master":
            v = act.get("value")
            if not _num(v) or not 0 <= v <= 1:
                return [], {}, "The master is 0 to 100%."
            return ([] if self.blackout else [("/output/master", [float(v)])]), {"master": float(v)}, None
        if do == "blackout":
            on = act.get("on")
            if not isinstance(on, bool):
                return [], {}, "Bad request"
            # Lightkey has no blackout message: the master goes to 0, and back to where it was.
            return [("/output/master", [0.0 if on else float(self.master)])], {"blackout": on}, None
        if do == "tap":
            return [("/beat/tap", [])], {}, None
        if do == "tempo":
            b = act.get("bpm")
            if not _num(b) or not 30 <= b <= 300:
                return [], {}, "The tempo is 30 to 300 BPM."
            return [("/beat/tempo", [float(b)])], {"bpm": float(b)}, None
        if do == "fixture":
            names, prop, v = act.get("fx"), act.get("prop"), act.get("value")
            if not isinstance(names, list) or not names or any(n not in fx_by for n in names):
                return [], {}, "Pick the fixtures first."
            for n in names:
                e = valid_value(prop, v, fx_by[n])
                if e:
                    return [], {}, "%s: %s" % (n, e)
            f = _fade_of(act)
            if f is None:
                return [], {}, "The fade is 0 to %d seconds." % MAX_FADE
            return [("/fixture/%s/overrides/%s" % (osc_part(n), prop), _value_args(prop, v)) for n in names], {"set": (names, prop, v), "fade": f}, None
        if do == "clear":
            names, prop = act.get("fx"), act.get("prop")
            if names == "all":
                return [("/fixture/*/overrides/clear", [])], {"clear": ("all", None)}, None
            if not isinstance(names, list) or not names or any(n not in fx_by for n in names) or (prop is not None and prop not in CLEARS):
                return [], {}, "Pick the fixtures first."
            tail = "/overrides/clear" if prop is None else "/overrides/%s/clear" % CLEARS[prop]
            return [("/fixture/%s%s" % (osc_part(n), tail), []) for n in names], {"clear": (names, prop)}, None
        return [], {}, "Bad request"

    def do(self, act, who=""):
        """Send it to Lightkey. -> (ok, error, messages)."""
        with self.lock:
            msgs, change, err = self.plan(act if isinstance(act, dict) else {})
            if err:
                return False, err, []
            if self.settings["mode"] != "control":
                return False, "The Lightkey link is off. An Admin can turn it on in Lights › Show setup.", msgs
            sets = [(n, change["set"][1], change["set"][2]) for n in change["set"][0]] if "set" in change else \
                [(st["fx"], st["prop"], st["value"]) for st in change["look"]["sets"]] if "look" in change else []
            fade, now_msgs, later = change.get("fade") or 0, msgs, []
            first = msgs[:len(msgs) - len(sets)]  # a cue's own messages, before its look's
            if fade > 0:
                # A fade starts from what C3 Life last set. Lightkey doesn't say what it's showing, so a property
                # C3 Life hasn't set yet (since the last Clear) changes straight away.
                now_msgs = list(first)
                for (n, prop, v), m in zip(sets, msgs[len(first):]):
                    cur = self._now(n, prop)
                    if prop in FADES and cur is not None and cur != v:
                        later.append((n, prop, cur, v))
                    else:
                        now_msgs.append(m)
            for n, prop, _ in sets:
                self.fades.pop((n, prop), None)
            try:
                for a, args in now_msgs:
                    self._send(a, args)
            except OSError as e:
                return False, "Couldn't reach %s (%s)." % (self.settings["mac"], e.strerror or e), msgs
            t0 = time.time()
            for n, prop, a, b in later:
                self.fades[(n, prop)] = [a, b, t0, float(fade)]
            if later and not (self.fader and self.fader.is_alive()):
                self.fader = threading.Thread(target=self._fade_loop, daemon=True)
                self.fader.start()
            self._apply(change)
            now = round(time.time(), 2)
            self.sent += [dict({"t": now, "addr": a, "args": args}, **({"fade": fade} if fade and (a, args) not in now_msgs else {})) for a, args in msgs]
            del self.sent[:-40]
        if self.log:
            self.log.info("lights: %s sent %s", who or "someone", "; ".join(a + "".join(" %s" % x for x in args) for a, args in msgs)[:400])
        return True, None, msgs

    def close(self):
        with self.lock:
            self.fades = {}
        self.sock.close()

    def _send(self, a, args):
        self.sock.sendto(wing_mod.osc(a, *args), (self.settings["ip"], int(self.settings["port"])))

    def _now(self, n, prop):
        """What this property is at right now, as far as C3 Life knows (part way through, if it's fading)."""
        f = self.fades.get((n, prop))
        if f:
            return _between(prop, f[0], f[1], min(1.0, (time.time() - f[2]) / f[3]))
        return self.overrides.get(n, {}).get(prop)

    def _fade_loop(self):
        while True:
            time.sleep(STEP)
            with self.lock:
                if not self.fades:
                    return
                t = time.time()
                for (n, prop), (a, b, t0, d) in list(self.fades.items()):
                    k = min(1.0, (t - t0) / d)
                    v = _between(prop, a, b, k)
                    if self.settings["mode"] == "control":
                        try:
                            self._send("/fixture/%s/overrides/%s" % (osc_part(n), prop), _value_args(prop, v))
                        except OSError:
                            pass
                    if k >= 1.0:
                        del self.fades[(n, prop)]

    def _apply(self, ch):
        if "live" in ch:
            self.live = ch["live"]
        if "master" in ch:
            self.master = ch["master"]
        if "blackout" in ch:
            self.blackout = ch["blackout"]
        if "bpm" in ch:
            self.bpm = ch["bpm"]
        if "preset" in ch:
            pid, on = ch["preset"]
            self.presets_on = [x for x in self.presets_on if x != pid] + ([pid] if on else [])
        if "look" in ch:
            for st in ch["look"]["sets"]:
                self.overrides.setdefault(st["fx"], {})[st["prop"]] = st["value"]
        if "set" in ch:
            names, prop, v = ch["set"]
            for n in names:
                self.overrides.setdefault(n, {})[prop] = v
        if "clear" in ch:
            names, prop = ch["clear"]
            for k in list(self.fades):
                if names == "all" or (k[0] in names and (prop is None or CLEARS[k[1]] == CLEARS[prop])):
                    del self.fades[k]
            if names == "all":
                self.overrides = {}
            for n in (names if isinstance(names, list) else []):
                if prop is None:
                    self.overrides.pop(n, None)
                else:
                    group = {p for p, c in CLEARS.items() if c == CLEARS[prop]}
                    self.overrides[n] = {p: v for p, v in self.overrides.get(n, {}).items() if p not in group}
                    if not self.overrides[n]:
                        del self.overrides[n]


def new_id():
    return uuid.uuid4().hex[:10]


def find(timeout=2.0, browse=None):
    """Lightkey on the network: it offers its OSC over Bonjour. -> [{"name","ip","port"}]"""
    import scan as scan_mod
    browse = browse or scan_mod.mdns_browse
    out = []
    for ip, d in sorted(browse(("_osc._udp.local",), timeout=timeout).items()):
        inst = d["services"].get("_osc._udp.local", "")
        out.append({"name": inst.split("._osc")[0] or ip, "ip": ip, "port": PORT, "lightkey": "lightkey" in inst.lower()})
    return out
