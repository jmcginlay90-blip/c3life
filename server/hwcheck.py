#!/usr/bin/env python3
"""Hardware check: is the real booth gear talking to C3 Life? Watch only. It changes nothing on any device.

Run it from any Mac or Windows laptop plugged into the booth network (no C3 Life server needed):
    python3 hwcheck.py                      (a Mac)
    py hwcheck.py                           (Windows)
    python3 hwcheck.py --wing 192.168.10.20 --tio 192.168.10.41 --propresenter http://192.168.10.30:50001

It runs the network scan (it only asks), then watches the Wing and the Tio for about 20 seconds with the same code
the C3 Life server uses, in Watch only, and reads ProPresenter's version. It refuses to send anything that would
change a device: every Wing message must be a question (no value) and every Tio line must be a "get".
    python3 hwcheck.py --find-talkback --wing 192.168.10.20      (press TB when asked: finds its address for MD talk)
It writes hwcheck-report-<date>.txt next to where you ran it. Send that file back; it has addresses and names,
no passwords.
"""

import argparse
import ipaddress
import json
import logging
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan as scan_mod  # noqa: E402
import tio as tio_mod  # noqa: E402
import wing as wing_mod  # noqa: E402

WATCH = 20  # seconds


class Report:
    def __init__(self):
        self.lines, self.ok, self.problems = [], 0, 0

    def say(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def good(self, text):
        self.ok += 1
        self.say("  OK    " + text)

    def bad(self, text):
        self.problems += 1
        self.say("  !!    " + text)

    def note(self, text):
        self.say("  ..    " + text)


def my_networks():
    """This laptop's private addresses -> their /24 networks."""
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for probe in ("192.168.10.1", "10.255.255.255"):  # no packet is sent: this only asks the computer which address it would use
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 9))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    out = []
    for ip in sorted(ips):
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback and not a.is_link_local:
            out.append((ip, str(ipaddress.ip_network(ip + "/24", strict=False))))
    return out


def guard_wing(w):
    """Only questions go to the Wing: an OSC message with no value. Anything else is refused and counted."""
    real, w.refused = w.send, []

    def send(packet):
        p = wing_mod.parse(packet)
        if p is None or p[1]:
            w.refused.append(p[0] if p else repr(packet[:30]))
            return False
        return real(packet)
    w.send = send
    w.set = lambda *a, **k: "The hardware check doesn't change anything."
    w.fade = lambda *a, **k: "The hardware check doesn't change anything."


def guard_tio(t):
    real, t.refused = t.send, []

    def send(line):
        if not line.startswith("get "):
            t.refused.append(line)
            return False
        return real(line)
    t.send = send
    t.set = lambda *a, **k: "The hardware check doesn't change anything."


def check_wing(r, ip, data, extra=None):
    r.say()
    r.say("Behringer Wing at %s (Watch only, %d seconds)" % (ip, WATCH))
    w = wing_mod.Wing(data, dict({"wing_ip": ip}, **(extra or {})), logging.getLogger("wing"))
    w.settings["mode"] = "watch"
    guard_wing(w)
    w.start()
    meters_seen = 0
    try:
        w.outputs()  # also read the Dante card and USB outputs' patch (questions only)
        end = time.time() + WATCH
        while time.time() < end:
            time.sleep(1)
            w.out_focus = time.time() + 30
            if w.meter_state().get("meters"):
                meters_seen += 1
        st = w.state()
        outs = w.outputs()
    finally:
        w.close()
    if st["status"] != "connected":
        r.bad("No answer on OSC port %d. Check the Wing's remote control (OSC) settings allow this computer, and its address on the desk." % w.osc_port)
        return
    c = st.get("console") or {}
    r.good("Answers. %s %s, firmware %s" % (c.get("name", ""), c.get("model", ""), c.get("firmware", "")))
    ch = st["strips"]["ch"]
    named = [(n, v) for n, v in sorted(ch.items(), key=lambda x: int(x[0])) if v.get("name")]
    have_fdr = [n for n, v in ch.items() if "fdr" in v]
    (r.good if named else r.bad)("Channel names read: %d of 40" % len(named))
    (r.good if len(have_fdr) >= 40 else r.bad)("Faders read: %d of 40 channels" % len(have_fdr))
    for n, v in named[:12]:
        f = v.get("fdr")
        r.note("Ch %02d %-14s fader %s  mute %s  pan %s" % (int(n), v["name"][:14], "-inf" if f is not None and f <= -144 else ("%+.1f dB" % f if f is not None else "?"),
                                                          {0: "off", 1: "ON"}.get(v.get("mute"), "?"), v.get("pan", "?")))
    ins = st.get("inputs") or {}
    (r.good if ins else r.bad)("Preamps read (gain, 48 V): %d inputs in use" % len(ins))
    order = ["LCL", "AUX", "A", "B", "C", "SC", "USB", "CRD", "MOD", "PLAY", "AES"]
    key = lambda kv: (order.index(kv[0].split("/")[0]) if kv[0].split("/")[0] in order else 99, int(kv[0].split("/")[1]) if kv[0].split("/")[-1].isdigit() else 0)
    for k, v in sorted(ins.items(), key=key)[:8]:
        r.note("Input %-8s gain %s dB  48V %s" % (k, v.get("g", "?"), {0: "off", 1: "ON"}.get(v.get("vph"), "?")))
    (r.good if meters_seen >= WATCH // 2 else r.bad)("Meters arrived for %d of %d seconds (Wing's own connection, TCP 2222 + UDP)" % (meters_seen, WATCH)
                                                    + ("" if meters_seen else ". If 0: a firewall on this computer may block incoming UDP."))
    card = {k: v for k, v in outs.items() if k.startswith("CRD/")}
    if card:
        r.good("Dante card outputs read: %d of 64 (so C3 Life's Set it up for me can patch this Wing)" % len(card))
        used = [(k, v) for k, v in sorted(card.items(), key=lambda kv: int(kv[0].split("/")[1])) if v.get("grp") not in (None, "OFF")]
        for k, v in used[:10]:
            r.note("Card out %-3s sends %s %s" % (k.split("/")[1], v.get("grp"), v.get("in", "")))
        r.note("Main and buses as sources: check above how the desk names them (for example MAIN 1 for Main 1 left).")
    else:
        r.bad("Dante card outputs didn't answer on /io/out/CRD/n. Set it up for me won't patch this Wing until that's fixed; send this report back.")
    (r.good if not w.refused else r.bad)("Nothing changed on the Wing: %d changes refused by the check" % len(w.refused))
    r.note("Compare a few of these with the desk. Then move one fader on the desk, run this again, and check the new value.")


