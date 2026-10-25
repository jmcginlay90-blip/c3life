"""Patching for recording and playback: where each of the C3 Life server's Dante channels comes from, what the Wing
calls it, which ones are stereo pairs, and saved layouts.

Recording takes its sound from anywhere on Dante: a Tio input, the Multitracks Mac, the Media Mac, a Wing output.
Each DVS channel is traced to the Wing channel that hears the same thing, so a track is named as on the desk:
  DVS 5 <- Tio "CH9 Bass"; the Wing's card input 6 also takes Tio "CH9 Bass"; Wing channel 9's source is CRD 6 -> "Bass"
  DVS 1 <- Wing card out 1; the Wing sends LCL 1 there; channel 1's source is LCL 1 -> its name
A Wing input set to stereo (ST) makes a pair: its first input is the left, the next one the right. Two DVS channels
that land on the left and right of the same Wing channel are recorded as one stereo track.

Layouts (data/patch.json): {"layouts": {"Sunday": {"saved": "2026-10-25 14:00", "by": "FOH sound",
  "routes": {"5": {"dev": "Y001-box-1-Yamaha-Tio1608", "ch": "CH9 Bass"}}}}}. Routes are kept by name, so a layout
still works when devices get new addresses."""

import json
import os
import re
import threading
import time

MON = re.compile(r"^\s*mon\b|^\s*mon[\s\-_.]", re.I)  # "Mon-Bass", "MON - Acoustic": a copy for the monitors


def wing_device(routes, chans, cfg):
    """The Wing's Dante card as Dante spells it, or ""."""
    want = (cfg.get("dante_wing_name") or "").lower()
    names = {r["rx_device"] for r in routes} | {r["tx_device"] for r in routes} | set(chans)
    for n in sorted(names):
        if (want and n.lower() == want) or (not want and "wing" in n.lower()):
            return n
    return ""


def _num(name, table):
    """A channel's number from its name ({number: name}), or from a leading number ("06 - Bass" -> 6)."""
    for k, v in (table or {}).items():
        if str(v) == str(name):
            return int(k)
    m = re.match(r"\s*(\d+)", str(name))
    return int(m.group(1)) if m else None


def channels_on(strips, inputs, grp, idx):
    """Wing channels that hear input grp/idx -> [(channel, side)], side "L"/"R" for a stereo input, else ""."""
    out = []
    for n, s in sorted(strips.items(), key=lambda x: int(x[0])):
        g, i = s.get("src_grp"), s.get("src_in")
        if g != grp or not isinstance(i, int):
            continue
        st = (inputs.get("%s/%d" % (g, i)) or {}).get("mode") == "ST"
        if i == idx:
            out.append((int(n), "L" if st else ""))
        elif st and i + 1 == idx:
            out.append((int(n), "R"))
    return out


def best(chs, strips):
    """The channel to name a track after: the first that isn't a monitor copy ("Mon-Bass"), else the first."""
    for n, side in chs:
        if not MON.match((strips.get(str(n)) or {}).get("name") or ""):
            return n, side
    return chs[0] if chs else (None, "")


