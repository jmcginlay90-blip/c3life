"""The live link to the Behringer Wing.

Names, colours, faders, pans, mutes, input gain and phantom power go over the Wing's OSC server (UDP port 2223).
The C3 Life server asks for every value every few seconds and also subscribes, so changes made on the desk
(or by Wing Co-Pilot, Mixing Station...) show within a moment. The Wing gives one OSC subscription at a
time, to whoever asked last; if another app takes it, the regular asking keeps every screen right.

Meters come from the Wing's native protocol: a TCP connection to port 2222 asks for them, and the levels
arrive by UDP (8 values a channel; values 2 and 3 are the channel's left/right in 1/256 dB).

It starts in Watch only: it reads the desk but sends nothing. An Admin switches it to Control (the app
asks first). Phantom power and renames are asked again in the app before they're sent; faders, mutes and
gain move straight away, with a question for a jump bigger than 6 dB.

Settings live in data/wing.json: {"ip": "192.168.10.20", "mode": "off" | "watch" | "control"}.
"""

import collections
import json
import os
import socket
import struct
import threading
import time

import desk as desk_mod

KINDS = {"ch": 40, "aux": 8, "bus": 16, "main": 4, "mtx": 8, "dca": 16}
# Outputs C3 Life can patch (for the RTA): the Dante card's 64 and USB's 48. Each output's source is a group and an
# index: /io/out/CRD/12/grp "LCL", /io/out/CRD/12/in 8. These names follow the channel inputs' /in/conn/grp and /in;
# they haven't been read from our Wing yet (the hardware check reads them first), so patching waits until they answer.
OUTS = {"CRD": 64, "USB": 48}
OUT_SRC = {"OFF": 0, "LCL": 8, "AUX": 8, "A": 48, "B": 48, "C": 48, "CRD": 64, "USB": 48, "MAIN": 8, "BUS": 32, "MTX": 16}
PREAMPS = ("LCL", "A", "B", "C")  # local and AES50 stagebox mic inputs: the ones with gain and phantom power
FIELDS = ("fdr", "mute", "name", "col", "pan")
MODES = ("off", "watch", "control")

# A channel's processing (the Wing's own names under /ch/N/...): (kind, low, high). "b" is on/off, "s" is
# read only (which plugin model the channel uses). Read only while someone has the channel open in the app.
_EQ = {}
for _b, _f in (("l", (20, 2000)), ("1", (20, 20000)), ("2", (20, 20000)), ("3", (20, 20000)), ("4", (20, 20000)), ("h", (50, 20000))):
    _EQ.update({"eq/%sg" % _b: ("n", -15, 15), "eq/%sf" % _b: ("n",) + _f, "eq/%sq" % _b: ("n", 0.44, 10)})
PROC = dict({
    "in/set/trim": ("n", -18, 18), "in/set/inv": ("b",),
    "flt/lc": ("b",), "flt/lcf": ("n", 20, 2000), "flt/hc": ("b",), "flt/hcf": ("n", 50, 20000), "flt/mdl": ("s",),
    "gate/on": ("b",), "gate/thr": ("n", -80, 0), "gate/range": ("n", 3, 60), "gate/mdl": ("s",),
    "eq/on": ("b",), "eq/mdl": ("s",),
    "dyn/on": ("b",), "dyn/thr": ("n", -60, 0), "dyn/ratio": ("n", 1.1, 100), "dyn/gain": ("n", -6, 12),
    "dyn/att": ("n", 0, 120), "dyn/rel": ("n", 4, 4000), "dyn/auto": ("b",), "dyn/mdl": ("s",),
}, **_EQ)
# Tap EQ: each channel's own 3-band EQ on its pre-fader send tap (what the in-ears hear, not front of house). It belongs
# to the channel, so it's heard in every monitor mix that takes that channel pre-fader. Read with the channel's processing.
TAP_EQ = {"peq/on": ("b",)}
for _b in "123":
    TAP_EQ.update({"peq/%sg" % _b: ("n", -15, 15), "peq/%sf" % _b: ("n", 20, 20000), "peq/%sq" % _b: ("n", 0.44, 10)})
PROC.update(TAP_EQ)
RTA_TAPS = ("IN", "PREEQ", "POSTEQ", "POST")
# The RTA's source is one index across the strips, in this order (channel 1 is 1, bus 1 is 49). The channels' part is
# confirmed by reading it back; the buses' place after the 8 aux inputs is how the Wing orders its strips, not yet
# tried on our Wing (the screen reads the source back and says when it isn't the mix it asked for).
RTA_BASE = {"ch": 0, "aux": 40, "bus": 48, "main": 64, "mtx": 68}
# a channel's send to a bus: /ch/N/send/B/<field> (an aux input's too: /aux/N/send/B/<field>, kept as "a<N>")
SEND = {"on": "b", "lvl": "n", "mode": "s", "plink": "b", "pan": "n"}
SENDERS = ("ch", "aux")
# A bus's EQ (Wing's standard EQ on a bus: low and high shelves, which can also be a cut, and 6 bands between)
BUS_EQ = {"eq/on": ("b",), "eq/leq": ("s", ("SHV", "PEQ", "CUT")), "eq/heq": ("s", ("SHV", "PEQ", "CUT")),
          "eq/lg": ("n", -15, 15), "eq/lf": ("n", 20, 2000), "eq/lq": ("n", 0.44, 10),
          "eq/hg": ("n", -15, 15), "eq/hf": ("n", 50, 20000), "eq/hq": ("n", 0.44, 10)}
