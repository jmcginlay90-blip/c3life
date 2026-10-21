"""Network scan: find the booth gear, connect what C3 Life knows, and flag what it doesn't.

How it looks (all of it only asks; a scan never changes anything on a device):
  1. The Wing search: the "WING?" broadcast every Wing answers (UDP 2222) with its name, model and serial number.
  2. Bonjour (mDNS): Dante devices, network MIDI sessions (MultiTracks Playback), and the names Macs and web devices give.
  3. Every address on the booth network: try the ports the booth apps use (TCP connect only).
  4. Ask what answered: ProPresenter GET /version, the C3 Life helper GET /hello, Shelly GET /shelly, Companion GET /,
     the Tio "get IO:Current/InCh/HAGain 0 0" (a read), Shure "< GET DEVICE_ID >" (a read).
  5. The ARP table (arp -a): each address's hardware (MAC) address. This is the hardware ID that recognises a device after
     it moves to another address. Trying every address also fills the ARP table, so devices C3 Life has no driver for
     show up too.

What happens after (the rules):
  - Recognised, not set up yet, and the only one of its kind: connects in Watch only.
  - Set up, not answering, and the same hardware ID answers at another address: follows it, in Watch only.
    If it was in Control it goes back to Watch only until an Admin turns Control on again.
  - Two or more of a kind and none is the one set up: nothing changes. The Drivers screen asks which one (use()).
  - Turned off on the Mixer screen: left off.
  - Control is never turned on by a scan.
  - Anything else that answered: "No driver yet". An Admin can hide it ("Not booth gear").

data/scan.json: {"auto": true, "seen": {kind: {"ip", "port", "hwid"}}, "propresenter": "http://...", "hidden": [hwid or ip]}
"""

import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

# The ports the booth apps use, and what each one might be.
PORTS = {50001: "propresenter", 8099: "helper", 80: "http", 49280: "yamaha", 8000: "companion", 2202: "shure"}
# Bonjour service types worth asking for.
MDNS_TYPES = ["_netaudio-arc._udp.local", "_apple-midi._udp.local", "_http._tcp.local", "_shelly._tcp.local"]
# The few network-card makers we're sure of (the first half of the hardware address).
VENDORS = {"001dc1": "Audinate (Dante)", "00a0de": "Yamaha", "000edd": "Shure", "001b66": "Sennheiser",
           "b827eb": "Raspberry Pi", "dca632": "Raspberry Pi", "e45f01": "Raspberry Pi", "d83add": "Raspberry Pi",
           "2ccf67": "Raspberry Pi", "28cdc1": "Raspberry Pi"}
# What each kind is called on the Drivers screen.
TYPES = {"wing": "Behringer Wing", "tio": "Yamaha Tio1608-D", "propresenter": "ProPresenter 7", "helper": "C3 Life helper",
         "companion": "Companion", "shelly": "Smart plug (Shelly)", "shure": "Wireless mic receiver", "dante": "Dante (discovery)",
         "midi": "Network MIDI"}
# The kinds the C3 Life server connects to itself, so a scan can connect them or follow them.
MANAGED = ("wing", "tio", "propresenter")
EVERY = 300  # seconds between scans when "auto" is on


def _mac(s):
    parts = re.split(r"[-:]", s.strip().lower())
    if len(parts) != 6:
        return ""
    try:
        m = "".join("%02x" % int(p, 16) for p in parts)
    except ValueError:
        return ""
    return "" if m in ("000000000000", "ffffffffffff") or int(m[:2], 16) & 1 else m


def parse_arp(text):
    """`arp -a` on Windows, `arp -an` on a Mac, or /proc/net/arp on Linux -> {ip: mac}"""
    out = {}
    for line in text.splitlines():
        ip = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        mac = re.search(r"\b([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})\b", line)
        if ip and mac and _mac(mac.group(1)):
            out[ip.group(1)] = _mac(mac.group(1))
    return out


