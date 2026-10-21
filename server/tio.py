"""The Yamaha Tio1608-D stagebox: gain, 48 V phantom power and the low cut on its 16 inputs.

Yamaha's text control protocol (the one its consoles and R Remote use) on TCP port 49280, one line each way:
    get IO:Current/InCh/HAGain 3 0          ->  OK get IO:Current/InCh/HAGain 3 0 30
    set IO:Current/InCh/48VOn 3 0 1         ->  OK set IO:Current/InCh/48VOn 3 0 1
and when something else (R Remote, a Yamaha desk) changes it:  NOTIFY set IO:Current/InCh/HAGain 3 0 32
Inputs are numbered from 0 on the wire (input 1 is 0). Gain is −6 to +66 dB in 1 dB steps; the low cut frequency
is sent in tenths of a hertz (800 is 80 Hz). These names and ranges come from the Tio's own parameter list
(prminfo, as published with the Bitfocus Companion Yamaha module); they haven't been tried on our Tio yet.

Like the Wing, it starts in Watch only. The Tio feeds the Wing over Dante: Tio input 1 arrives on Wing card input
`card_from` (1 unless the Dante routing is different), so the Mixer shows the Tio's gain on that channel.

Settings: data/tio.json {"ip", "mode": "off" | "watch" | "control", "card_from": 1}.
"""

import json
import os
import socket
import threading
import time

PORT = 49280
INPUTS = 16
PARAMS = {"gain": "HAGain", "vph": "48VOn", "hpf": "HPFOn", "hpff": "HPFFreq"}
NAMES = {v: k for k, v in PARAMS.items()}
MODES = ("off", "watch", "control")


class Tio:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.port = int(cfg.get("tio_port", PORT))
        self.path = os.path.join(data_dir, "tio.json")
        self.settings = {"ip": cfg.get("tio_ip", ""), "mode": "watch" if cfg.get("tio_ip") else "off", "card_from": 1}
        try:
            with open(self.path, encoding="utf-8") as f:
                s = json.load(f)
            if s.get("mode") in MODES:
                self.settings["mode"] = s["mode"]
            if isinstance(s.get("ip"), str):
                self.settings["ip"] = s["ip"]
            if isinstance(s.get("card_from"), int) and 1 <= s["card_from"] <= 64:
                self.settings["card_from"] = s["card_from"]
        except (OSError, ValueError):
            pass
        self.lock = threading.Lock()
        self.inputs = {n: {} for n in range(1, INPUTS + 1)}
        self.seen = 0.0
        self.version = 0
        self.conn = None
        self.stop = threading.Event()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f)
        os.replace(tmp, self.path)

    def configure(self, ip=None, mode=None, card_from=None):
        if mode is not None and mode not in MODES:
            return "Mode is off, watch or control."
        if card_from is not None and (not isinstance(card_from, int) or isinstance(card_from, bool) or not 1 <= card_from <= 64 - INPUTS + 1):
            return "The Wing card input for Tio input 1 is 1 to %d." % (64 - INPUTS + 1)
        with self.lock:
            if ip is not None and ip.strip() != self.settings["ip"]:
                self.settings["ip"] = ip.strip()
                self.inputs = {n: {} for n in range(1, INPUTS + 1)}
                self.seen = 0.0
                self.drop()
            if mode is not None:
                self.settings["mode"] = mode
                if mode == "off":
                    self.drop()
            if card_from is not None:
                self.settings["card_from"] = card_from
            self.version += 1
            self.save()
        return None

    def status(self):
        ip, mode = self.settings["ip"], self.settings["mode"]
        st = "off" if mode == "off" else ("no address" if not ip else ("connected" if self.seen and time.time() - self.seen < 12 else "no answer"))
        return {"status": st, "mode": mode, "ip": ip, "card_from": self.settings["card_from"], "version": self.version}

    def state(self):
        with self.lock:
            return dict(self.status(), inputs={str(n): dict(v) for n, v in self.inputs.items()})

    # ---- the connection --------------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def close(self):
        self.stop.set()
        self.drop()

    def drop(self):
        c, self.conn = self.conn, None
        if c:
            try:
                c.close()
            except OSError:
                pass

    def send(self, line):
        c = self.conn
        if not c:
            return False
        try:
            c.sendall((line + "\n").encode("ascii"))
            return True
        except OSError:
            self.drop()
            return False

    def ask_all(self):
        for n in range(INPUTS):
            for p in PARAMS.values():
                self.send("get IO:Current/InCh/%s %d 0" % (p, n))

    def loop(self):
        while not self.stop.is_set():
            ip = self.settings["ip"]
            if not ip or self.settings["mode"] == "off":
                self.stop.wait(2)
                continue
            try:
                c = socket.create_connection((ip, self.port), timeout=3)
            except OSError:
                self.stop.wait(5)
                continue
            c.settimeout(0.5)
            self.conn, buf, last_ask = c, b"", 0.0
            try:
                while not self.stop.is_set() and self.conn is c:
                    if time.time() - last_ask > 5:  # every few seconds, in case a NOTIFY was missed
                        self.ask_all()
                        last_ask = time.time()
                    try:
                        d = c.recv(4096)
                    except socket.timeout:
                        continue
                    if not d:
                        break
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.take(line.decode("ascii", "replace").strip())
            except OSError as e:
                self.log.info("tio: connection dropped (%s)", e.__class__.__name__)
            finally:
                if self.conn is c:
                    self.drop()
            self.stop.wait(2)

    def take(self, line):
        """OK get|set ... or NOTIFY set ...: IO:Current/InCh/<param> <input> 0 <value>"""
        t = line.split()
        if len(t) < 6 or t[0] not in ("OK", "NOTIFY") or t[1] not in ("get", "set") or not t[2].startswith("IO:Current/InCh/"):
            return
        name = NAMES.get(t[2].rsplit("/", 1)[1])
        try:
            n, v = int(t[3]) + 1, int(t[5])
        except ValueError:
            return
        if not name or not 1 <= n <= INPUTS:
            return
        with self.lock:
            self.seen = time.time()
            if name == "hpff":
                v = v / 10.0
            if self.inputs[n].get(name) != v:
                self.inputs[n][name] = v
                self.version += 1

    # ---- changes from the app --------------------------------------------------------------------

    def set(self, n, field, value):
        if self.settings["mode"] != "control":
            return "The app is set to watch the Tio only. An Admin can switch it to Control on the Mixer screen."
        if self.status()["status"] != "connected":
            return "The Tio isn't answering, so nothing was sent."
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= INPUTS:
            return "The Tio has inputs 1 to 16."
        if field not in PARAMS:
            return "That can't be changed on the Tio here."
        if field in ("vph", "hpf"):
            v = 1 if value else 0
        else:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return "That isn't a number."
            if field == "gain":
                if not -6 <= value <= 66:
                    return "Tio gain goes from −6 to +66 dB."
                v = int(round(value))
            else:
                if not 20 <= value <= 600:
                    return "The Tio's low cut goes from 20 to 600 Hz."
                v = int(round(value * 10))
        p = PARAMS[field]
        ok = self.send("set IO:Current/InCh/%s %d 0 %d" % (p, n - 1, v)) and self.send("get IO:Current/InCh/%s %d 0" % (p, n - 1))
        return None if ok else "Couldn't send to the Tio."
