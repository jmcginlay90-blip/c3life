"""Updates from GitHub, seen and started from the app (Saved setup › Updates, Admins).

The work is done by Update-C3Life.ps1, run by the scheduled task "C3 Life Update" as SYSTEM: only it can read the
GitHub key (the repository is private) and replace the program files. It looks once a day by itself. This side only:
  - shows what it found (data/update/status.json, written by the task) and what's installed (installed.json)
  - asks it to look now, or to install the newest version (data/update/request.json, then starts the task)
It refuses on a Sunday, while recording or playing back, while one is already running, and without the key. The task
checks again, and only ever installs the newest version on main that it found itself, so a request can't choose code.
"""

import json
import os
import subprocess
import time

BUSY = ("checking", "downloading", "backing up", "installing", "starting", "putting back")


class Updater:
    def __init__(self, data_dir, cfg, log):
        self.dir = os.path.join(data_dir, "update")
        self.cfg, self.log = cfg, log
        self.asked = 0.0  # when the task was last started from here

    def _read(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8-sig") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _task(self):
        """The command that starts the task: schtasks on the HP; tests give their own."""
        t = self.cfg.get("update_task")
        if isinstance(t, list) and t:
            return t
        return ["schtasks", "/run", "/tn", "C3 Life Update"] if os.name == "nt" else None

    def why_not(self, st, recorder=None, now=None):
        """Why updating can't happen now, in words ("" = it can)."""
        now = now or time.localtime()
        if not self._task():
            return "Updates are made on the HP (C3 Life on Windows)."
        if st.get("key") is False:
            return "Add the GitHub key on the HP first (Set-GitHubKey.ps1, HP Set-up guide)."
        if st.get("state") in BUSY:
            return "An update is already running."
        if now.tm_wday == 6 and not self.cfg.get("update_any_day"):
            return "Not on a Sunday. Update on a weekday, when nobody needs the booth."
        if recorder is not None and getattr(recorder, "rec", None):
            return "Not while recording. Try again after."
        if recorder is not None and getattr(recorder, "player", None):
            return "Not while playing back to the desk. Press Back to stage first."
        if not st.get("latest"):
            return "Look for updates first."
        if not st.get("behind"):
            return "This is the newest version."
        return ""

    def view(self, recorder=None):
        st, inst, req = self._read("status.json"), self._read("installed.json"), self._read("request.json")
        if req and time.time() - self.asked > 300:  # the task never picked it up
            req = {}
        busy = st.get("state") in BUSY or bool(req)
        return {"installed": inst or None, "latest": st.get("latest"), "new": st.get("new") or [], "behind": bool(st.get("behind")),
                "checked": st.get("checked", ""), "ok": st.get("ok", None), "error": st.get("error", ""), "key": st.get("key", None),
                "state": st.get("state") or ("waiting" if req else "idle"), "busy": busy, "last": st.get("last"),
                "why_not": "" if busy else self.why_not(st, recorder), "can_check": bool(self._task()) and not busy}

    def _start(self, req, who):
        cmd = self._task()
        if not cmd:
            return "Updates are made on the HP (C3 Life on Windows)."
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, "request.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(req, by=who, when=time.strftime("%Y-%m-%d %H:%M")), f)
        os.replace(tmp, os.path.join(self.dir, "request.json"))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=(0x08000000 if os.name == "nt" else 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            return "The update task didn't start (%s). Run Install-C3Life.ps1 again on the HP." % e.__class__.__name__
        if r.returncode != 0:
            return "The update task didn't start: %s" % ((r.stderr or r.stdout or "").strip().splitlines() or ["no reason given"])[-1][:200]
        self.asked = time.time()
        return None

    def check(self, who):
        st = self._read("status.json")
        if st.get("state") in BUSY:
            return "An update is already running."
        self.log.info("update: %s asked to look for updates", who)
        return self._start({"action": "check"}, who)

    def apply(self, sha, who, recorder=None):
        st = self._read("status.json")
        why = self.why_not(st, recorder)
        if why:
            return why
        if not isinstance(sha, str) or sha != (st.get("latest") or {}).get("sha"):
            return "That isn't the newest version any more. Look for updates again."
        self.log.info("update: %s asked to update to %s", who, sha[:7])
        return self._start({"action": "apply", "sha": sha}, who)
