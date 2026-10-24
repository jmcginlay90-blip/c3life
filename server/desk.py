"""The sound desk, behind one interface, so a different desk can be added without changing the rest of C3 Life.

The C3 Life server only talks to the desk through a Desk (below). Each kind of desk is a driver: a class with these
methods, in its own file, listed in DRIVERS. config.json picks one with "desk" ("wing", the default, or "none").
Adding a desk (an Allen & Heath dLive, a Yamaha CL, an X32...) means writing its driver; the screens, the access levels,
monitor mixes, cues, the RTA and Lights stay as they are. A driver says what it can do in CAN, and anything it can't
answers in words ("This desk can't do that yet."), which the screens show as they are.

What a driver provides (the Wing's driver, wing.py, is the full example):
  NAME, KINDS          the desk's name, and its strips: {"ch": 40, "aux": 8, "bus": 16, "main": 4, "mtx": 8, "dca": 16}
  SENDERS              which kinds of strip send to buses ("ch", "aux")
  BUS_EQ, RATIOS       a bus's EQ fields with their ranges; the compressor's ratios
  TAP_EQ               a channel's EQ on its pre-fader send tap (the in-ears), if the desk has one
  RTA_TAPS, OUTS       where the RTA can listen (before or after the EQ...); outputs C3 Life may patch, {"CRD": 64}
  CAN                  what it can do (see CAN below)
  settings             {"ip", "mode": "off" | "watch" | "control"}
  start() close() configure(ip=, mode=)                 connect, disconnect, Watch only or Control
  status() state()                                      connected or not; names, colours, faders, mutes, inputs
  set(kind, n, field, value)                            one change: "fdr", "mute", "pan", "name", "col", "gain", "vph",
                                                        "send/<bus>/<lvl|on|pan|mode|plink>", processing, a bus's EQ
                                                        -> None, or what's wrong in words
  fade(moves)                                           slide faders over time (cues, macros)
  channel(n) history(n, since) meter_state()            a channel's processing, its last seconds, the meters
  bus_sends(b) bus_eq(b)                                every send to bus b; bus b's EQ (monitor mixes)
  ask_eq(kind, n) eq(kind, n)                           a bus's, main's or matrix's EQ (Ring-Out cuts on Main 1)
  ask_mtx(b, m) mtx_send(b, m)                          bus b's send to matrix m (the foyer): ask for it, then read it
                                                        (set it with set("bus", b, "mtxsend/<m>/on|lvl", v))
  rta_state() rta_point(n, tap, kind)                   the desk's RTA, and pointing it at a strip
  outputs() set_out(out, grp, idx)                      output patching (the RTA's "Set it up for me")
  set_src(n, grp, idx)                                  a channel's input (a recording played back to the desk)
  find()                                                desks of this kind on the network
"""

import importlib

# What a driver can do. The Wing's driver can do all of these.
CAN = ("faders", "names", "sends", "bus_eq", "tap_eq", "processing", "preamps", "meters", "rta", "fades", "patch_outputs", "find")
CANT = "This desk can't do that yet."

# config.json "desk" -> (module, class)
DRIVERS = {"wing": ("wing", "Wing"), "none": ("desk", "NoDesk")}


class Desk:
    """The interface every desk driver has, with nothing behind it. A driver overrides what its desk can do."""

    NAME = "No sound desk"
    KINDS = {}
    SENDERS = ()
    BUS_EQ = {}
    RATIOS = ()
    RTA_TAPS = ()
    OUTS = {}
    TAP_EQ = {}  # each channel's EQ on its pre-fader send tap (the in-ears), {"peq/on": ("b",), "peq/1g": ("n", -15, 15)...}
    CAN = {k: False for k in CAN}

    def __init__(self, data_dir=None, cfg=None, log=None):
        self.settings = {"ip": "", "mode": "off"}
        self.native_port = self.osc_port = 0
        self.version = 0

    # connecting
    def start(self):
        pass

    def close(self):
        pass

    def configure(self, ip=None, mode=None):
        return CANT

    def status(self):
        return {"status": "off", "mode": "off", "ip": "", "desk": self.NAME}

    def state(self):
        return dict(self.status(), strips={k: {} for k in self.KINDS}, inputs={}, version=self.version)

    # changes
    def set(self, kind, n, field, value):
        return CANT

    def fade(self, moves):
        return CANT

    def set_src(self, n, grp, idx):
        """Switch channel n's input (playing a recording back to the desk)."""
        return CANT

    # reading
    def channel(self, n):
        return {}

    def history(self, n, since=0.0):
        return []

    def meter_state(self):
        return {"meters": False, "ch": {}, "lr": {}}

    def input_level(self, n):
        return None

    def watch(self, path):
        return None

    def bus_sends(self, b):
        return {}

    def bus_eq(self, b):
        return {}

    def ask_eq(self, kind, n):
        pass

    def eq(self, kind, n):
        return {}

    def ask_mtx(self, b, m):
        pass

    def mtx_send(self, b, m):
        return {}

    def rta_state(self):
        return {"rta": [], "src": None, "kind": None, "n": None, "tap": None, "fresh": False}

    def rta_point(self, n, tap, kind="ch"):
        return CANT

    def outputs(self):
        return {}

    def set_out(self, out, grp, idx):
        return CANT

    def find(self, target=None):
        return []


class NoDesk(Desk):
    """"desk": "none" in config.json: C3 Life without a sound desk it can talk to. Mixer, monitor mixes on the desk,
    the desk's RTA and patching say the desk can't; everything else works."""


def make(cfg, data_dir, log):
    """The driver config.json asks for ("desk", default "wing")."""
    name = cfg.get("desk", "wing")
    if name not in DRIVERS:
        raise SystemExit("config.json: \"desk\": %r isn't a desk C3 Life knows. It knows: %s." % (name, ", ".join(sorted(DRIVERS))))
    mod, cls = DRIVERS[name]
    return getattr(importlib.import_module(mod), cls)(data_dir, cfg, log)