def trace(dvs_routes, routes, chans, strips, inputs, outs, wing):
    """{dvs channel: {"name", "wing_ch", "wing_chs", "side", "src", "from", "from_dev", "from_ch"}} for each routed DVS channel.
    dvs_routes: {"5": {"tx_device", "tx_channel"}} (into this server); routes: every route on the network;
    chans: {device: {"tx": {n: name}, "rx": {n: name}}}; strips/inputs/outs: the Wing's state."""
    wl = wing.lower()
    wing_rx = (chans.get(wing) or {}).get("rx") or {}
    wing_tx = (chans.get(wing) or {}).get("tx") or {}
    hears = {}  # (tx device, tx channel) -> [Wing card input numbers that take it]
    for r in routes:
        if wl and r["rx_device"].lower() == wl:
            n = _num(r["rx_channel"], wing_rx)
            if n:
                hears.setdefault((r["tx_device"].lower(), r["tx_channel"]), []).append(n)
    out = {}
    for k, r in dvs_routes.items():
        if not str(k).isdigit():
            continue
        dev, ch = r.get("tx_device") or "", str(r.get("tx_channel") or "")
        item = {"name": "%s %s" % (dev, ch), "wing_ch": None, "wing_chs": [], "side": "", "src": "", "from": "%s · %s" % (dev, ch),
                "from_dev": dev, "from_ch": ch, "waiting": False}
        chs = []
        if wl and dev.lower() == wl:  # a Wing card output: what the Wing sends there
            num = _num(ch, wing_tx)
            o = outs.get("CRD/%d" % num) if num else None
            if o and o.get("grp") == "OFF":
                item["name"] = "Wing card out %d (off)" % num
            elif o and o.get("grp"):
                item["src"] = "%s/%s" % (o["grp"], o.get("in"))
                chs = channels_on(strips, inputs, o["grp"], int(o.get("in") or 0))
                if not chs:
                    from rta_patch import _src_text
                    item["name"] = _src_text(o["grp"], int(o.get("in") or 0))
            elif num:
                item["name"], item["waiting"] = "Wing card out %d" % num, True
        else:  # anything else: the Wing channels whose card input takes the same source
            for n in hears.get((dev.lower(), ch), []):
                chs += channels_on(strips, inputs, "CRD", n)
            if hears.get((dev.lower(), ch)):
                item["src"] = "CRD/%d" % hears[(dev.lower(), ch)][0]
        if chs:
            n, side = best(chs, strips)
            item.update(name=(strips.get(str(n)) or {}).get("name") or "Ch %d" % n, wing_ch=n, side=side,
                        wing_chs=sorted({c for c, _ in chs}))
        out[int(k)] = item
    return out


def pairs(names, split=()):
    """Stereo pairs among traced DVS channels: k and k+1 on the left and right of the same Wing channel -> {k: k+1}.
    `split`: DVS channels to keep mono."""
    out = {}
    for k in sorted(names):
        a, b = names[k], names.get(k + 1)
        if k in out.values() or not b or k in split:
            continue
        if a.get("wing_ch") and a.get("wing_ch") == b.get("wing_ch") and a.get("side") == "L" and b.get("side") == "R":
            out[k] = k + 1
    return out


class Layouts:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "patch.json")
        self.lock = threading.Lock()
        self.d = {"layouts": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("layouts"), dict):
                self.d["layouts"] = d["layouts"]
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return [{"name": n, "saved": v.get("saved", ""), "by": v.get("by", ""), "count": len(v.get("routes") or {})}
                    for n, v in sorted(self.d["layouts"].items())]

    def get(self, name):
        with self.lock:
            v = self.d["layouts"].get(name)
            return {int(k): dict(r) for k, r in (v.get("routes") or {}).items()} if v else None

    def save(self, name, routes, who):
        name = (name or "").strip()[:40]
        if not name:
            return "A layout needs a name."
        with self.lock:
            if name not in self.d["layouts"] and len(self.d["layouts"]) >= 20:
                return "There are 20 layouts already. Delete one first."
            self.d["layouts"][name] = {"saved": time.strftime("%Y-%m-%d %H:%M"), "by": who,
                                       "routes": {str(k): {"dev": r["tx_device"], "ch": r["tx_channel"]} for k, r in routes.items() if str(k).isdigit()}}
            self._save()
        return None

    def delete(self, name):
        with self.lock:
            if self.d["layouts"].pop(name, None) is None:
                return "That layout isn't there."
            self._save()
        return None


def layout_changes(layout, now):
    """What applying a layout would change on this server's DVS channels -> [{"dvs", "now", "to"}] (to "" = cleared)."""
    out = []
    for k in sorted(set(layout) | {int(x) for x in now if str(x).isdigit()}):
        want = layout.get(k)
        cur = now.get(str(k)) or now.get(k)
        cur_t = (cur["tx_device"], cur["tx_channel"]) if cur else None
        want_t = (want["dev"], want["ch"]) if want else None
        if cur_t != want_t:
            out.append({"dvs": k, "now": "%s · %s" % cur_t if cur_t else "nothing", "to": "%s · %s" % want_t if want_t else "",
                        "dev": want["dev"] if want else "", "ch": want["ch"] if want else ""})
    return out
