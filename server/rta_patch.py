"""Set up the RTA's routing for you: the Wing output and the Dante route, after asking.

plan() works out what would change so the room mic (or the desk reference) arrives where C3 Life listens:
  - the Wing: which Dante card output (or USB output) sends that source. One that already sends it is used as it is;
    otherwise the first card output that's Off. Main, bus and matrix are sent as their left side.
  - Dante: route that Wing card channel (or the Tio's channel) into the C3 Life server's Dante Virtual Soundcard channel.
The RTA screen shows every step, from what to what, and only after Yes does apply() make them, one at a time, each read
back to check it took. Anything that changed in between (someone else patched it) stops it. undo() puts them back.
AES67 flows and "another Dante device" are left to Dante Controller.

Wing card output n is taken to be the Wing's Dante transmit channel n. Like the Wing output names, this hasn't been
tried on our Wing and Dante network yet.
"""

import hashlib
import json
import threading
import time

LAST = {}  # which -> the steps last made, to put back
LOCK = threading.Lock()


def _wing_src(v):
    """"LCL/8" -> ("LCL", 8); "MAIN/1" or "MAIN/1/L" -> ("MAIN", 1), "MAIN/1/R" -> ("MAIN", 2); None for TIO and DANTE."""
    p = v.split("/")
    if p[0] in ("TIO", "DANTE") or len(p) < 2 or not p[1].isdigit():
        return None
    n = int(p[1])
    if p[0] in ("MAIN", "BUS", "MTX"):
        return (p[0], 2 * n if len(p) == 3 and p[2] == "R" else 2 * n - 1)
    return (p[0], n)