def check_tio(r, ip, port, data):
    r.say()
    r.say("Yamaha Tio1608-D at %s (Watch only, %d seconds)" % (ip, WATCH))
    t = tio_mod.Tio(data, {"tio_ip": ip, "tio_port": port}, logging.getLogger("tio"))
    t.settings["mode"] = "watch"
    guard_tio(t)
    t.start()
    try:
        time.sleep(WATCH)
        st = t.state()
    finally:
        t.close()
    if st["status"] != "connected":
        r.bad("No answer on TCP %d. Is the Tio on, with its control address on the booth network? R Remote must be able to see it." % port)
        return
    got = {n: v for n, v in st["inputs"].items() if v}
    (r.good if len(got) == 16 else r.bad)("Inputs read: %d of 16" % len(got))
    for n, v in sorted(got.items(), key=lambda x: int(x[0]))[:16]:
        r.note("Input %2s gain %s dB  48V %s  low cut %s%s" % (n, v.get("gain", "?"), {0: "off", 1: "ON"}.get(v.get("vph"), "?"),
                                                            {0: "off", 1: "on"}.get(v.get("hpf"), "?"), (" at %g Hz" % v["hpff"]) if "hpff" in v else ""))
    (r.good if not t.refused else r.bad)("Nothing changed on the Tio: %d changes refused by the check" % len(t.refused))
    r.note("Compare with R Remote. Then change one gain in R Remote, run this again, and check it shows here.")


def check_pp(r, url):
    r.say()
    r.say("ProPresenter at %s" % url)
    j = scan_mod._json(url.rstrip("/") + "/version")
    if not isinstance(j, dict):
        r.bad("No answer. In ProPresenter: Settings > Network > Enable Network, and use this port.")
        return
    r.good("Answers: %s" % json.dumps(j)[:200])


def find_talkback(r, ip, secs=15, port=None, now=time.time, say_press=None):
    """MD talk can follow the Wing's own TB button, but the Wing's address for it isn't documented. This subscribes
    (a question: no value is sent), waits a moment, then asks you to press and hold TB: what changes is printed."""
    port = port or 2223
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("0.0.0.0", 0))
    u.settimeout(0.3)
    sub = wing_mod.osc("/*s")
    assert not wing_mod.parse(sub)[1]  # only a question
    seen, changed = {}, {}

    def listen(until, record):
        last = 0.0
        while now() < until:
            if now() - last > 5:
                u.sendto(sub, (ip, port))
                last = now()
            try:
                data, frm = u.recvfrom(65536)
            except socket.timeout:
                continue
            p = wing_mod.parse(data)
            if not p or frm[0] != ip:
                continue
            addr, args = p
            if record and seen.get(addr) != args:
                changed.setdefault(addr, []).append(args)
            seen[addr] = args
    r.say()
    r.say("Find the Wing's talkback button (for MD talk). Don't touch the desk for 3 seconds…")
    listen(now() + 3, False)
    (say_press or (lambda: r.say("Now press and hold TB (talkback A) for 2 seconds, let go, and do it again. %d seconds…" % secs)))()
    listen(now() + secs, True)
    u.close()
    if not changed:
        r.bad("Nothing changed on the Wing. Was TB pressed? Is this the Wing's address (%s)?" % ip)
        return []
    r.good("These changed while TB was pressed (the one that went on and off is the talkback):")
    for a, vals in sorted(changed.items(), key=lambda kv: -len(kv[1]))[:20]:
        r.say("    %-40s %s" % (a, " → ".join(str(v[-1] if len(v) == 3 else (v[0] if v else "")) for v in vals[:6])))
    r.say("Type the talkback one on MD talk › Go green when… › Wing talkback is pressed.")
    return list(changed)