def read_arp():
    try:
        if sys.platform.startswith("linux") and os.path.exists("/proc/net/arp"):
            with open("/proc/net/arp", encoding="ascii", errors="replace") as f:
                return parse_arp(f.read())
        args = ["arp", "-a"] if os.name == "nt" else ["arp", "-an"]
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console window on Windows
        r = subprocess.run(args, capture_output=True, text=True, timeout=8, **kw)
        return parse_arp(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return {}


# ---- Bonjour (mDNS) ------------------------------------------------------------------------------

def _name(data, i, depth=0):
    labels = []
    while i < len(data) and depth < 20:
        n = data[i]
        if n == 0:
            return ".".join(labels), i + 1
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[i:i + 2])[0] & 0x3FFF
            rest, _ = _name(data, ptr, depth + 1)
            return ".".join(labels + ([rest] if rest else [])), i + 2
        labels.append(data[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n
    return ".".join(labels), i + 1


def mdns_query(types, qu=True):
    q = struct.pack("!HHHHHH", 0, 0, len(types), 0, 0, 0)
    for t in types:
        q += b"".join(bytes([len(p)]) + p.encode() for p in t.split(".")) + b"\0"
        q += struct.pack("!HH", 12, 0x8001 if qu else 1)  # PTR, IN, ask for a unicast answer
    return q


def parse_mdns(data):
    """One mDNS answer -> {"ptr": [(type, instance)], "srv": {instance: (host, port)}, "a": {host: ip}}"""
    out = {"ptr": [], "srv": {}, "a": {}}
    try:
        _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        i = 12
        for _ in range(qd):
            _, i = _name(data, i)
            i += 4
        for _ in range(an + ns + ar):
            nm, i = _name(data, i)
            typ, _, _, ln = struct.unpack("!HHIH", data[i:i + 10])
            i += 10
            rd = data[i:i + ln]
            if typ == 12:
                out["ptr"].append((nm, _name(data, i)[0]))
            elif typ == 33 and ln >= 6:
                out["srv"][nm] = (_name(data, i + 6)[0], struct.unpack("!H", rd[4:6])[0])
            elif typ == 1 and ln == 4:
                out["a"][nm] = socket.inet_ntoa(rd)
            i += ln
    except (struct.error, IndexError):
        pass
    return out


def mdns_browse(types=MDNS_TYPES, timeout=2.0, target=("224.0.0.251", 5353)):
    """-> {ip: {"names": set, "services": {type: instance}}}"""
    found, hosts, srv, ptr = {}, {}, {}, []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.settimeout(0.25)
        s.sendto(mdns_query(types), target)
        end = time.time() + timeout
        while time.time() < end:
            try:
                d, frm = s.recvfrom(9000)
            except socket.timeout:
                continue
            r = parse_mdns(d)
            ptr += r["ptr"]
            srv.update(r["srv"])
            hosts.update(r["a"])
            for t, inst in r["ptr"]:  # a PTR answer from a device that sent no address record: use where it came from
                found.setdefault(frm[0], {"names": set(), "services": {}})["services"][t] = inst
    except OSError:
        pass
    finally:
        s.close()
    for t, inst in ptr:
        host = (srv.get(inst) or ("", 0))[0]
        ip = hosts.get(host)
        if ip:
            e = found.setdefault(ip, {"names": set(), "services": {}})
            e["services"][t] = inst
            e["names"].add(host.replace(".local", ""))
    return found


# ---- asking what answered ------------------------------------------------------------------------

def _get(url, timeout=1.5, limit=65536):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "C3Life-scan"}), timeout=timeout) as r:
            return r.status, r.read(limit).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  (anything that isn't an answer is just "no")
        return 0, ""


def _json(url):
    st, body = _get(url)
    if st != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _talk(ip, port, line, want, timeout=1.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as c:
            c.settimeout(timeout)
            c.sendall(line.encode("ascii"))
            buf, end = b"", time.time() + timeout
            while time.time() < end and b"\n" not in buf and b">" not in buf:
                d = c.recv(1024)
                if not d:
                    break
                buf += d
            t = buf.decode("ascii", "replace").strip()
            return t if t.startswith(want) else ""
    except OSError:
        return ""


def identify(ip, port, guess):
    """-> {"kind", "how", "detail"} or None"""
    if guess in ("propresenter", "http", "helper", "companion"):
        if guess == "helper":
            j = _json("http://%s:%d/hello" % (ip, port))
            if isinstance(j, dict) and j.get("service") == "c3life-helper":
                return {"kind": "helper", "how": "The C3 Life helper answered" + (" · " + j["name"] if j.get("name") else ""),
                        "detail": ", ".join(j.get("apps", []))}
            return None
        if guess == "propresenter":
            j = _json("http://%s:%d/version" % (ip, port))
            if isinstance(j, dict) and ("host_description" in j or "api_version" in j or "ProPresenter" in json.dumps(j)):
                return {"kind": "propresenter", "how": "ProPresenter answered on this port", "detail": str(j.get("host_description") or j.get("name") or "")}
            return None
        if guess == "http":
            j = _json("http://%s:%d/shelly" % (ip, port))
            if isinstance(j, dict) and (j.get("type") or j.get("model") or j.get("gen")):
                return {"kind": "shelly", "how": "A Shelly answered", "detail": str(j.get("model") or j.get("type") or "")}
            st, body = _get("http://%s:%d/" % (ip, port))
            if st:
                t = re.search(r"<title[^>]*>([^<]{1,80})", body, re.I)
                return {"kind": "", "how": "Web page on port %d" % port + (": " + t.group(1).strip() if t else ""), "detail": ""}
            return None
        st, body = _get("http://%s:%d/" % (ip, port))
        if st and "companion" in body.lower():
            return {"kind": "companion", "how": "Companion answered", "detail": ""}
        return {"kind": "", "how": "Something answers on port %d" % port, "detail": ""} if st else None
    if guess == "yamaha":
        r = _talk(ip, port, "get IO:Current/InCh/HAGain 0 0\n", "OK get IO:Current/InCh/HAGain")
        if r:
            return {"kind": "tio", "how": "Yamaha control answered with input 1's gain", "detail": ""}
        return {"kind": "", "how": "Yamaha control port open, but not a Tio's inputs", "detail": ""}
    if guess == "shure":
        r = _talk(ip, port, "< GET DEVICE_ID >", "< REP")
        if r:
            m = re.search(r"\{([^}]*)\}", r)
            return {"kind": "shure", "how": "A Shure receiver answered", "detail": m.group(1).strip() if m else ""}
        return None
    return {"kind": "", "how": "Something answers on port %d" % port, "detail": ""}


def _open(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---- the scanner ---------------------------------------------------------------------------------

class Scanner:
    def __init__(self, data_dir, cfg, log, wing=None, tio=None, wing_find=None):
        self.cfg, self.log, self.wing, self.tio = cfg, log, wing, tio
        self.wing_find = wing_find  # wing.find, passed in so tests can point it at a fake
        self.path = os.path.join(data_dir, "scan.json")
        self.lock = threading.Lock()
        self.settings = {"auto": True, "seen": {}, "propresenter": "", "hidden": []}
        try:
            with open(self.path, encoding="utf-8") as f:
                s = json.load(f)
            for k in self.settings:
                if isinstance(s.get(k), type(self.settings[k])):
                    self.settings[k] = s[k]
        except (OSError, ValueError):
            pass
        if self.settings["propresenter"]:
            cfg["propresenter"] = self.settings["propresenter"]  # it moved, and a scan followed it
        self.running, self.step, self.result = False, "", None
        self.stop = threading.Event()
        self.arp = read_arp
        self.mdns = mdns_browse

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=1)
        os.replace(tmp, self.path)

    # where to look
    def networks(self):
        nets = []
        for n in self.cfg.get("scan_networks") or self.cfg.get("allowed_networks", []):
            try:
                n = ipaddress.ip_network(n, strict=False)
            except ValueError:
                continue
            if n.version == 4 and not n.is_loopback and n.prefixlen >= 22:
                nets.append(n)
        return nets

    def hosts(self):
        if self.cfg.get("scan_hosts") is not None:  # tests, or a fixed list
            return list(self.cfg["scan_hosts"])
        out = []
        for n in self.networks():
            out += [str(h) for h in n.hosts()]
        return out

    def ports(self):
        p = dict(self.cfg.get("scan_ports") or PORTS)
        u = urllib.parse.urlparse(self.cfg.get("propresenter") or "")
        if u.port and u.port not in p:
            p[u.port] = "propresenter"
        return p

    # what's set up now
    def current(self):
        cur = {}
        if self.wing:
            cur["wing"] = {"ip": self.wing.settings["ip"], "port": self.wing.osc_port, "mode": self.wing.settings["mode"]}
        if self.tio:
            cur["tio"] = {"ip": self.tio.settings["ip"], "port": self.tio.port, "mode": self.tio.settings["mode"]}
        u = urllib.parse.urlparse(self.cfg.get("propresenter") or "")
        if u.hostname:
            cur["propresenter"] = {"ip": u.hostname, "port": u.port or 80, "mode": "on"}
        return cur

    def state(self):
        with self.lock:
            return {"running": self.running, "step": self.step, "auto": self.settings["auto"], "result": self.result}

    def start(self, who="", auto=False):
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self.run, args=(who, auto), daemon=True).start()
        return True

    def loop(self, first=20):
        if self.stop.wait(first):
            return
        while not self.stop.is_set():
            if self.settings["auto"]:
                self.start(auto=True)
            self.stop.wait(EVERY)

    def run(self, who="", auto=False):
        try:
            r = self.scan()
            changes = self.apply(r, who or "scan")
            r["changes"] = changes
            r["by"], r["auto"] = who, auto
            with self.lock:
                self.result = r
            if changes:
                self.log.info("scan: %s", "; ".join(changes))
        except Exception as e:  # noqa: BLE001  (a scan must never take the server down)
            self.log.warning("scan failed: %s", e)
        finally:
            with self.lock:
                self.running, self.step = False, ""

    def scan(self, timeout=None):
        t0 = time.time()
        timeout = timeout or float(self.cfg.get("scan_timeout", 0.4))
        found = []  # {"kind", "ip", "port", "how", "detail", "hwid"}

        self.step = "Asking for Wings"
        wings = []
        if self.wing_find:
            try:
                wings = self.wing_find()
            except OSError:
                wings = []
        for w in wings:
            found.append({"kind": "wing", "ip": w["ip"], "port": self.wing.osc_port if self.wing else 2223,
                          "how": "The Wing answered the desk search" + (" · " + w["name"] if w.get("name") else ""),
                          "detail": " ".join(x for x in (w.get("model", ""), w.get("firmware", "")) if x), "serial": w.get("serial", "")})

        self.step = "Looking for names devices announce (Bonjour)"
        names = {}
        try:
            names = self.mdns() or {}
        except OSError:
            pass
        for ip, e in names.items():
            svc = e.get("services", {})
            nm = ", ".join(sorted(e.get("names", []))) or ""
            if any(t.startswith("_netaudio-arc") for t in svc):
                inst = next(v for t, v in svc.items() if t.startswith("_netaudio-arc"))
                found.append({"kind": "dante", "ip": ip, "port": 0, "how": "Dante device · " + inst.split(".")[0], "detail": nm})
            if any(t.startswith("_apple-midi") for t in svc):
                inst = next(v for t, v in svc.items() if t.startswith("_apple-midi"))
                found.append({"kind": "midi", "ip": ip, "port": 5004, "how": "Network MIDI session · " + inst.split(".")[0], "detail": nm})

        self.step = "Checking every address on the booth network"
        ports, hosts = self.ports(), self.hosts()
        tries = [(h, p) for h in hosts for p in ports]
        if "127.0.0.1" not in hosts:
            tries += [("127.0.0.1", p) for p, k in ports.items() if k == "companion"]  # Companion runs on this server
        open_ = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            for (h, p), ok in zip(tries, ex.map(lambda hp: _open(hp[0], hp[1], timeout), tries)):
                if ok:
                    open_.append((h, p))

        self.step = "Asking each one what it is"
        wing_ips = {f["ip"] for f in found if f["kind"] == "wing"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            ids = list(ex.map(lambda hp: identify(hp[0], hp[1], ports[hp[1]]), open_))
        other = {}  # ip -> [what answered] for things we don't recognise
        for (h, p), r in zip(open_, ids):
            if not r:
                continue
            if r["kind"]:
                found.append({"kind": r["kind"], "ip": h, "port": p, "how": r["how"], "detail": r["detail"]})
            elif h not in wing_ips:
                other.setdefault(h, []).append(r["how"])

        self.step = "Reading hardware IDs"
        arp = {}
        try:
            arp = self.arp() or {}
        except OSError:
            pass
        mine = set(self.cfg.get("_my_ips") or [])
        nets = self.networks()
        for f in found:
            f["hwid"] = ("wing:" + f["serial"]) if f.get("serial") else arp.get(f["ip"], "")
            f.pop("serial", None)
        known_ips = {f["ip"] for f in found}
        for ip, mac in arp.items():  # every device that answered on the network, even if no port was open
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if ip in known_ips or ip in mine or ip in other or not any(a in n for n in nets):
                continue
            other[ip] = []
        unknown = []
        for ip, hows in sorted(other.items(), key=lambda x: tuple(int(p) for p in x[0].split("."))):
            mac = arp.get(ip, "")
            if ip in known_ips or ip in self.settings["hidden"] or (mac and mac in self.settings["hidden"]):
                continue
            nm = ", ".join(sorted(names.get(ip, {}).get("names", [])))
            ven = VENDORS.get(mac[:6], "") if mac else ""
            bits = ([nm] if nm else []) + (["made by " + ven + " (from its network card)"] if ven else []) + hows
            unknown.append({"ip": ip, "hwid": mac, "how": " · ".join(bits) or "Answers on the network, but no app port is open"})
        return {"at": time.time(), "took": round(time.time() - t0, 1), "found": found, "unknown": unknown, "hosts": len(hosts)}

    # the rules
    def apply(self, r, who):
        cur, seen, changes = self.current(), self.settings["seen"], []
        rows = []
        for kind in MANAGED:
            c = cur.get(kind)
            of = [f for f in r["found"] if f["kind"] == kind]
            if c is None:
                continue
            at = next((f for f in of if f["ip"] == c["ip"]), None)
            if at:
                if at["hwid"]:
                    seen[kind] = {"ip": at["ip"], "port": at["port"], "hwid": at["hwid"]}
                at["state"] = "off" if c["mode"] == "off" else "connected"
                continue
            if c["mode"] == "off" and c["ip"]:
                for f in of:
                    f["state"] = "off_choice"
                continue
            same = [f for f in of if f["hwid"] and f["hwid"] == (seen.get(kind) or {}).get("hwid")]
            if c["ip"] and len(same) == 1:
                f = same[0]
                was_control = c["mode"] == "control"
                self.connect(kind, f["ip"], f["port"])
                seen[kind] = {"ip": f["ip"], "port": f["port"], "hwid": f["hwid"]}
                f["state"], f["was"] = "moved", c["ip"]
                changes.append("%s moved from %s to %s, so C3 Life followed it%s" % (TYPES[kind], c["ip"], f["ip"],
                               " and put it back to Watch only" if was_control else " (Watch only)"))
            elif not c["ip"] and len(of) == 1:
                f = of[0]
                self.connect(kind, f["ip"], f["port"])
                if f["hwid"]:
                    seen[kind] = {"ip": f["ip"], "port": f["port"], "hwid": f["hwid"]}
                f["state"] = "connected_now"
                changes.append("Found the %s at %s and connected in Watch only" % (TYPES[kind], f["ip"]))
            elif of:
                for f in of:
                    f["state"] = "choose"
            if c["ip"] and not any(f.get("state") == "moved" for f in of):
                rows.append({"kind": kind, "ip": c["ip"], "port": c["port"], "state": "missing"})
        for f in r["found"]:
            f.setdefault("state", "recognised")
            f["type"] = TYPES.get(f["kind"], f["kind"])
        for m in rows:
            m["type"] = TYPES[m["kind"]]
        r["missing"] = rows
        self.save()
        return changes

    def connect(self, kind, ip, port):
        """Connect (or reconnect) in Watch only. Never Control."""
        if kind == "wing" and self.wing:
            self.wing.configure(ip=ip, mode="watch")
        elif kind == "tio" and self.tio:
            self.tio.configure(ip=ip, mode="watch")
        elif kind == "propresenter":
            url = "http://%s:%d" % (ip, port)
            self.cfg["propresenter"] = url
            self.settings["propresenter"] = url

    def use(self, kind, ip, port, who):
        """Someone chose this one (duplicates, or "Use this address"). -> error or None"""
        if kind not in MANAGED:
            return "C3 Life doesn't connect to that itself. Set it on Drivers."
        r = self.result or {}
        f = next((x for x in r.get("found", []) if x["kind"] == kind and x["ip"] == ip and int(x["port"]) == int(port)), None)
        if not f:
            return "That wasn't found in the last scan. Scan again."
        self.connect(kind, ip, f["port"])
        if f.get("hwid"):
            self.settings["seen"][kind] = {"ip": ip, "port": f["port"], "hwid": f["hwid"]}
        self.save()
        for x in r.get("found", []):
            if x["kind"] == kind:
                x["state"] = "connected" if x is f else "recognised"
        r["missing"] = [m for m in r.get("missing", []) if m["kind"] != kind]
        self.log.info("scan: %s chose the %s at %s (Watch only)", who, TYPES[kind], ip)
        return None

    def hide(self, ip, hwid, hidden=True):
        key = hwid or ip
        with self.lock:
            h = [x for x in self.settings["hidden"] if x != key]
            self.settings["hidden"] = h + ([key] if hidden else [])
            if self.result and hidden:
                self.result["unknown"] = [u for u in self.result["unknown"] if (u["hwid"] or u["ip"]) != key]
        self.save()

    def set_auto(self, on):
        self.settings["auto"] = bool(on)
        self.save()
