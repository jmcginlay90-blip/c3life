"""Help › Run system check, and Send support report: what the C3 Life server can see right now, in words.

Every line is something the server already knows (it asks nothing new of any device, and changes nothing):
the server itself, its sound (DVS), each device that's set up (the same list as the warning on every screen),
Dante, the last network scan, the recording drive, storage, the internet and Listen.

  check(parts)      -> [{"name", "ok": True/False/None, "state", "fix"}]   None = nothing to check (not set up)
  report_text(...)  -> the support report: the check, the last 200 lines of the service log, and the settings
                       with every password, PIN, key and token taken out
"""

import json
import os
import shutil
import socket
import time

SECRET = ("pass", "pin", "key", "token", "secret", "salt", "hash")


def _gb(n):
    return "%.0f GB" % (n / 1e9) if n >= 1e9 else "%.0f MB" % (n / 1e6)


def internet(timeout=1.5):
    """Can the server reach the internet (a DNS server)? Only asked when someone runs the check."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 53), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def check(p, now=time.time, online=internet):
    """p: {"version", "start", "cfg", "health", "engine", "recorder", "scanner", "dante", "listen"} (any may be missing)."""
    out = []
    add = lambda name, ok, state, fix="": out.append({"name": name, "ok": ok, "state": state, "fix": fix})
    up = int(now() - p.get("start", now()))
    add("C3 Life app on the C3 Life server", True, "OK · version %s · running %s" % (p.get("version", "?"), "%dh %02dm" % (up // 3600, up // 60 % 60)))

    e = p.get("engine")
    if e:
        v = e.view()
        ok = v["status"] == "running"
        add("Sound (Dante Virtual Soundcard)", ok, "%s · %d channels" % ("Running" if ok else v["status"].capitalize(), v["channels"]) if ok else (v["reason"] or v["status"]),
            "" if ok else "Check Dante Virtual Soundcard is running on the C3 Life server, in ASIO mode with 64 x 64 channels.")
    else:
        add("Sound (Dante Virtual Soundcard)", None, "Not used (audio_engine is off)")

    h = p.get("health")
    for d in (h.devices() if h else []):
        if d.get("left"):
            add(d["name"], None, "Left out on Devices")
        else:
            add(d["name"], d["ok"], "OK" if d["ok"] else "Not answering", "" if d["ok"] else d["why"])

    dn = p.get("dante")
    if dn and hasattr(dn, "get"):
        d = dn.get()
        ok = d.get("status") == "ok"
        n = len(d.get("channels") or {})
        add("Dante routing to the C3 Life server", ok if d.get("status") != "starting" else None,
            "%d DVS channels seen%s" % (n, "" if not d.get("stale") else " · from the last good read") if ok else (d.get("reason") or d.get("status", "")),
            "" if ok else "Check Dante Virtual Soundcard is running, and the Dante cable from the C3 Life server to the switch.")

    sc = p.get("scanner")
    r = sc.state().get("result") if sc else None
    if r:
        mins = int((now() - r["at"]) / 60)
        add("Last network scan", True, "%d found · %d without a driver · %s" % (len(r["found"]), len(r["unknown"]), "just now" if mins < 1 else "%d min ago" % mins))
    elif sc:
        add("Last network scan", None, "No scan yet · Network › Scan now")

    rec = p.get("recorder")
    if rec:
        v = rec.view()
        dr = v.get("drive")
        if v.get("recording"):
            add("Recording", True, "Recording to %s · %d tracks" % (v["recording"]["where"], v["recording"]["tracks"]))
        elif dr:
            add("Recording drive", True, "%s · %s free" % (dr.get("label", "Drive"), dr.get("free_text", "")))
        else:
            add("Recording drive", None, "None plugged in")

    data = (p.get("cfg") or {}).get("data_dir") or "."
    try:
        u = shutil.disk_usage(data if os.path.isdir(data) else ".")
        pct = int(100 * u.free / u.total)
        add("Storage on the C3 Life server", pct >= 10, "%d%% free · %s of %s" % (pct, _gb(u.free), _gb(u.total)),
            "" if pct >= 10 else "Copy old recordings to a drive (Record › Recordings) and delete them from the server.")
    except OSError:
        pass

    net = online()
    add("Internet", True if net else None, "Connected · reports send straight away" if net else "No internet · reports wait on the server and send later")

    ls = p.get("listen")
    if ls is not None and hasattr(ls, "sources"):
        n = sum(len(x.listeners) for x in list(ls.sources.values()))
        add("Listen", True, "%d listening now" % n)
    return out


def clean(cfg):
    """The settings without passwords, PINs, keys or tokens (or anything starting with _)."""
    def walk(v, k=""):
        if any(s in k.lower() for s in SECRET):
            return "(hidden)" if v else ""
        if isinstance(v, dict):
            return {kk: walk(vv, str(kk)) for kk, vv in v.items() if not str(kk).startswith("_")}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    return walk(cfg)


def report_text(who, note, results, log_lines, cfg):
    lines = ["C3 Life support report", "From: %s" % who, "At: %s" % time.strftime("%Y-%m-%d %H:%M"), ""]
    if note:
        lines += ["What happened:", note, ""]
    lines.append("System check:")
    for r in results:
        mark = "OK " if r["ok"] else ("-- " if r["ok"] is None else "!! ")
        lines.append("  %s%s: %s%s" % (mark, r["name"], r["state"], (" (%s)" % r["fix"]) if r["fix"] else ""))
    lines += ["", "Service log (last %d):" % len(log_lines)]
    lines += ["  %s %s %s" % (x["at"], x["level"].upper()[:4], x["text"]) for x in log_lines]
    lines += ["", "Settings (no passwords, PINs or keys):", json.dumps(clean(cfg), indent=1, sort_keys=True)]
    return "\n".join(lines)
