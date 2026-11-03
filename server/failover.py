"""Backup Mac: one press moves the Wing's Dante inputs from the main Playback Mac to the backup one, with 10 seconds to undo.

Every Wing card input that is fed from the main Mac (the Multitracks MacBook) is routed instead from the backup Mac's
channel with the same name, or the same number if the names differ. Nothing on the Wing itself changes: the Wing's
channels still listen to the same card inputs, only where Dante sends them from changes (like Dante Controller's grid).

For 10 seconds after a switch, Undo puts back exactly the routes there were before. After that, the same button
switches back the other way (it asks first then, as it's no longer an emergency).

It never runs by itself: only when someone presses it. The Wing must be in Control (only an Admin switches it), as for
playing a recording to the desk. If one route doesn't take, the others still switch (the main Mac may be gone), and the
answer says which didn't. This hasn't been tried on our Dante network yet.

data/failover.json: {"main": "C3-LIfe-Multitracks", "backup": "<the backup Mac's Dante name>"}
"""

import json
import os
import threading
import time

import patching

UNDO_S = 10
FRESH_S = 20


class Failover:
    def __init__(self, data_dir, cfg, log, dante, desk=None, clock=time.monotonic):
        self.path = os.path.join(data_dir, "failover.json")
        self.cfg, self.log, self.dante, self.desk, self.clock = cfg, log, dante, desk, clock
        self.lock = threading.Lock()
        self.settings = {"main": "", "backup": ""}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.settings.update({k: d[k] for k in ("main", "backup") if isinstance(d.get(k), str)})
        except (OSError, ValueError):
            pass
        self.job = None        # the last switch: {"to", "who", "steps", "done", "failed", "busy", "at", "undone"}
        self.cache = None      # (when, {"on", "main_n", "backup_n", "devices"})
        self.reading = False

    # ---- settings ----
    def save(self, main, backup, who):
        main, backup = (main or "").strip()[:64], (backup or "").strip()[:64]
        if not main or not backup:
            return "Choose both Macs."
        if main.lower() == backup.lower():
            return "The backup Mac must be a different device from the main one."
        self.settings = {"main": main, "backup": backup}
        from c3life_server import write_json
        write_json(self.path, self.settings)
        self.cache = None
        self.log.info("backup mac: %s chose %s as the main Playback Mac and %s as the backup", who, main, backup)
        return None

    # ---- reading Dante ----
    def _read(self):
        routes, chans = [], {}
        try:
            routes = self.dante.all_routes() or []
        except Exception:
            routes = []
        try:
            chans = self.dante.channels() or {}
        except Exception:
            chans = {}
        return routes, chans, patching.wing_device(routes, chans, self.cfg)

    def _main(self, routes, wing):
        """The main Mac as Dante spells it: the one chosen, or else the device whose name has "multitrack" in it."""
        want = self.settings["main"].lower()
        for r in routes:
            if r["tx_device"].lower() == want:
                return r["tx_device"]
        if want:
            return self.settings["main"]
        for r in routes:
            if r["rx_device"].lower() == wing.lower() and "multitrack" in r["tx_device"].lower():
                return r["tx_device"]
        return ""

    def plan(self, to="backup", read=None):
        """The routes to change: [{"rx_dev", "rx_ch", "prev_dev", "prev_ch", "tx_dev", "tx_ch"}], notes, error."""
        routes, chans, wing = read or self._read()
        if not wing:
            return [], [], "C3 Life can't find the Wing on Dante."
        main = self._main(routes, wing)
        backup = self.settings["backup"]
        if not main or not backup:
            return [], [], "Choose the main and backup Macs first (Mixer › Dante › Backup Mac)."
        src, dst = (main, backup) if to == "backup" else (backup, main)
        real = {d.lower(): d for d in chans}
        dst = real.get(dst.lower(), dst)
        src_tx = (chans.get(real.get(src.lower(), src)) or {}).get("tx") or {}
        dst_tx = (chans.get(dst) or {}).get("tx") or {}
        if not dst_tx:
            return [], [], "C3 Life can't see %s's channels on Dante. Is it switched on, with Dante Virtual Soundcard running?" % dst
        by_name = {str(v).lower(): str(v) for v in dst_tx.values()}
        steps, notes = [], []
        for r in routes:
            if r["rx_device"].lower() != wing.lower() or r["tx_device"].lower() != src.lower():
                continue
            ch = by_name.get(r["tx_channel"].lower())
            if not ch:
                n = patching._num(r["tx_channel"], src_tx)
                ch = str(dst_tx.get(n) or dst_tx.get(str(n)) or "") if n else ""
            if not ch:
                notes.append("Wing card in %s (%s) has no matching channel on %s, so it stays as it is." % (r["rx_channel"], r["tx_channel"], dst))
                continue
            steps.append({"rx_dev": r["rx_device"], "rx_ch": r["rx_channel"], "prev_dev": r["tx_device"], "prev_ch": r["tx_channel"],
                          "tx_dev": dst, "tx_ch": ch})
        if not steps and not notes:
            return [], [], "Nothing on the Wing comes from %s right now." % src
        return steps, notes, None

    # ---- pressing it ----
    def _control(self):
        st = getattr(self.desk, "settings", None)
        if self.desk is not None and isinstance(st, dict) and st.get("mode") != "control":
            return "The Wing is in Watch only. An Admin switches it to Control on the Mixer first."
        return None

    def switch(self, to, who, wait=False):
        """Start moving the Wing's inputs to "backup" (or back to "main"). -> error or None. The routes change in the background."""
        if to not in ("backup", "main"):
            return "Bad request"
        e = self._control()
        if e:
            return e
        with self.lock:
            if self.job and self.job["busy"]:
                return "It's still switching. Wait a moment, or press Undo."
            job = {"to": to, "who": who, "steps": [], "done": [], "failed": [], "notes": [], "busy": True, "at": None, "undone": False,
                   "cancel": False, "error": ""}
            self.job = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        t.start()
        if wait:
            t.join()
        return None

    def _run(self, job):
        steps, notes, err = self.plan(job["to"])
        job["steps"], job["notes"] = steps, notes
        if err:
            job["error"], job["busy"], job["at"] = err, False, self.clock()
            return
        for st in steps:
            if job["cancel"]:
                break
            e = self.dante.route_any(st["rx_dev"], st["rx_ch"], st["tx_dev"], st["tx_ch"])
            (job["failed"] if e else job["done"]).append(dict(st, error=e) if e else st)
        job["busy"], job["at"] = False, self.clock()
        self.cache = None
        self.log.info("backup mac: %s moved %d Wing Dante inputs to the %s Mac (%s)%s", job["who"], len(job["done"]), job["to"],
                      steps[0]["tx_dev"] if steps else "", ", %d didn't take" % len(job["failed"]) if job["failed"] else "")

    def undo_left(self):
        j = self.job
        if not j or j["undone"] or not j["done"] and not j["busy"]:
            return 0
        if j["busy"]:
            return UNDO_S
        return max(0, int(round(UNDO_S - (self.clock() - j["at"]) + 0.49)))

    def undo(self, who):
        """Within 10 seconds of a switch: put back exactly the routes there were. -> error or None"""
        with self.lock:
            j = self.job
            if not j or j["undone"] or (not j["busy"] and self.undo_left() <= 0):
                return "It's too late to undo. Use the button to switch back."
            j["cancel"], j["undone"] = True, True
        while j["busy"]:  # let the route being made finish, then put them all back
            time.sleep(0.05)
        errs = []
        for st in reversed(j["done"]):
            e = self.dante.route_any(st["rx_dev"], st["rx_ch"], st["prev_dev"], st["prev_ch"])
            if e:
                errs.append("card in %s: %s" % (st["rx_ch"], e))
        self.cache = None
        self.log.info("backup mac: %s undid the switch to the %s Mac (%d routes put back)", who, j["to"], len(j["done"]) - len(errs))
        return ("Some inputs couldn't be put back: " + "; ".join(errs)) if errs else None

    # ---- what the button shows ----
    def _refresh(self):
        try:
            routes, chans, wing = self._read()
            main = self._main(routes, wing) if wing else ""
            backup = self.settings["backup"].lower()
            into = [r for r in routes if wing and r["rx_device"].lower() == wing.lower()]
            mn = sum(1 for r in into if main and r["tx_device"].lower() == main.lower())
            bn = sum(1 for r in into if backup and r["tx_device"].lower() == backup)
            self.cache = (self.clock(), {"main": main, "main_n": mn, "backup_n": bn, "on": "backup" if bn > mn else "main",
                                         "devices": sorted(chans), "wing": wing})
        finally:
            self.reading = False

    def view(self):
        """For Home and Mixer › Dante: which Mac the Wing hears, and the undo countdown. Never waits for Dante."""
        if self.settings["backup"] and (not self.cache or self.clock() - self.cache[0] > FRESH_S) and not self.reading:
            self.reading = True
            threading.Thread(target=self._refresh, daemon=True).start()
        c = (self.cache or (0, {}))[1]
        j = self.job
        out = {"main": self.settings["main"] or c.get("main", ""), "backup": self.settings["backup"], "ready": bool(self.settings["backup"]),
               "on": c.get("on", ""), "main_n": c.get("main_n", 0), "backup_n": c.get("backup_n", 0), "devices": c.get("devices", []),
               "undo_left": self.undo_left(), "busy": bool(j and j["busy"])}
        if j:
            out["last"] = {"to": j["to"], "who": j["who"], "done": len(j["done"]), "of": len(j["steps"]), "undone": j["undone"],
                           "failed": ["card in %s: %s" % (f["rx_ch"], f["error"]) for f in j["failed"]], "notes": j["notes"], "error": j["error"]}
            if not j["busy"] and not j["undone"] and j["done"]:
                out["on"] = j["to"]  # what we just did, before Dante is read again
        return out
