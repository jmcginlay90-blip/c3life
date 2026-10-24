"""Wireless mics: battery, signal and name from networked Shure receivers (QLX-D, ULX-D, SLX-D, Axient Digital), for
Mixer › Mics and the low-battery warning on Home. Watch only: it only ever sends GET, never SET.

Shure's receivers answer plain text on TCP port 2202:
  < GET 1 CHAN_NAME >      -> < REP 1 CHAN_NAME {Pastor HH                       } >
  < GET 1 BATT_BARS >      -> < REP 1 BATT_BARS 004 >          0-5 bars, 255 = unknown (no transmitter)
  < GET 1 BATT_CHARGE >    -> < REP 1 BATT_CHARGE 064 >        % for a Shure rechargeable (SB900...), 255 = not one
  < GET 1 BATT_RUN_TIME >  -> < REP 1 BATT_RUN_TIME 00260 >    minutes left, 65535 = unknown, 65534 = working it out
  < GET 1 TX_TYPE >        -> < REP 1 TX_TYPE QLXD2 >          UNKN = no transmitter switched on
  < GET 1 RX_RF_LVL >      -> < REP 1 RX_RF_LVL -056 >         signal in dBm (ULX-D/Axient; QLX-D/SLX-D may not answer)
Each receiver is in data/mics.json with the Wing channel each of its channels feeds, so the name comes from the Wing.
**Not tried with the church's receivers yet**: the commands follow Shure's command strings documents.
"""

import json
import os
import re
import socket
import threading
import time

PORT = 2202
EVERY = 10
REP = re.compile(r"<\s*REP\s+(?:(\d+)\s+)?([A-Z_]+)\s+(\{[^}]*\}|[^>]*?)\s*>")
ASK = ("CHAN_NAME", "TX_TYPE", "BATT_BARS", "BATT_CHARGE", "BATT_RUN_TIME", "RX_RF_LVL")


def parse(text):
    """-> {channel: {field: value}}"""
    out = {}
    for ch, k, v in REP.findall(text):
        v = v.strip()
        if v.startswith("{"):
            v = v[1:-1].strip()
        out.setdefault(int(ch or 0), {})[k] = v
    return out


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def mic_view(fields):
    """One channel's fields -> what the screen shows."""
    tx = (fields.get("TX_TYPE") or "").upper()
    bars, charge, run = _int(fields.get("BATT_BARS")), _int(fields.get("BATT_CHARGE")), _int(fields.get("BATT_RUN_TIME"))
    off = tx in ("", "UNKN", "UNKNOWN") and (bars is None or bars == 255)
    pct = charge if charge is not None and 0 <= charge <= 100 else (bars * 20 if bars is not None and 0 <= bars <= 5 else None)
    rf = _int(fields.get("RX_RF_LVL"))
    rf_bars = None if rf is None else (5 if rf > -60 else 4 if rf > -70 else 3 if rf > -80 else 2 if rf > -90 else 1)
    return {"name": fields.get("CHAN_NAME", ""), "tx": tx if tx not in ("UNKN", "UNKNOWN") else "", "off": off,
            "pct": None if off else pct, "rechargeable": charge is not None and 0 <= charge <= 100,
            "mins": run if run is not None and run < 65534 and not off else None, "rf": None if off else rf_bars}


class Mics:
    def __init__(self, data_dir, cfg, log, connect=None):
        self.cfg, self.log = cfg, log
        self.path = os.path.join(data_dir, "mics.json")
        self.connect = connect or (lambda ip: socket.create_connection((ip, PORT), timeout=2))
        self.lock = threading.Lock()
        self.settings = {"receivers": [], "warn": 25}   # receivers: [{"ip", "name", "chans": [wing ch or 0, ...]}]
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d.get("receivers"), list):
                self.settings["receivers"] = d["receivers"]
            if d.get("warn") in (20, 25, 30, 40):
                self.settings["warn"] = d["warn"]
        except (OSError, ValueError, AttributeError):
            pass
        self.read = {}   # ip -> {"at", "error", "chans": {n: fields}}
        self.stop_ev = threading.Event()

    def set(self, b, who):
        """{"receivers": [...], "warn"} -> error or None"""
        if not isinstance(b, dict):
            return "Bad request"
        new = dict(self.settings)
        if "warn" in b:
            if b["warn"] not in (20, 25, 30, 40):
                return "Bad request"
            new["warn"] = b["warn"]
        if "receivers" in b:
            rs = b["receivers"]
            if not isinstance(rs, list) or len(rs) > 16:
                return "Bad request"
            clean = []
            for r in rs:
                ip = r.get("ip") if isinstance(r, dict) else None
                chans = r.get("chans") if isinstance(r, dict) else None
                if not isinstance(ip, str) or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or not isinstance(chans, list) or not 1 <= len(chans) <= 4 or \
                        not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 40 for c in chans):
                    return "Each receiver needs its address (like 192.168.10.60) and, for each of its 1 to 4 channels, the Wing channel it feeds."
                clean.append({"ip": ip, "name": str(r.get("name") or "")[:40], "chans": chans})
            new["receivers"] = clean
        with self.lock:
            self.settings = new
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(new, fh, indent=1)
        os.replace(tmp, self.path)
        self.log.info("mics: %s changed the wireless mics (%d receivers)", who, len(new["receivers"]))
        threading.Thread(target=self.poll, daemon=True).start()
        return None

    def ask(self, ip, n):
        """Ask one receiver about its n channels. -> {channel: fields}"""
        s = self.connect(ip)
        try:
            s.settimeout(1.5)
            s.sendall("".join("< GET %d %s >" % (c, k) for c in range(1, n + 1) for k in ASK).encode("ascii"))
            buf, t0 = b"", time.time()
            want = n * len(ASK)
            while time.time() - t0 < 2.5 and buf.count(b">") < want:
                try:
                    part = s.recv(4096)
                except socket.timeout:
                    break
                if not part:
                    break
                buf += part
        finally:
            s.close()
        return parse(buf.decode("ascii", "replace"))

    def poll(self):
        for r in list(self.settings["receivers"]):
            try:
                got = self.ask(r["ip"], len(r["chans"]))
                v = {"at": time.time(), "error": "", "chans": got}
            except OSError as e:
                v = dict(self.read.get(r["ip"], {}), error="No answer at %s (%s). Is it on, and on the booth network?" % (r["ip"], e.__class__.__name__))
            with self.lock:
                self.read[r["ip"]] = v

    def run(self):
        while not self.stop_ev.is_set():
            if self.settings["receivers"]:
                self.poll()
            self.stop_ev.wait(EVERY)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def view(self, names=None):
        names = names or {}
        mics, problems = [], []
        with self.lock:
            rs, read = list(self.settings["receivers"]), dict(self.read)
        for r in rs:
            got = read.get(r["ip"]) or {}
            if got.get("error"):
                problems.append({"ip": r["ip"], "name": r["name"], "error": got["error"]})
            for i, ch in enumerate(r["chans"], 1):
                m = mic_view((got.get("chans") or {}).get(i, {}))
                m.update(ch=ch, receiver=r["name"] or r["ip"], rx_ch=i, wing_name=names.get(str(ch), "") if ch else "",
                         read=bool((got.get("chans") or {}).get(i)), at=got.get("at", 0))
                mics.append(m)
        low = [m for m in mics if not m["off"] and m["pct"] is not None and m["pct"] < self.settings["warn"]]
        return {"settings": self.settings, "mics": mics, "problems": problems,
                "low": [{"name": m["wing_name"] or m["name"] or "Mic", "pct": m["pct"], "mins": m["mins"]} for m in low]}