def main(argv=None):
    ap = argparse.ArgumentParser(description="C3 Life hardware check (Watch only; changes nothing)")
    ap.add_argument("--network", help="the booth network to scan, e.g. 192.168.10.0/24 (default: this laptop's)")
    ap.add_argument("--wing", help="the Wing's address, if the scan doesn't find it")
    ap.add_argument("--tio", help="the Tio's address, if the scan doesn't find it")
    ap.add_argument("--propresenter", help="ProPresenter's address, e.g. http://192.168.10.30:50001")
    ap.add_argument("--no-scan", action="store_true", help="skip the network scan")
    ap.add_argument("--find-talkback", action="store_true", help="only find the Wing's talkback button for MD talk (press TB when asked)")
    a = ap.parse_args(argv)
    if a.find_talkback:
        r = Report()
        ip = a.wing or next((w["ip"] for w in wing_mod.find()), None)
        if not ip:
            print("The Wing wasn't found: use --wing 192.168.10.x")
            return 1
        return 0 if find_talkback(r, ip) else 1
    logging.basicConfig(level=logging.WARNING)
    r = Report()
    r.say("C3 Life hardware check · %s · Watch only: nothing on any device is changed" % time.strftime("%Y-%m-%d %H:%M"))
    r.say("This computer: %s, Python %s" % (sys.platform, sys.version.split()[0]))
    nets = my_networks()
    for ip, n in nets:
        r.say("  address %s on %s" % (ip, n))
    net = a.network or next((n for ip, n in nets if n.startswith("192.168.10.")), nets[0][1] if nets else "")
    if not net:
        r.bad("This computer has no network address. Plug it into the booth switch.")
    data = tempfile.mkdtemp(prefix="c3hw-")
    wing_ip, tio_ip, pp = a.wing, a.tio, a.propresenter
    tio_port = tio_mod.PORT
    if not a.no_scan and net:
        r.say()
        r.say("Network scan of %s (only asks; about 10-30 seconds)" % net)
        cfg = {"scan_networks": [net], "propresenter": pp or "http://192.168.10.30:50001", "_my_ips": [ip for ip, _ in nets], "scan_timeout": 0.5}
        sc = scan_mod.Scanner(data, cfg, logging.getLogger("scan"), wing_find=lambda: wing_mod.find())
        res = sc.scan()
        r.say("  %d addresses tried in %s s" % (res["hosts"], res["took"]))
        for f in res["found"]:
            r.good("%-22s %s%s · %s%s" % (scan_mod.TYPES.get(f["kind"], f["kind"]), f["ip"], (":%s" % f["port"]) if f["port"] else "", f["how"],
                                          (" · " + f["detail"]) if f.get("detail") else ""))
        for u in res["unknown"]:
            r.note("No driver: %-15s %s %s" % (u["ip"], u["how"], ("· " + u["hwid"]) if u["hwid"] else ""))
        kinds = {f["kind"] for f in res["found"]}
        for k in ("wing", "tio", "propresenter", "dante"):
            if k not in kinds:
                r.bad("Not found by the scan: %s" % scan_mod.TYPES[k])
        for k, want in (("wing", "wing_ip"), ("tio", "tio_ip")):
            of = [f for f in res["found"] if f["kind"] == k]
            if len(of) > 1:
                r.note("More than one %s found. Checking the first; use --%s to pick." % (scan_mod.TYPES[k], k))
        wing_ip = wing_ip or next((f["ip"] for f in res["found"] if f["kind"] == "wing"), None)
        t = next((f for f in res["found"] if f["kind"] == "tio"), None)
        if t and not tio_ip:
            tio_ip, tio_port = t["ip"], t["port"]
        p = next((f for f in res["found"] if f["kind"] == "propresenter"), None)
        if p and not pp:
            pp = "http://%s:%d" % (p["ip"], p["port"])
    if wing_ip:
        check_wing(r, wing_ip, data)
    else:
        r.say()
        r.bad("Wing: not checked (not found; use --wing 192.168.10.x)")
    if tio_ip:
        check_tio(r, tio_ip, tio_port, data)
    else:
        r.say()
        r.bad("Tio: not checked (not found; use --tio 192.168.10.x)")
    if pp:
        check_pp(r, pp)
    r.say()
    r.say("Done: %d OK, %d to look at." % (r.ok, r.problems))
    name = "hwcheck-report-%s.txt" % time.strftime("%Y%m%d-%H%M")
    try:
        with open(name, "w", encoding="utf-8") as f:
            f.write("\n".join(r.lines) + "\n")
        print("Saved %s. Send it back." % os.path.abspath(name))
    except OSError as e:
        print("Couldn't save the report (%s). Copy the text above instead." % e)
    return 0 if r.problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
