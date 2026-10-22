"""MIDI from MultiTracks Playback into Lights, over network MIDI.

The C3 Life server is a network MIDI session ("C3 Life", Apple's RTP-MIDI, the one Macs and iPads use: control port
5004, data port 5005). On the Mac with Playback: Audio MIDI Setup › Window › Show MIDI Studio › Network: in Directory
press +, type the C3 Life server's address and port 5004, then Connect. Playback's MIDI Out then goes to that session.

What each message does is set on Lights › Show setup › MultiTracks Playback, and kept in data/lights_midi.json:
  - Program Change n (on the chosen channel): C3 Life's cue number n (or n + 1 when numbers start at 0)
  - a Note: Go, Back, Release, Blackout on/off, a cue, a palette preset or a look
  - a Control Change: the master (0-127 is 0-100 %)
  - MIDI clock: Lightkey's tempo (sent when it changes by a beat or more, at most every 2 s)
Nothing happens unless Follow Playback is on: until then each message is shown with what it would have done.
The Sunday run sheet is unchanged: this only follows what Playback is told to send.

Sending to Playback (send()): the run sheet's Playback steps (Play, Stop, Next song, Previous song) go back over the same
session, as notes on the channel set on Macros › Playback (data/macros.json). In Playback, set its MIDI input to the
"C3 Life" network session and map those notes to its controls (Playback's MIDI settings, MIDI learn).

Not tried with Playback yet. RFC 6295 (RTP-MIDI) and Apple's session protocol; no journal recovery.
"""

import json
import os
import socket
import struct
import threading
import time

NAME = "C3 Life"
PORT = 5004
ACTIONS = ("go", "back", "release", "blackout_on", "blackout_off", "cue", "preset", "look")
DEFAULT = {"follow": False, "channel": 0, "pc": True, "pc_from": 1, "notes": [], "cc_master": 0, "tempo": False}


def valid(d, show=None):
    """None if these settings are fine, else why not."""
    if not isinstance(d, dict):
        return "Settings not understood."
    for k in ("follow", "pc", "tempo"):
        if not isinstance(d.get(k), bool):
            return "Settings not understood."
    if not isinstance(d.get("channel"), int) or isinstance(d["channel"], bool) or not 0 <= d["channel"] <= 16:
        return "The MIDI channel is 1 to 16, or any."
    if d.get("pc_from") not in (0, 1):
        return "Program numbers start at 0 or 1."
    if not isinstance(d.get("cc_master"), int) or isinstance(d["cc_master"], bool) or not 0 <= d["cc_master"] <= 127:
        return "The master's controller is 1 to 127 (0 for none)."
    notes = d.get("notes")
    if not isinstance(notes, list) or len(notes) > 64:
        return "Up to 64 notes."
    seen = set()
    ids = {k: {x["id"] for x in (show or {}).get(k, [])} for k in ("cues", "presets", "looks")}
    for r in notes:
        if not isinstance(r, dict) or not isinstance(r.get("note"), int) or isinstance(r["note"], bool) or not 0 <= r["note"] <= 127:
            return "Each note is 0 to 127."
        if r["note"] in seen:
            return "Note %d is there twice." % r["note"]
        seen.add(r["note"])
        if r.get("do") not in ACTIONS:
            return "Note %d: what should it do?" % r["note"]
        if r["do"] in ("cue", "preset", "look"):
            if show is not None and r.get("id") not in ids[r["do"] + "s"]:
                return "Note %d: that %s has gone." % (r["note"], r["do"])
    return None


