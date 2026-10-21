"""Rename a Dante device or channel, keeping every route that uses it.

Dante routes find their source by name: a receiver asks for "WING-ABC123 · 01". So renaming a device, or a transmit
channel, breaks every route from it until it's routed again. check() lists exactly which routes that is; rename() does
the rename (netaudio config, as Dante Controller would) and then makes those routes again with the new name, each
checked. Receive channel names aren't used by routes, so renaming one breaks nothing.

C3 Life finds the Wing's Dante card, the Tio and its own Dante Virtual Soundcard by name, so when one of those is renamed
the new name is kept in data/dante.json ("wing_name", "tio_name", "dvs_name") and used from then on.
Like the routing, this hasn't been tried on our Dante network yet.
"""

import hashlib
import json
import os
import re
import threading

DEVICE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,29}[A-Za-z0-9])?$")
LOCK = threading.Lock()


def valid_device_name(n):
    if not isinstance(n, str) or not DEVICE_RE.match(n):
        return "A Dante device name is 1 to 31 letters, numbers or hyphens (no spaces), and can't start or end with a hyphen."
    return None


def valid_channel_name(n):
    if not isinstance(n, str) or not n.strip() or len(n.strip()) > 31 or any(c in n for c in "=.@") or any(ord(c) < 32 for c in n):
        return "A Dante channel name is 1 to 31 characters, without = . or @."
    return None


class Names:
    """C3 Life's own record of what the Wing's card, the Tio and this server's DVS are called on Dante."""

    def __init__(self, data_dir, cfg):
        self.path, self.cfg = os.path.join(data_dir, "dante.json"), cfg
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        for k, c in (("wing_name", "dante_wing_name"), ("tio_name", "dante_tio_name"), ("dvs_name", "dante_dvs_name")):
            if isinstance(d.get(k), str) and d[k]:
                cfg[c] = d[k]

    def keep(self, key, value):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d[key] = value
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)
        self.cfg[{"wing_name": "dante_wing_name", "tio_name": "dante_tio_name", "dvs_name": "dante_dvs_name"}[key]] = value


def role_of(dev, cfg, dvs):
    """Which of C3 Life's own devices this is: 'wing', 'tio', 'dvs' or ''."""
    low = dev.lower()
    if low == (dvs or "").lower():
        return "dvs"
    for k, word in (("wing", "wing"), ("tio", "tio")):
        conf = cfg.get("dante_%s_name" % k)
        if (conf and conf == dev) or (not conf and word in low):
            return k
    return ""


def devices(dante, cfg):
    ch = dante.channels()
    dvs = dante.data.get("device_name") or cfg.get("dante_dvs_name") or ""
    return [{"name": d, "tx": {str(n): v for n, v in sorted(c.get("tx", {}).items())}, "rx": {str(n): v for n, v in sorted(c.get("rx", {}).items())},
             "is": role_of(d, cfg, dvs)} for d, c in sorted(ch.items())]


def check(t, dante, cfg):
    """t: {"device", "new"} or {"device", "kind": "tx"|"rx", "number", "new"} -> {"ok", "error"?, "text", "routes", "notes", "id"}"""
    ch = dante.channels()
    dev = t.get("device")
    if dev not in ch:
        return {"ok": False, "error": "Dante doesn't show a device called %s." % dev}
    new = (t.get("new") or "").strip() if isinstance(t.get("new"), str) else None
    subs = dante.all_routes()
    notes = []
    if t.get("kind") is None:
        err = valid_device_name(new)
        if err:
            return {"ok": False, "error": err}
        if new == dev:
            return {"ok": False, "error": "That's its name already."}
        if any(d.lower() == new.lower() for d in ch if d != dev):
            return {"ok": False, "error": "Another Dante device is already called %s." % new}
        hit = [s for s in subs if s["tx_device"] == dev]
        text = "Rename the Dante device %s to %s" % (dev, new)
        role = role_of(dev, cfg, dante.data.get("device_name") or cfg.get("dante_dvs_name") or "")
        if role:
            notes.append({"wing": "This is the Wing's Dante card", "tio": "This is the Tio1608-D", "dvs": "This is the C3 Life server's own Dante Virtual Soundcard"}[role]
                         + ". C3 Life will find it by its new name.")
    else:
        kind, num = t.get("kind"), t.get("number")
        if kind not in ("tx", "rx") or not isinstance(num, int) or isinstance(num, bool):
            return {"ok": False, "error": "Bad request"}
        old = ch[dev].get(kind, {}).get(num)
        if old is None:
            return {"ok": False, "error": "%s has no %s channel %d." % (dev, "transmit" if kind == "tx" else "receive", num)}
        err = valid_channel_name(new)
        if err:
            return {"ok": False, "error": err}
        new = new.strip()
        if new == old:
            return {"ok": False, "error": "That's its name already."}
        if new in ch[dev].get(kind, {}).values():
            return {"ok": False, "error": "%s already has a %s channel called %s." % (dev, "transmit" if kind == "tx" else "receive", new)}
        hit = [s for s in subs if s["tx_device"] == dev and s["tx_channel"] == old] if kind == "tx" else []
        text = "Rename %s's %s channel %d from %s to %s" % (dev, "transmit" if kind == "tx" else "receive", num, old, new)
        if kind == "rx":
            notes.append("Routes don't use receive channel names, so nothing else changes.")
    routes = [{"rx_device": s["rx_device"], "rx_channel": s["rx_channel"], "tx_device": s["tx_device"], "tx_channel": s["tx_channel"],
               "text": "%s · %s ← %s · %s" % (s["rx_device"], s["rx_channel"], s["tx_device"], s["tx_channel"])} for s in hit]
    body = {"t": {k: t.get(k) for k in ("device", "kind", "number")}, "new": new, "routes": routes}
    return {"ok": True, "text": text, "routes": routes, "notes": notes, "id": hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]}


def rename(t, pid, dante, cfg, names):
    """Do it, if it's still what was checked; then route again everything that used the old name. -> (status, body)"""
    with LOCK:
        c = check(t, dante, cfg)
        if not c["ok"]:
            return 400, c
        if c["id"] != pid:
            return 409, {"ok": False, "error": "Something changed on Dante since you checked. Check again.", "check": c}
        dev, new = t["device"], t["new"].strip()
        role = role_of(dev, cfg, dante.data.get("device_name") or cfg.get("dante_dvs_name") or "") if t.get("kind") is None else ""
        if t.get("kind") is None:
            err = dante.config("--device-name", dev, "--set-device-name", new)
        else:
            err = dante.config("--device-name", dev, "--channel-type", t["kind"], "--channel-number", str(t["number"]), "--set-channel-name", new)
        results = [{"text": c["text"], "ok": not err, "error": err or ""}]
        if err:
            return 200, {"ok": False, "results": results}
        if role:
            names.keep(role + "_name", new)
        for r in c["routes"]:
            tx_dev = new if t.get("kind") is None else r["tx_device"]
            tx_ch = new if t.get("kind") == "tx" else r["tx_channel"]
            e = dante.route_any(r["rx_device"], r["rx_channel"], tx_dev, tx_ch)
            results.append({"text": "Route again: %s · %s ← %s · %s" % (r["rx_device"], r["rx_channel"], tx_dev, tx_ch), "ok": not e, "error": e or ""})
        dante.refresh()
        return 200, {"ok": all(x["ok"] for x in results), "results": results}