for _b in "123456":
    BUS_EQ.update({"eq/%sg" % _b: ("n", -15, 15), "eq/%sf" % _b: ("n", 20, 20000), "eq/%sq" % _b: ("n", 0.44, 10)})
RATIOS = (1.1, 1.2, 1.3, 1.5, 1.7, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0, 20.0, 50.0, 100.0)


# ---- OSC ---------------------------------------------------------------------------------------

def _str(s):
    b = s.encode("utf-8") + b"\0"
    return b + b"\0" * (-len(b) % 4)


def osc(addr, *args):
    tags, data = ",", b""
    for a in args:
        if isinstance(a, bool) or isinstance(a, int):
            tags, data = tags + "i", data + struct.pack(">i", int(a))
        elif isinstance(a, float):
            tags, data = tags + "f", data + struct.pack(">f", a)
        else:
            tags, data = tags + "s", data + _str(str(a))
    return _str(addr) + _str(tags) + data


def parse(data):
    """OSC packet -> (address, [args]); None if it isn't one."""
    def rd_str(i):
        j = data.index(b"\0", i)
        return data[i:j].decode("utf-8", "replace"), j + 1 + (-(j + 1) % 4)
    try:
        addr, i = rd_str(0)
        if i >= len(data):
            return addr, []
        tags, i = rd_str(i)
        out = []
        for t in tags[1:]:
            if t == "s":
                v, i = rd_str(i)
            elif t == "i":
                v, i = struct.unpack(">i", data[i:i + 4])[0], i + 4
            elif t == "f":
                v, i = struct.unpack(">f", data[i:i + 4])[0], i + 4
            elif t == "b":
                n = struct.unpack(">i", data[i:i + 4])[0]
                v, i = data[i + 4:i + 4 + n], i + 4 + n + (-n % 4)
            else:
                return None
            out.append(v)
        return addr, out
    except (ValueError, struct.error):
        return None


# ---- fader law (the Wing's: 0.75 is 0 dB, 1.0 is +10 dB) ---------------------------------------

def pos_to_db(p):
    p = max(0.0, min(1.0, p))
    if p >= 0.5:
        return p * 40 - 30
    if p >= 0.25:
        return p * 80 - 50
    if p >= 0.0625:
        return p * 160 - 70
    return p * 480 - 90 if p > 0 else -144.0


def db_to_pos(db):
    if db <= -90:
        return 0.0
    if db >= -10:
        return min(1.0, (db + 30) / 40)
    if db >= -30:
        return (db + 50) / 80
    if db >= -60:
        return (db + 70) / 160
    return (db + 90) / 480


def _num(args):
    for a in reversed(args):
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return a
    try:
        return float(args[0])
    except (IndexError, ValueError, TypeError):
        return None