def note_name(n):
    return ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][n % 12] + str(n // 12 - 2)  # 60 = C3, as Playback shows it


# ---- RTP-MIDI ----------------------------------------------------------------------------------

def _delta(data, i):
    """A delta time: 1 to 4 bytes, the top bit says more follow."""
    for _ in range(4):
        if i >= len(data):
            return i
        b = data[i]
        i += 1
        if not b & 0x80:
            return i
    return i


def _data_len(status):
    if 0x80 <= status <= 0xBF or 0xE0 <= status <= 0xEF:
        return 2
    if 0xC0 <= status <= 0xDF:
        return 1
    return {0xF1: 1, 0xF2: 2, 0xF3: 1}.get(status, 0)


def parse_rtp_midi(pkt):
    """An RTP-MIDI packet -> [(status, data bytes)]. Running status, delta times and SysEx are handled; the
    recovery journal is ignored."""
    if len(pkt) < 13 or pkt[0] >> 6 != 2 or pkt[1] & 0x7F != 0x61:
        return []
    i = 12 + 4 * (pkt[0] & 0x0F)
    if i >= len(pkt):
        return []
    flags = pkt[i]
    if flags & 0x80:
        if i + 1 >= len(pkt):
            return []
        length, i = ((flags & 0x0F) << 8) | pkt[i + 1], i + 2
    else:
        length, i = flags & 0x0F, i + 1
    data = pkt[i:i + length]
    out, j, running, first = [], 0, None, True
    while j < len(data):
        if not first or flags & 0x20:
            j = _delta(data, j)
            if j >= len(data):
                break
        first = False
        b = data[j]
        if b == 0xF0 or b == 0xF7:  # SysEx (or a piece of one): skip to its end
            j += 1
            while j < len(data) and data[j] not in (0xF7, 0xF0, 0xF4):
                j += 1
            j += 1
            continue
        if b & 0x80:
            status, j = b, j + 1
            if status < 0xF0:
                running = status
            elif status < 0xF8:
                running = None
        elif running is not None:
            status = running
        else:
            j += 1
            continue
        n = _data_len(status)
        out.append((status, bytes(data[j:j + n])))
        j += n
    return out


class Midi:
    """The network MIDI session, and what Playback's messages do in Lights."""

    def __init__(self, data_dir, cfg, log, lights, allowed=None):
        self.path = os.path.join(data_dir, "lights_midi.json")
        self.log, self.lights, self.allowed = log, lights, allowed or (lambda ip: True)
        self.port = int(cfg.get("midi_port", PORT))
        self.lock = threading.Lock()
        self.d = dict(DEFAULT)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = dict(DEFAULT, **json.load(f))
            if not valid(d):
                self.d = d
        except (OSError, ValueError, TypeError):
            pass
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.peers = {}   # their ssrc -> {"name", "ip", "since", "seen", "data": (ip, port) of their data port}
        self.seq = int.from_bytes(os.urandom(2), "big")
        self.heard = []   # the last 30 messages, with what they did
        self.clock = []   # MIDI clock tick times, for the tempo
        self.bpm_sent = (0.0, 0.0)
        self.stop = threading.Event()
        self.ctl = self.dat = None
        self.error = ""

    def start(self):
        # Network MIDI uses two ports side by side (5004 and 5005). With port 0 (the tests: any free pair), try a few.
        for _ in range(1 if self.port else 30):
            self.ctl = self.dat = None
            try:
                self.ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.ctl.bind(("0.0.0.0", self.port))
                p = self.ctl.getsockname()[1]
                if p >= 65535:
                    raise OSError(0, "no port after %d" % p)
                self.dat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.dat.bind(("0.0.0.0", p + 1))
                break
            except OSError as e:
                for x in (self.ctl, self.dat):
                    if x:
                        x.close()
                self.ctl = self.dat = None
                err = e
        if not self.ctl:
            self.error = "Port %d or %d is in use, so Playback can't connect (%s)." % (self.port, self.port + 1, err.strerror or err)
            self.log.warning("midi: %s", self.error)
            return
        self.port = self.ctl.getsockname()[1]
        for sock in (self.ctl, self.dat):
            sock.settimeout(0.5)
            threading.Thread(target=self.loop, args=(sock,), daemon=True).start()
        self.log.info("midi: network MIDI session %r on ports %d and %d", NAME, self.port, self.port + 1)

    def close(self):
        self.stop.set()
        for s in (self.ctl, self.dat):
            if s:
                s.close()

    # ---- the session (Apple's protocol) ---------------------------------------------------------

    def loop(self, sock):
        while not self.stop.is_set():
            try:
                pkt, frm = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.allowed(frm[0]):
                continue
            try:
                if pkt[:2] == b"\xff\xff":
                    self.command(sock, pkt, frm)
                else:
                    self.midi(pkt, frm)
            except (struct.error, IndexError, ValueError) as e:
                self.log.warning("midi: a message from %s wasn't understood: %s", frm[0], e)

    def command(self, sock, pkt, frm):
        cmd = pkt[2:4]
        if cmd == b"IN" and len(pkt) >= 16:
            _, token, ssrc = struct.unpack(">III", pkt[4:16])
            name = pkt[16:].split(b"\0")[0].decode("utf-8", "replace")[:60] or frm[0]
            sock.sendto(b"\xff\xffOK" + struct.pack(">III", 2, token, self.ssrc) + NAME.encode() + b"\0", frm)
            with self.lock:
                new = ssrc not in self.peers
                old = self.peers.get(ssrc, {})
                self.peers[ssrc] = {"name": name, "ip": frm[0], "since": old.get("since", time.time()), "seen": time.time(),
                                    "data": frm if sock is self.dat else old.get("data")}
            if new and sock is self.dat:
                self.log.info("midi: %s (%s) connected", name, frm[0])
        elif cmd == b"BY" and len(pkt) >= 16:
            ssrc = struct.unpack(">I", pkt[12:16])[0]
            with self.lock:
                p = self.peers.pop(ssrc, None)
            if p:
                self.log.info("midi: %s disconnected", p["name"])
        elif cmd == b"CK" and len(pkt) >= 36:
            ssrc, count = struct.unpack(">IB", pkt[4:9])
            if count == 0:
                ts1 = pkt[12:20]
                now = struct.pack(">Q", int(time.time() * 10000) & 0xFFFFFFFFFFFFFFFF)
                sock.sendto(b"\xff\xffCK" + struct.pack(">IB", self.ssrc, 1) + b"\0\0\0" + ts1 + now + b"\0" * 8, frm)
            with self.lock:
                if ssrc in self.peers:
                    self.peers[ssrc]["seen"] = time.time()

    # ---- sending (the run sheet's Playback steps) -----------------------------------------------

    def send(self, msg, to=None):
        """Sends MIDI bytes (one message, e.g. [0x9F, 60, 100]) to everyone connected, or to the peer named `to`.
        -> None, or what's wrong in words"""
        with self.lock:
            peers = [p for p in self.peers.values() if p.get("data") and time.time() - p["seen"] < 60 and (not to or p["name"] == to)]
            self.seq = (self.seq + 1) & 0xFFFF
            seq = self.seq
        if not self.dat:
            return self.error or "C3 Life's network MIDI session isn't running."
        if not peers:
            return "Playback isn't connected to C3 Life's network MIDI session. On the Mac with Playback: Audio MIDI Setup › Network › connect to C3 Life."
        ts = int(time.time() * 10000) & 0xFFFFFFFF
        b = bytes(msg)
        pkt = struct.pack(">BBHII", 0x80, 0x61, seq, ts, self.ssrc) + bytes([len(b) & 0x0F]) + b  # RTP-MIDI, short command list
        for p in peers:
            try:
                self.dat.sendto(pkt, p["data"])
            except OSError as e:
                return "Couldn't send to %s (%s)." % (p["name"], e)
        return None

    # ---- what the MIDI does ---------------------------------------------------------------------

    def midi(self, pkt, frm):
        ssrc = struct.unpack(">I", pkt[8:12])[0] if len(pkt) >= 12 else 0
        with self.lock:
            who = self.peers.get(ssrc, {}).get("name") or frm[0]
            if ssrc in self.peers:
                self.peers[ssrc]["seen"] = time.time()
        for status, data in parse_rtp_midi(pkt):
            self.handle(status, data, who)

    def handle(self, status, data, who="Playback", now=None):
        """One MIDI message -> what it does (if Follow Playback is on). `now`: when it arrived (the tests)."""
        d = self.settings()
        if status == 0xF8:  # clock: 24 a beat
            if d["tempo"]:
                self.tick(d["follow"], now)
            return
        if status >= 0xF0:
            return
        kind, ch = status & 0xF0, (status & 0x0F) + 1
        if d["channel"] and ch != d["channel"]:
            return
        act, what = None, ""
        if kind == 0xC0 and data:
            what = "Program %d" % (data[0] + (1 if d["pc_from"] == 1 else 0))
            if d["pc"]:
                num = data[0] + (1 if d["pc_from"] == 1 else 0)
                cues = self.lights.show["cues"]
                if 1 <= num <= len(cues):
                    act = {"do": "cue", "id": cues[num - 1]["id"]}
                else:
                    self.note(who, ch, what, "no cue %d in the list" % num, False)
                    return
        elif kind == 0x90 and len(data) == 2 and data[1] > 0:
            what = "Note %s (%d)" % (note_name(data[0]), data[0])
            r = next((x for x in d["notes"] if x["note"] == data[0]), None)
            if r:
                act = {"go": {"do": "go"}, "back": {"do": "back"}, "release": {"do": "release"},
                       "blackout_on": {"do": "blackout", "on": True}, "blackout_off": {"do": "blackout", "on": False},
                       "cue": {"do": "cue", "id": r.get("id")}, "preset": {"do": "preset", "id": r.get("id"), "on": True},
                       "look": {"do": "look", "id": r.get("id")}}[r["do"]]
        elif kind == 0xB0 and len(data) == 2:
            what = "CC %d = %d" % (data[0], data[1])
            if d["cc_master"] and data[0] == d["cc_master"]:
                act = {"do": "master", "value": round(data[1] / 127.0, 3)}
        elif kind == 0x80 or (kind == 0x90 and len(data) == 2 and data[1] == 0):
            return  # note off
        else:
            return
        if not act:
            self.note(who, ch, what, "nothing set for it", False)
            return
        desc = self.describe(act)
        if not d["follow"]:
            self.note(who, ch, what, desc + " (not sent: Follow Playback is off)", False)
            return
        ok, err, _ = self.lights.do(act, "Playback (MIDI)")
        self.note(who, ch, what, desc + ("" if ok else " (not sent: %s)" % err), ok)

    def describe(self, act):
        show = self.lights.show
        name = lambda k, i: next((x["name"] for x in show[k] if x["id"] == i), "?")
        return {"go": "Go", "back": "Back", "release": "Release"}.get(act["do"]) or \
            ({"cue": lambda: "cue " + name("cues", act["id"]), "preset": lambda: "preset " + name("presets", act["id"]),
              "look": lambda: "look " + name("looks", act["id"]), "blackout": lambda: "Blackout " + ("on" if act["on"] else "off"),
              "master": lambda: "master %d%%" % round(act["value"] * 100)}[act["do"]])()

    def tick(self, follow, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.clock = [t for t in self.clock if now - t < 3.0] + [now]
            if len(self.clock) < 48:
                return
            bpm = 60.0 / ((self.clock[-1] - self.clock[0]) / (len(self.clock) - 1) * 24)
            last_bpm, last_t = self.bpm_sent
            if not 30 <= bpm <= 300 or (abs(bpm - last_bpm) < 1.0) or now - last_t < 2.0:
                return
            self.bpm_sent = (round(bpm, 1), now)
        if follow:
            ok, err, _ = self.lights.do({"do": "tempo", "bpm": round(bpm, 1)}, "Playback (MIDI clock)")
            self.note("Playback", 0, "MIDI clock", "tempo %.0f BPM" % bpm + ("" if ok else " (not sent: %s)" % err), ok)
        else:
            self.note("Playback", 0, "MIDI clock", "tempo %.0f BPM (not sent: Follow Playback is off)" % bpm, False)

    def note(self, who, ch, what, did, sent):
        with self.lock:
            self.heard.append({"t": round(time.time(), 2), "from": who, "ch": ch, "msg": what, "did": did, "sent": sent})
            del self.heard[:-30]

    # ---- settings and what the screen gets ------------------------------------------------------

    def settings(self):
        with self.lock:
            return json.loads(json.dumps(self.d))

    def save(self, d):
        d = {k: d[k] for k in DEFAULT}
        with self.lock:
            self.d = d
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(self.path + ".tmp", self.path)

    def view(self):
        with self.lock:
            now = time.time()
            peers = [{"name": p["name"], "ip": p["ip"], "since": round(p["since"]), "live": now - p["seen"] < 30} for p in self.peers.values()]
            return {"settings": json.loads(json.dumps(self.d)), "session": NAME, "port": self.port if self.ctl else None,
                    "error": self.error, "peers": peers, "heard": list(reversed(self.heard)), "bpm": self.bpm_sent[0] or None}
