"""Server power (C3 Life server power): restart C3 Life, restart or shut down this computer, or a hard reset with the
smart plug. The screen asks first and counts down (with Undo) before it asks for any of these.

  app      C3 Life closes and Windows' task starts it again (schtasks /run "C3 Life", a few seconds later)
  restart  Windows restarts in 15 seconds (shutdown /r); "abort" (shutdown /a) stops it in that time
  off      Windows shuts down in 15 seconds (shutdown /s); "abort" stops it too
  cycle    the smart plug ("smart_plug": its address) turns this computer's power off for 10 seconds, then back on:
           a Shelly plug (Gen2 /rpc/Switch.Set toggle_after, else Gen1 /relay/0 timer). Only for a frozen computer.

Before any of them, a recording is stopped and saved (what's on the drive is kept), and the service log notes who.
**Not tried on the HP yet.**
"""

import os
import subprocess
import threading
import urllib.request

ACTIONS = ("app", "restart", "off", "cycle", "abort")


class Power:
    def __init__(self, cfg, log, before=None, run=None, exit_fn=None):
        self.cfg, self.log = cfg, log
        self.before = before or (lambda: None)   # stop and save a recording
        self.run = run or self._run              # the commands (the tests replace this)
        self.exit = exit_fn or (lambda: os._exit(0))
        self.pending = ""                        # "restart" or "off" while Windows counts down

    @staticmethod
    def _run(args):
        flags = 0x08000000 | 0x00000008 if os.name == "nt" else 0  # no window; detached, so it outlives C3 Life
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)

    def plug(self):
        ip = (self.cfg.get("smart_plug") or "").strip()
        if not ip:
            return "No smart plug is set up (\"smart_plug\" in config.json, e.g. 192.168.10.5)."
        base = ip if ip.startswith("http") else "http://" + ip
        for path in ("/rpc/Switch.Set?id=0&on=false&toggle_after=10", "/relay/0?turn=off&timer=10"):
            try:
                with urllib.request.urlopen(base + path, timeout=3) as r:
                    if r.status == 200:
                        return None
            except OSError:
                continue
        return "The smart plug at %s didn't answer. Switch it off and on from its own page, or press the power button." % ip

    def do(self, what, who):
        if what not in ACTIONS:
            return "Bad request"
        if os.name != "nt" and self.run is Power._run:
            return "Only the Windows C3 Life server can do this."
        if what == "abort":
            if not self.pending:
                return "Nothing is waiting to happen."
            self.run(["shutdown", "/a"])
            self.log.info("power: %s stopped the %s", who, "restart" if self.pending == "restart" else "shut down")
            self.pending = ""
            return None
        if what == "cycle":
            self.log.warning("power: %s asked the smart plug for a hard reset", who)
            self.before()
            return self.plug()
        self.log.warning("power: %s: %s", who, {"app": "restart C3 Life", "restart": "restart this computer", "off": "shut down this computer"}[what])
        self.before()
        if what == "app":
            # Windows' scheduled task starts C3 Life again a few seconds after it closes
            self.run(["cmd", "/c", "timeout /t 4 /nobreak >nul & schtasks /run /tn \"%s\"" % self.cfg.get("task_name", "C3 Life")])
            threading.Timer(1.5, self.exit).start()
            return None
        self.run(["shutdown", "/r" if what == "restart" else "/s", "/t", "15", "/c", "C3 Life: " + ("restart" if what == "restart" else "shut down") + " (" + who + ")"])
        self.pending = what
        return None