class Wing(desk_mod.Desk):
    """The Behringer Wing: the first desk driver (see desk.py for what a driver provides)."""
    NAME = "Behringer Wing"
    CAN = {k: True for k in desk_mod.CAN}
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.path = os.path.join(data_dir, "wing.json")
        self.osc_port = int(cfg.get("wing_osc_port", 2223))
        self.native_port = int(cfg.get("wing_native_port", 2222))
        self.lock = threading.Lock()
        self.settings = {"ip": cfg.get("wing_ip", ""), "mode": "watch" if cfg.get("wing_ip") else "off"}
        try:
            with open(self.path, encoding="utf-8") as f:
                s = json.load(f)
            if s.get("mode") in MODES:
                self.settings["mode"] = s["mode"]
            if isinstance(s.get("ip"), str):
                self.settings["ip"] = s["ip"]
        except (OSError, ValueError):
            pass
        self.reset()
        self.sock = None
        self.fades = {}
        self.stop = threading.Event()
        self.threads = []

    def reset(self):
        self.version = 0
        self.strips = {k: {n: {} for n in range(1, c + 1)} for k, c in KINDS.items()}
        self.inputs = {}  # "LCL/1" -> {"g": dB, "vph": 0/1}
        self.proc = {}  # channel -> {"eq/1g": 3.0, ...}
        self.hist = {}  # open channel -> recent (t, in, out, gate key, gate GR, dyn key, dyn GR)
        self.rta, self.rta_at, self.rtacfg = [], 0.0, {}
        self.rta_want, self.rta_off = None, 0
        self.sends = {}  # bus -> channel (or "a<N>" for an aux input) -> {"on", "lvl", "mode", "plink", "pan"}
        self.buseq = {}  # bus -> {"eq/on": 1, "eq/1g": -3.0, ...}
        self.bus_focus = {}  # bus -> until when someone has its sends open
        self.outs = {}  # "CRD/12" -> {"grp": "LCL", "in": 8}, read while out_focus is set
        self.out_focus = 0.0
        self.focus = {}  # channel -> until when someone has it open
        self.console = {}
        self.seen = 0.0
        self.meters = {}
        self.meters_at = 0.0

    # ---- settings -------------------------------------------------------------------------------

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f)
        os.replace(tmp, self.path)

    def configure(self, ip=None, mode=None):
        with self.lock:
            if ip is not None:
                ip = ip.strip()
                if ip != self.settings["ip"]:
                    self.settings["ip"] = ip
                    self.reset()
            if mode is not None:
                if mode not in MODES:
                    return "Mode is off, watch or control."
                self.settings["mode"] = mode
                if mode == "off":
                    self.reset()
            self.save()
        return None

    def status(self):
        ip, mode = self.settings["ip"], self.settings["mode"]
        if mode == "off":
            st = "off"
        elif not ip:
            st = "no address"
        elif self.seen and time.time() - self.seen < 6:
            st = "connected"
        else:
            st = "no answer"
        return {"status": st, "mode": mode, "ip": ip, "console": self.console, "version": self.version,
                "seen": round(time.time() - self.seen, 1) if self.seen else None, "meters": time.time() - self.meters_at < 3}

    def state(self):
        with self.lock:
            out = self.status()
            out["strips"] = {k: {str(n): dict(v) for n, v in s.items()} for k, s in self.strips.items()}
            out["inputs"] = {k: dict(v) for k, v in self.inputs.items()}
        return out

    def meter_state(self):
        if time.time() - self.meters_at > 3:
            return {"meters": False}
        with self.lock:
            return {"meters": True, "ch": {str(n): max(v) for n, v in self.meters.items()}, "lr": {str(n): v for n, v in self.meters.items()}}

    # ---- talking to the Wing --------------------------------------------------------------------

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:  # room for a whole round of answers: Windows' own buffer is small and drops the rest
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)
        for fn in (self.rx_loop, self.ask_loop, self.meter_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass

    def send(self, packet):
        ip = self.settings["ip"]
        if not ip or self.settings["mode"] == "off" or not self.sock:
            return False
        try:
            self.sock.sendto(packet, (ip, self.osc_port))
            return True
        except OSError:
            return False

    def queries(self, slow):
        q = []
        for k, c in KINDS.items():
            for n in range(1, c + 1):
                q += ["/%s/%d/fdr" % (k, n), "/%s/%d/mute" % (k, n)] + (["/%s/%d/pan" % (k, n)] if k != "dca" else [])
                if slow:
                    q += ["/%s/%d/name" % (k, n), "/%s/%d/col" % (k, n)]
        for n in range(1, KINDS["ch"] + 1):
            # every 20 s, and every round until the answer has come (a lost reply isn't left for 20 s)
            st = self.strips["ch"].get(n) or {}
            if slow or "src_grp" not in st or "src_in" not in st:
                q += ["/ch/%d/in/conn/grp" % n, "/ch/%d/in/conn/in" % n]
        with self.lock:
            used = sorted(set(self.src(n) for n in range(1, KINDS["ch"] + 1)) - {None})
            fed = sorted(set(self.src_any(n) for n in range(1, KINDS["ch"] + 1)) - {None})
        for key in used:
            q += ["/io/in/%s/g" % key, "/io/in/%s/vph" % key]
        # mono (M), stereo (ST) or mid-side (MS): every 20 s, and every round for an input whose mode hasn't come yet
        q += ["/io/in/%s/mode" % key for key in fed if slow or "mode" not in self.inputs.get(key, {})]
        q += ["/cfg/rta/$src", "/cfg/rta/$tap"]
        return q

    def ask_loop(self):
        last_slow, last_sub, rnd = 0.0, 0.0, 0
        while not self.stop.is_set():
            now = time.time()
            if self.settings["ip"] and self.settings["mode"] != "off":
                if now - last_sub > 8:
                    self.send(osc("/?"))
                    self.send(osc("/*s"))
                    last_sub = now
                slow = now - last_slow > 20 or rnd < 2  # twice at the start, in case a reply was lost
                if slow:
                    last_slow = now
                for b in [k for k, t in list(self.bus_focus.items()) if t > now]:
                    self.ask_bus(b)
                if self.out_focus > now and (slow or rnd % 3 == 0):  # the card and USB outputs, while the RTA routing is open
                    for i, (g, c) in enumerate(OUTS.items()):
                        for n in range(1, c + 1):
                            self.send(osc("/io/out/%s/%d/grp" % (g, n)))
                            self.send(osc("/io/out/%s/%d/in" % (g, n)))
                            if n % 8 == 0:
                                self.stop.wait(0.02)
                for n in [k for k, t in list(self.focus.items()) if t > now]:
                    for a in PROC:  # the open channels: every second, not every 2
                        self.send(osc("/ch/%d/%s" % (n, a)))
                for i, a in enumerate(self.queries(slow)):
                    self.send(osc(a))
                    if i % 10 == 9:
                        self.stop.wait(0.02)  # don't flood the desk (or our own receive buffer)
                rnd += 1
            self.stop.wait(1 if any(t > time.time() for t in list(self.focus.values()) + list(self.bus_focus.values())) else 2)

    def channel(self, n):
        """Someone opened channel n's processing: read it now, and keep reading it for 30 seconds."""
        first = self.focus.get(n, 0) < time.time()
        self.focus[n] = time.time() + 30
        if first:
            for a in PROC:
                self.send(osc("/ch/%d/%s" % (n, a)))
        with self.lock:
            return dict(self.proc.get(n, {}))

    def bus_sends(self, b):
        """Someone opened bus b's sends (sends on faders, a monitor mix): read them now and for 30 seconds."""
        first = self.bus_focus.get(b, 0) < time.time()
        self.bus_focus[b] = time.time() + 30
        if first:
            self.ask_bus(b)
        with self.lock:
            return {str(n): dict(v) for n, v in self.sends.get(b, {}).items()}

    def ask_bus(self, b):
        """Every channel's and aux input's send to bus b, and the bus's EQ."""
        i = 0
        for kind in SENDERS:
            for n in range(1, KINDS[kind] + 1):
                for f in SEND:
                    self.send(osc("/%s/%d/send/%d/%s" % (kind, n, b, f)))
                i += 1
                if i % 4 == 0:
                    self.stop.wait(0.01)
        for a in BUS_EQ:
            self.send(osc("/bus/%d/%s" % (b, a)))

    def bus_eq(self, b):
        with self.lock:
            return dict(self.buseq.get(b, {}))

    def rx_loop(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                time.sleep(0.2)
                continue
            if addr[0] != self.settings["ip"]:
                continue
            p = parse(data)
            if p:
                self.take(*p)

    def take(self, addr, args):
        """One value from the Wing (an answer or a subscription message)."""
        parts = addr.strip("/").split("/")
        with self.lock:
            self.seen = time.time()
            if addr == "/?" and args and isinstance(args[0], str) and args[0].startswith("WING,"):
                t = args[0].split(",")
                self.console = {"name": t[2] if len(t) > 2 else "", "model": t[3] if len(t) > 3 else "", "firmware": t[5] if len(t) > 5 else ""}
                return
            changed = False
            if len(parts) == 3 and parts[0] in KINDS and parts[1].isdigit() and parts[2] in FIELDS:
                s = self.strips[parts[0]].get(int(parts[1]))
                if s is None or not args:
                    return
                f = parts[2]
                if f in ("fdr", "pan"):
                    db = args[2] if len(args) >= 3 and isinstance(args[2], float) else _num(args)
                    if db is None:
                        return
                    v = {"fdr": round(max(-144.0, float(db)), 1)} if f == "fdr" else {"pan": int(round(float(db)))}
                elif f == "name":
                    v = {"name": str(args[0])[:16]}
                else:
                    x = _num(args)
                    if x is None:
                        return
                    v = {f: int(round(x))}
                for k2, x in v.items():
                    if s.get(k2) != x:
                        s[k2], changed = x, True
            elif len(parts) == 3 and parts[:2] == ["cfg", "rta"] and parts[2] in ("$src", "$tap", "rtasrc", "rtatap"):
                if not args:
                    return
                v = str(args[0]) if parts[2] in ("$tap", "rtatap") else _num(args)
                if v is None:
                    return
                v = v if parts[2] in ("$tap", "rtatap") else int(round(v))
                if self.rtacfg.get(parts[2]) != v:
                    self.rtacfg[parts[2]], changed = v, True
                if parts[2] == "$src":
                    self._rta_check()
            elif len(parts) == 4 and parts[0] == "bus" and parts[1].isdigit() and "/".join(parts[2:]) in BUS_EQ:
                b, key, k = int(parts[1]), "/".join(parts[2:]), BUS_EQ["/".join(parts[2:])]
                if not args or not 1 <= b <= KINDS["bus"]:
                    return
                if k[0] == "s":
                    v = str(args[0])
                else:
                    x = args[2] if k[0] == "n" and len(args) >= 3 and isinstance(args[2], float) else _num(args)
                    if x is None:
                        return
                    v = int(round(x)) if k[0] == "b" else round(float(x), 2)
                cur = self.buseq.setdefault(b, {})
                if cur.get(key) != v:
                    cur[key], changed = v, True
            elif len(parts) == 5 and parts[0] in SENDERS and parts[1].isdigit() and parts[2] == "send" and parts[3].isdigit() and parts[4] in SEND:
                n, b, f = int(parts[1]), int(parts[3]), parts[4]
                if not args or n not in self.strips[parts[0]] or not 1 <= b <= KINDS["bus"]:
                    return
                if parts[0] == "aux":
                    n = "a%d" % n
                if SEND[f] == "s":
                    v = str(args[0])
                elif SEND[f] == "b":
                    x = _num(args)
                    if x is None:
                        return
                    v = int(round(x))
                else:
                    x = args[2] if len(args) >= 3 and isinstance(args[2], float) else _num(args)
                    if x is None:
                        return
                    v = round(max(-144.0, float(x)), 1) if f == "lvl" else int(round(float(x)))
                cur = self.sends.setdefault(b, {}).setdefault(n, {})
                if cur.get(f) != v:
                    cur[f], changed = v, True
            elif len(parts) >= 4 and parts[0] == "ch" and parts[1].isdigit() and "/".join(parts[2:]) in PROC:
                key, kind = "/".join(parts[2:]), PROC["/".join(parts[2:])][0]
                if not args or int(parts[1]) not in self.strips["ch"]:
                    return
                if kind == "s":
                    v = str(args[0])
                elif kind == "b":
                    x = _num(args)
                    if x is None:
                        return
                    v = int(round(x))
                else:
                    x = args[2] if len(args) >= 3 and isinstance(args[2], float) else _num(args)
                    if x is None:
                        return
                    v = round(float(x), 3)
                cur = self.proc.setdefault(int(parts[1]), {})
                if cur.get(key) != v:
                    cur[key], changed = v, True
            elif len(parts) == 5 and parts[0] == "ch" and parts[1].isdigit() and parts[2:4] == ["in", "conn"] and parts[4] in ("grp", "in"):
                s = self.strips["ch"].get(int(parts[1]))
                if s is None or not args:
                    return
                v = str(args[0]) if parts[4] == "grp" else _num(args)
                if v is None:
                    return
                k2 = "src_grp" if parts[4] == "grp" else "src_in"
                v = v if k2 == "src_grp" else int(v)
                if s.get(k2) != v:
                    s[k2], changed = v, True
            elif len(parts) == 5 and parts[:2] == ["io", "out"] and parts[2] in OUTS and parts[3].isdigit() and parts[4] in ("grp", "in"):
                if not args:
                    return
                v = str(args[0]) if parts[4] == "grp" else _num(args)
                if v is None:
                    return
                v = v if parts[4] == "grp" else int(v)
                cur = self.outs.setdefault(parts[2] + "/" + parts[3], {})
                if cur.get(parts[4]) != v:
                    cur[parts[4]], changed = v, True
            elif len(parts) == 5 and parts[:2] == ["io", "in"] and parts[4] == "mode":
                if not args:
                    return
                cur = self.inputs.setdefault(parts[2] + "/" + parts[3], {})
                if cur.get("mode") != str(args[0]):
                    cur["mode"], changed = str(args[0]), True
            elif len(parts) == 5 and parts[:2] == ["io", "in"] and parts[4] in ("g", "vph"):
                key = parts[2] + "/" + parts[3]
                x = _num(args) if parts[4] == "vph" else (args[2] if len(args) >= 3 and isinstance(args[2], float) else _num(args))
                if x is None:
                    return
                x = int(round(x)) if parts[4] == "vph" else round(float(x), 1)
                cur = self.inputs.setdefault(key, {})
                if cur.get(parts[4]) != x:
                    cur[parts[4]], changed = x, True
            if changed:
                self.version += 1

    def src_any(self, n):
        """Whatever input feeds channel n ('USB/3' too), or None if it's off."""
        s = self.strips["ch"].get(n) or {}
        if s.get("src_grp") and s.get("src_grp") != "OFF" and s.get("src_in"):
            return "%s/%d" % (s["src_grp"], s["src_in"])
        return None

    def src(self, n):
        """The mic input feeding channel n ('A/3'), or None when it isn't a preamp (USB, card, off...)."""
        s = self.strips["ch"].get(n) or {}
        if s.get("src_grp") in PREAMPS and s.get("src_in"):
            return "%s/%d" % (s["src_grp"], s["src_in"])
        return None

    def outputs(self):
        """Someone has the RTA routing open: read the card and USB outputs' sources for the next 30 seconds."""
        first = self.out_focus < time.time()
        self.out_focus = time.time() + 30
        if first:
            for g, c in OUTS.items():
                for n in range(1, c + 1):
                    self.send(osc("/io/out/%s/%d/grp" % (g, n)))
                    self.send(osc("/io/out/%s/%d/in" % (g, n)))
        with self.lock:
            return {k: dict(v) for k, v in self.outs.items() if "grp" in v}

    def set_out(self, out, grp, idx):
        """Patch output `out` ("CRD/12") to source grp/idx ("LCL", 8). Only in Control, and only once that output has been
        read from the Wing, so we know the names work. -> error or None"""
        if self.settings["mode"] != "control":
            return "The Wing is in Watch only. An Admin switches it to Control on the Mixer screen first."
        if self.status()["status"] != "connected":
            return "The Wing isn't answering."
        g, _, n = (out or "").partition("/")
        if g not in OUTS or not n.isdigit() or not 1 <= int(n) <= OUTS[g]:
            return "That output can't be patched from here."
        if grp not in OUT_SRC or not isinstance(idx, int) or isinstance(idx, bool) or not (idx == 0 if grp == "OFF" else 1 <= idx <= OUT_SRC[grp]):
            return "That isn't a source the Wing can send there."
        with self.lock:
            known = "grp" in self.outs.get(out, {})
        if not known:
            return "The Wing hasn't told C3 Life what %s sends yet, so it won't change it. Try again in a few seconds." % out
        self.send(osc("/io/out/%s/%s/grp" % (g, n), grp))
        self.send(osc("/io/out/%s/%s/in" % (g, n), int(idx)))
        self.send(osc("/io/out/%s/%s/grp" % (g, n)))  # and read it back
        self.send(osc("/io/out/%s/%s/in" % (g, n)))
        return None

    # ---- changes from the app -------------------------------------------------------------------

    def set(self, kind, n, field, value):
        """Send one change. Returns None, or what's wrong (in words for the app)."""
        if self.settings["mode"] != "control":
            return "The app is set to watch the Wing only. An Admin can switch it to control on the Mixer screen."
        if self.status()["status"] != "connected":
            return "The Wing isn't answering, so nothing was sent."
        if kind not in KINDS or not isinstance(n, int) or not 1 <= n <= KINDS[kind]:
            return "There's no %s %s on the Wing." % (kind, n)
        if field == "fdr":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not -144 <= value <= 10:
                return "A fader goes from −∞ to +10 dB."
            self.cancel_fade(kind, n)
            ok = self.send(osc("/%s/%d/fdr" % (kind, n), float(value)))
        elif field == "mute":
            ok = self.send(osc("/%s/%d/mute" % (kind, n), 1 if value else 0))
        elif field == "pan":
            if kind == "dca" or not isinstance(value, (int, float)) or isinstance(value, bool) or not -100 <= value <= 100:
                return "Pan goes from −100 (left) to +100 (right)."
            ok = self.send(osc("/%s/%d/pan" % (kind, n), float(value)))
        elif field == "name":
            if not isinstance(value, str) or not value.strip() or len(value) > 16:
                return "A name is 1 to 16 letters."
            ok = self.send(osc("/%s/%d/name" % (kind, n), value.strip()))
        elif field == "col":
            if not isinstance(value, int) or not 1 <= value <= 12:
                return "A colour is 1 to 12."
            ok = self.send(osc("/%s/%d/col" % (kind, n), value))
        elif field.startswith("send/") and field.count("/") == 2:
            _, b, f = field.split("/")
            if kind not in SENDERS or not b.isdigit() or not 1 <= int(b) <= KINDS["bus"] or f not in SEND:
                return "That send isn't on the Wing."
            if f == "lvl":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not -144 <= value <= 10:
                    return "A send goes from −∞ to +10 dB."
                v = float(value)
            elif f == "pan":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not -100 <= value <= 100:
                    return "Pan goes from −100 (left) to +100 (right)."
                v = float(value)
            elif f == "mode":
                if value not in ("PRE", "POST"):
                    return "A send is Pre or Post."
                v = value
            else:
                v = 1 if value else 0
            ok = self.send(osc("/%s/%d/send/%s/%s" % (kind, n, b, f), v)) and self.send(osc("/%s/%d/send/%s/%s" % (kind, n, b, f)))
            return None if ok else "Couldn't send to the Wing."
        elif field in BUS_EQ and kind == "bus":
            k = BUS_EQ[field]
            if k[0] == "s":
                if value not in k[1]:
                    return "That's one of %s." % ", ".join(k[1])
                v = value
            elif k[0] == "b":
                v = 1 if value else 0
            elif not isinstance(value, (int, float)) or isinstance(value, bool) or not k[1] <= value <= k[2]:
                return "That's outside what the Wing allows (%g to %g)." % (k[1], k[2])
            else:
                v = float(value)
            ok = self.send(osc("/bus/%d/%s" % (n, field), v)) and self.send(osc("/bus/%d/%s" % (n, field)))
            return None if ok else "Couldn't send to the Wing."
        elif field in PROC:
            kind2 = PROC[field]
            if kind != "ch":
                return "EQ, gate and compressor are on channels here."
            if kind2[0] == "s":
                return "Change the plugin model on the Wing itself."
            if kind2[0] == "b":
                value = 1 if value else 0
            elif not isinstance(value, (int, float)) or isinstance(value, bool) or not kind2[1] <= value <= kind2[2]:
                return "That's outside what the Wing allows (%g to %g)." % (kind2[1], kind2[2])
            if field == "dyn/ratio":
                value = min(RATIOS, key=lambda r: abs(r - value))  # the Wing's own steps
            ok = self.send(osc("/ch/%d/%s" % (n, field), int(value) if kind2[0] == "b" else float(value)))
            if ok:
                self.send(osc("/ch/%d/%s" % (n, field)))
            return None if ok else "Couldn't send to the Wing."
        elif field in ("gain", "vph"):
            if kind != "ch":
                return "Gain and phantom power are on channels."
            with self.lock:
                key = self.src(n)
                s = self.strips["ch"][n]
                what = s.get("src_grp", "nothing")
            if not key:
                return "Ch %d isn't fed by a mic input (it's %s), so it has no gain or phantom power here." % (n, what)
            if field == "gain":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not -3 <= value <= 60:
                    return "Gain goes from −3 to +60 dB."
                ok = self.send(osc("/io/in/%s/g" % key, float(value)))
            else:
                ok = self.send(osc("/io/in/%s/vph" % key, 1 if value else 0))
            if ok:
                self.send(osc("/io/in/%s/%s" % (key, "g" if field == "gain" else "vph")))
            return None if ok else "Couldn't send to the Wing."
        else:
            return "That can't be changed here."
        if ok:
            self.send(osc("/%s/%d/%s" % (kind, n, field)))  # read it back, so every screen shows what the desk has
        return None if ok else "Couldn't send to the Wing."

    def cancel_fade(self, kind, n):
        with self.lock:
            ev = self.fades.pop((kind, n), None)
        if ev:
            ev.set()

    def fade(self, moves):
        """Slide cues: [{"kind","n","to" dB, "secs", "mute"?: before, "then_mute"?, "then_fdr"?}] run together."""
        if self.settings["mode"] != "control":
            return "The app is set to watch the Wing only."
        if self.status()["status"] != "connected":
            return "The Wing isn't answering, so nothing was sent."
        jobs = []
        for m in moves:
            kind, n = m.get("kind", "ch"), m.get("n")
            if kind not in KINDS or not isinstance(n, int) or not 1 <= n <= KINDS[kind]:
                return "There's no %s %s on the Wing." % (kind, n)
            with self.lock:
                start = self.strips[kind][n].get("fdr", -144.0)
            jobs.append((kind, n, m, start))
        for kind, n, m, start in jobs:
            self.cancel_fade(kind, n)
            ev = threading.Event()
            with self.lock:
                self.fades[(kind, n)] = ev
            threading.Thread(target=self._fade_one, args=(kind, n, m, start, ev), daemon=True).start()
        return None

    def _fade_one(self, kind, n, m, start, ev):
        a = "/%s/%d/" % (kind, n)
        if "from" in m:
            start = float(m["from"])
            self.send(osc(a + "fdr", start))
        if "mute" in m:
            self.send(osc(a + "mute", 1 if m["mute"] else 0))
        to, secs = float(m.get("to", start)), max(0.0, float(m.get("secs", 0)))
        p0, p1, t0 = db_to_pos(start), db_to_pos(to), time.time()
        while secs and not ev.is_set():
            k = min(1.0, (time.time() - t0) / secs)
            self.send(osc(a + "fdr", float(round(pos_to_db(p0 + (p1 - p0) * k), 2))))
            if k >= 1:
                break
            ev.wait(0.05)
        if ev.is_set():
            return
        if not secs:
            self.send(osc(a + "fdr", to))
        if "then_mute" in m:
            self.send(osc(a + "mute", 1 if m["then_mute"] else 0))
        if "then_fdr" in m:
            self.send(osc(a + "fdr", float(m["then_fdr"])))
        for f in ("fdr", "mute"):
            self.send(osc(a + f))
        with self.lock:
            if self.fades.get((kind, n)) is ev:
                del self.fades[(kind, n)]

    # ---- meters (native protocol) ---------------------------------------------------------------

    @staticmethod
    def _esc(bs):
        out = bytearray()
        for b in bs:
            out.append(b)
            if b == 0xDF:
                out.append(0xDE)
        return bytes(out)

    def meter_request(self, port, mid=1):
        pb = self._esc(bytes([(port >> 8) & 0xFF, port & 0xFF]))
        ib = self._esc(bytes([(mid >> 8) & 0xFF, mid & 0xFF]))
        body = b"".join(bytes([0xA0, n]) for n in range(KINDS["ch"]))
        return b"\xdf\xd3\xd3" + pb + b"\xd4" + ib + pb + b"\xdc" + body + b"\xde\xdf\xd1"

    def meter_keepalive(self, port, mid=1):
        return b"\xdf\xd3\xd4" + self._esc(bytes([(mid >> 8) & 0xFF, mid & 0xFF])) + self._esc(bytes([(port >> 8) & 0xFF, port & 0xFF])) + b"\xdf\xd1"

    def meter_loop(self):
        if not self.cfg.get("wing_meters", True):
            return
        while not self.stop.is_set():
            ip = self.settings["ip"]
            if not ip or self.settings["mode"] == "off":
                self.stop.wait(2)
                continue
            try:
                self._meters(ip)
            except OSError as e:
                self.log.info("wing: meters stopped (%s); trying again", e.__class__.__name__)
            self.stop.wait(5)

    def _meters(self, ip):
        while True:  # a port with no 0xdf byte, so nothing in the request needs escaping
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(("0.0.0.0", 0))
            port = u.getsockname()[1]
            if 0xDF not in (port >> 8, port & 0xFF):
                break
            u.close()
        u.settimeout(0.5)
        t = socket.create_connection((ip, self.native_port), timeout=3)
        try:
            t.setblocking(False)
            t.sendall(b"\xdf\xd1" + self.meter_request(port) + self.rta_request(port))
            last_keep, last_data, last_tcp = time.time(), time.time(), 0.0
            while not self.stop.is_set() and self.settings["ip"] == ip and self.settings["mode"] != "off":
                now = time.time()
                if now - last_keep > 3:
                    t.sendall(self.meter_keepalive(port) + self.meter_keepalive(port, 2) + b"\xdf\xd1")
                    last_keep = now
                if now - last_tcp > 1:  # nothing to read on the TCP side, but notice if it closes
                    last_tcp = now
                    try:
                        if t.recv(4096) == b"":
                            return
                    except (BlockingIOError, InterruptedError):
                        pass
                try:
                    data, addr = u.recvfrom(16384)
                except socket.timeout:
                    if time.time() - last_data > 10:
                        return
                    continue
                if addr[0] != ip or len(data) < 4:
                    continue
                last_data = time.time()
                self.take_meters(data)
        finally:
            t.close()
            u.close()

    def take_meters(self, data):
        """One meter packet: <report id, 4 bytes><values, 2 bytes each, signed, 1/256 dB>.
        Report 1 is the channels (8 values each: input L/R, output L/R, gate key, gate gain, dyn key, dyn gain),
        report 2 the RTA (120 bands, 1/12 octave from 20 Hz)."""
        vals = [struct.unpack(">h", data[i:i + 2])[0] for i in range(4, len(data) - 1, 2)]
        now = time.time()
        if data[0:2] == b"\x00\x02":
            with self.lock:
                self.rta, self.rta_at = [round(v / 256.0, 1) for v in vals[:120]], now
            return
        m, d = {}, {}
        for n in range(KINDS["ch"]):
            if n * 8 + 7 < len(vals):
                v = vals[n * 8:n * 8 + 8]
                m[n + 1] = [round(v[2] / 256.0, 1), round(v[3] / 256.0, 1)]  # left, right (after the fader)
                # gain reduction: 1.0 (256) is 60 dB for the standard gate and 20 dB for the compressor
                d[n + 1] = (round(max(v[0], v[1]) / 256.0, 1), round(max(v[2], v[3]) / 256.0, 1),
                            round(v[4] / 256.0, 1), round(abs(v[5]) / 256.0 * 60, 1), round(v[6] / 256.0, 1), round(abs(v[7]) / 256.0 * 20, 1))
            elif n * 8 + 3 < len(vals):
                m[n + 1] = [round(vals[n * 8 + 2] / 256.0, 1), round(vals[n * 8 + 3] / 256.0, 1)]
        with self.lock:
            self.meters, self.meters_at = m, now
            for n in [k for k, t in self.focus.items() if t > now and k in d]:
                h = self.hist.setdefault(n, collections.deque(maxlen=200))
                if not h or now - h[-1][0] >= 0.04:  # up to 25 a second, the last 8 seconds
                    h.append((now,) + d[n])

    def find(self, target="255.255.255.255"):
        """Wings on the network (the desk answers WING? on its own port)."""
        return find(port=self.native_port, target=target)

    def history(self, n, since=0.0):
        """The open channel's last few seconds: [t, in, out, gate key, gate gain reduction, dyn key, dyn gain reduction]."""
        with self.lock:
            # compare the rounded time the app is given, or asking "since the last one" could return that one again
            return [[round(x[0], 2)] + list(x[1:]) for x in self.hist.get(n, ()) if round(x[0], 2) > since]

    def rta_request(self, port, mid=2):
        pb = self._esc(bytes([(port >> 8) & 0xFF, port & 0xFF]))
        return b"\xdf\xd3\xd4" + self._esc(bytes([(mid >> 8) & 0xFF, mid & 0xFF])) + pb + b"\xdc\xaa\xde\xdf\xd1"

    def rta_state(self):
        with self.lock:
            fresh = time.time() - self.rta_at < 2
            src = self.rtacfg.get("$src")
            kind, n = rta_strip(src)
            return {"rta": list(self.rta) if fresh else [], "src": src, "kind": kind, "n": n, "tap": self.rtacfg.get("$tap") or self.rtacfg.get("rtatap"), "fresh": fresh}

    def rta_point(self, n, tap, kind="ch"):
        """Point the Wing's RTA at channel n, or another strip (a bus: a monitor mix), and the desk's own RTA screen
        follows. The source is an index; it's set, then read back, and corrected if the Wing counts from 1."""
        if self.settings["mode"] != "control":
            return "The app is set to watch the Wing only."
        if self.status()["status"] != "connected":
            return "The Wing isn't answering, so nothing was sent."
        if kind not in RTA_BASE or not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= KINDS[kind] or tap not in RTA_TAPS:
            return "Pick a channel, and before or after the EQ."
        idx = RTA_BASE[kind] + n
        self.rta_want = (idx, tap)
        self.send(osc("/cfg/rta/rtatap", tap))
        self.send(osc("/cfg/rta/rtasrc", idx - 1 + self.rta_off))
        for a in ("/cfg/rta/$src", "/cfg/rta/$tap", "/cfg/rta/rtasrc"):
            self.send(osc(a))
        return None

    def _rta_check(self):
        # called when /cfg/rta/$src arrives: if the Wing counts its index differently, fix it once
        w = self.rta_want
        src = self.rtacfg.get("$src")
        if not w or src is None or src == w[0]:
            return
        if self.rta_off == 0 and src == w[0] - 1:
            self.rta_off = 1
            self.send(osc("/cfg/rta/rtasrc", w[0] - 1 + self.rta_off))
            self.send(osc("/cfg/rta/$src"))

def rta_strip(src):
    """The RTA's source index -> ("bus", 4), or (None, None)."""
    if not isinstance(src, int) or isinstance(src, bool):
        return None, None
    for kind, base in sorted(RTA_BASE.items(), key=lambda kv: -kv[1]):
        if src > base:
            return (kind, src - base) if src - base <= KINDS[kind] else (None, None)
    return None, None


def find(port=2222, timeout=1.5, target="255.255.255.255"):
    """Look for Wings on the network. -> [{"ip","name","model","firmware"}]"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    out, end = [], time.time() + timeout
    try:
        s.sendto(b"WING?", (target, port))
        while time.time() < end:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            t = data.decode("ascii", "replace").split(",")
            if len(t) >= 6 and t[0] == "WING" and not any(x["ip"] == t[1] for x in out):
                out.append({"ip": t[1], "name": t[2], "model": t[3], "serial": t[4], "firmware": t[5]})
    except OSError:
        pass
    finally:
        s.close()
    return out


# what the Wing has, for the Desk interface (desk.py)
Wing.KINDS, Wing.SENDERS, Wing.BUS_EQ, Wing.RATIOS, Wing.RTA_TAPS, Wing.OUTS, Wing.TAP_EQ = KINDS, SENDERS, BUS_EQ, RATIOS, RTA_TAPS, OUTS, TAP_EQ