def _src_text(grp, idx, names=None):
    if grp in ("MAIN", "BUS", "MTX"):
        return {"MAIN": "Main", "BUS": "Bus", "MTX": "Matrix"}[grp] + " %d %s" % ((idx + 1) // 2, "L" if idx % 2 else "R")
    if grp == "OFF" or not grp:
        return "Off"
    base = {"LCL": "Local In", "AUX": "Aux In", "A": "AES50 A", "B": "AES50 B", "C": "AES50 C", "CRD": "Card in", "USB": "USB in"}.get(grp, grp)
    return "%s %d" % (base, idx)


def _find_device(tx, word, configured):
    if configured:
        return configured if configured in tx else None, None
    hits = [d for d in tx if word in d.lower()]
    if len(hits) == 1:
        return hits[0], None
    return None, ("Dante shows more than one %s: %s. Set which one in c3life.json." % (word, ", ".join(sorted(hits))) if hits else None)


def plan(which, d, wing, dante, cfg):
    """-> {"steps": [...], "blocked": [reasons], "notes": [...], "id": "..."}. Stereo plans the left and the right;
    a desk reference with subs added plans the subs' two sides as well."""
    x = d[which]
    steps, blocked, notes, taken = [], [], [], set()
    sides = [("left", x["from"], x["ch"]), ("right", x["from2"], x["ch2"])] if x.get("stereo") else [("", x["from"], x["ch"])]
    sub = x.get("subs") or {}
    if sub.get("on"):
        sides += [("subs left", sub["from"], sub["ch"]), ("subs right", sub["from2"], sub["ch2"])]
    for side, f, c in sides:
        _plan_one({"from": f, "via": x["via"], "ch": c}, wing, dante, cfg, steps, blocked, notes, taken, side)
        if blocked:
            break
    return _done(steps, blocked, notes)


def _plan_one(x, wing, dante, cfg, steps, blocked, notes, taken, side):
    """Plan one channel (a mono signal, or one side of a stereo one) into steps, blocked and notes."""
    tag = (" (%s)" % side) if side else ""
    src = _wing_src(x["from"])
    if x["via"] == "aes67" or x["from"] == "DANTE":
        blocked.append("AES67 flows and other Dante devices are set up in Dante Controller. The steps above say what to set.")
        return
    outs = None
    if src or x["via"] == "usb":
        if wing is None or wing.status()["status"] != "connected":
            blocked.append("The Wing isn't connected, so C3 Life can't patch it.")
            return
        outs = wing.outputs()
        need = "USB/" if x["via"] == "usb" else "CRD/"
        if sum(1 for k in outs if k.startswith(need)) < (48 if need == "USB/" else 64):  # every one, so a free one is really free
            blocked.append("C3 Life is still reading the Wing's outputs. Try again in a few seconds.")
            return
    if x["via"] == "usb":
        if not src:
            blocked.append("A Tio input has to reach the Wing over Dante first. Route it in Dante Controller, then choose that Wing card input here.")
            return
        key = "USB/%d" % x["ch"]
        cur = outs.get(key)
        if cur is None:
            blocked.append("The Wing hasn't said what USB output %d sends yet." % x["ch"])
        elif (cur.get("grp"), cur.get("in")) == src:
            notes.append("The Wing already sends %s to USB output %d." % (_src_text(*src), x["ch"]))
        else:
            steps.append({"kind": "wing", "out": key, "from": {"grp": cur.get("grp"), "in": cur.get("in")}, "to": {"grp": src[0], "in": src[1]},
                          "text": "Wing: USB output %d from %s to %s%s" % (x["ch"], _src_text(cur.get("grp"), cur.get("in", 0)), _src_text(*src), tag)})
        return
    # by Dante Virtual Soundcard
    tx = dante.tx_channels() if dante is not None else {}
    if not tx:
        blocked.append("Dante didn't list its channels (netaudio isn't installed, or no Dante device answered).")
        return
    if src:
        dev, why = _find_device(tx, "wing", cfg.get("dante_wing_name"))
        if why:
            blocked.append(why)
        elif not dev:
            blocked.append("Dante doesn't show the Wing's Dante card.")
        card = sorted((k for k, v in outs.items() if k.startswith("CRD/") and (v.get("grp"), v.get("in")) == src), key=lambda k: int(k.split("/")[1]))
        if card:
            n = int(card[0].split("/")[1])
            notes.append("Wing card output %d already sends %s." % (n, _src_text(*src)))
        else:
            free = sorted((k for k, v in outs.items() if k.startswith("CRD/") and v.get("grp") == "OFF" and k not in taken), key=lambda k: int(k.split("/")[1]))
            if not free:
                blocked.append("Every Wing card output is in use. Free one on the Wing first.")
                return
            n = int(free[0].split("/")[1])
            taken.add(free[0])
            steps.append({"kind": "wing", "out": "CRD/%d" % n, "from": {"grp": "OFF", "in": outs[free[0]].get("in", 0)}, "to": {"grp": src[0], "in": src[1]},
                          "text": "Wing: card output %d from Off to %s%s" % (n, _src_text(*src), tag)})
        if dev:
            name = tx[dev].get(n)
            if not name:
                blocked.append("Dante doesn't list channel %d on %s." % (n, dev))
            else:
                _dante_step(steps, notes, dante, dev, name, x["ch"], tag)
    else:  # a Tio input, straight on Dante
        dev, why = _find_device(tx, "tio", cfg.get("dante_tio_name"))
        n = int(x["from"].split("/")[1])
        if why or not dev:
            blocked.append(why or "Dante doesn't show the Tio1608-D.")
        elif not tx[dev].get(n):
            blocked.append("Dante doesn't list channel %d on %s." % (n, dev))
        else:
            _dante_step(steps, notes, dante, dev, tx[dev][n], x["ch"], tag)
    return


def _dante_step(steps, notes, dante, dev, name, ch, tag=""):
    cur = dante.get().get("channels", {}).get(str(ch))
    if cur and cur.get("tx_device") == dev and cur.get("tx_channel") == name:
        notes.append("Dante already routes %s · %s to channel %d%s." % (dev, name, ch, tag))
        return
    was = "%s · %s" % (cur["tx_device"], cur["tx_channel"]) if cur else "nothing"
    steps.append({"kind": "dante", "ch": ch, "from": ({"tx_device": cur["tx_device"], "tx_channel": cur["tx_channel"]} if cur else None),
                  "to": {"tx_device": dev, "tx_channel": name}, "text": "Dante: C3 Life server channel %d from %s to %s · %s%s" % (ch, was, dev, name, tag)})


def _done(steps, blocked, notes):
    pid = hashlib.sha1(json.dumps([steps, blocked], sort_keys=True).encode()).hexdigest()[:16]
    return {"steps": steps, "blocked": blocked, "notes": notes, "id": pid}


def _make(steps, wing, dante, undo=False):
    """Make each step and read it back. Stops at the first that doesn't take. -> [results]"""
    out = []
    for st in steps:
        want = st["from"] if undo else st["to"]
        if st["kind"] == "wing":
            err = wing.set_out(st["out"], want["grp"], int(want["in"]))
            if not err:
                end, ok = time.time() + 5, False
                while time.time() < end and not ok:
                    time.sleep(0.25)
                    got = wing.outputs().get(st["out"], {})
                    ok = got.get("grp") == want["grp"] and (want["grp"] == "OFF" or got.get("in") == int(want["in"]))
                err = None if ok else "The Wing didn't confirm %s. Check it on the desk." % st["out"]
        else:
            if want is None:
                err = dante.unroute(st["ch"])
            else:
                err = dante.route(want["tx_device"], want["tx_channel"], st["ch"])
                if not err:
                    got = dante.get().get("channels", {}).get(str(st["ch"])) or {}
                    if got.get("tx_device") != want["tx_device"] or got.get("tx_channel") != want["tx_channel"]:
                        err = "Dante didn't show the new route on channel %d yet. Check it in Dante Controller." % st["ch"]
        out.append({"text": st["text"], "ok": not err, "error": err or ""})
        if err:
            break
    return out


def apply(which, pid, d, wing, dante, cfg):
    """Make the plan the person said yes to, if it's still the same. -> (status, body)"""
    with LOCK:
        p = plan(which, d, wing, dante, cfg)
        if p["id"] != pid:
            return 409, {"ok": False, "error": "Something changed since you checked (someone may have patched it). Check again.", "plan": p}
        if p["blocked"] or not p["steps"]:
            return 409, {"ok": False, "error": (p["blocked"] or ["Nothing to change."])[0], "plan": p}
        res = _make(p["steps"], wing, dante)
        done = [st for st, r in zip(p["steps"], res) if r["ok"]]
        if done:
            LAST[which] = done
        return 200, {"ok": all(r["ok"] for r in res), "results": res, "can_undo": bool(done)}


def undo(which, wing, dante):
    with LOCK:
        steps = LAST.pop(which, None)
        if not steps:
            return 409, {"ok": False, "error": "Nothing to put back."}
        res = _make(list(reversed(steps)), wing, dante, undo=True)
        return 200, {"ok": all(r["ok"] for r in res), "results": res}
