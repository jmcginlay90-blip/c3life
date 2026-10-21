"""Reads Dante routing from the network, so Listen sources can be named after the Dante channels feeding them.

Uses netaudio (pip install netaudio), which finds Dante devices with mDNS and asks each for its subscriptions,
like Dante Controller does. Reading is all it does by itself. If it isn't installed or no device answers, Listen
settings says so and uses the routes saved on Mixer › Dante instead.

route() makes one route (a subscription), the way Dante Controller's grid does: only when someone has said yes to it on
the RTA screen. `netaudio subscription add` replaces whatever that receive channel had. This hasn't been tried on our
Dante network yet.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

REFRESH_S = 30


class DanteReader:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.data = {"status": "starting", "reason": "", "at": "", "device": "", "channels": {}}
        self.started = False

    def command(self):
        exe = self.cfg.get("netaudio")
        if isinstance(exe, list):  # tests: a pretend netaudio, given the same arguments
            return exe + ["--json", "--timeout", "3", "subscription", "list"]
        if exe:
            return [exe, "--json", "--timeout", "3", "subscription", "list"]
        found = shutil.which("netaudio")
        if found:
            return [found, "--json", "--timeout", "3", "subscription", "list"]
        return [sys.executable, "-m", "netaudio", "--json", "--timeout", "3", "subscription", "list"]

    def run_netaudio(self, *args):
        exe = self.cfg.get("netaudio")
        if isinstance(exe, list):  # tests
            base = exe
        else:
            base = [exe] if exe else ([shutil.which("netaudio")] if shutil.which("netaudio") else [sys.executable, "-m", "netaudio"])
        return subprocess.run(base + list(args), capture_output=True, text=True, timeout=25,
                              creationflags=(0x08000000 if os.name == "nt" else 0))

    def channels(self):
        """Every device's channels -> {device: {"tx": {number: name}, "rx": {number: name}}}, or {} if netaudio can't say."""
        try:
            out = self.run_netaudio("--json", "--timeout", "3", "channel", "list")
            data = json.loads(out.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {}
        found = {}

        def add(dev, kind, num, name):
            try:
                found.setdefault(str(dev), {"tx": {}, "rx": {}})[kind][int(num)] = str(name)
            except (TypeError, ValueError):
                pass
        # netaudio's JSON has changed between versions: a list of channels, or channels grouped by device
        if isinstance(data, list):
            for c in data:
                if isinstance(c, dict):
                    kind = "rx" if str(c.get("type") or c.get("direction") or "tx").lower().startswith("rx") else "tx"
                    add(c.get("device_name") or c.get("device"), kind, c.get("number") or c.get("channel_number"), c.get("name") or c.get("channel_name"))
        elif isinstance(data, dict):
            for dev, v in data.items():
                if not isinstance(v, dict):
                    continue
                for kind, keys in (("tx", ("transmitters", "tx_channels", "tx")), ("rx", ("receivers", "rx_channels", "rx"))):
                    chs = next((v[k] for k in keys if k in v), None)
                    if isinstance(chs, dict):
                        for num, c in chs.items():
                            add(dev, kind, (c or {}).get("number", num) if isinstance(c, dict) else num, (c or {}).get("name", num) if isinstance(c, dict) else c)
                    elif isinstance(chs, list):
                        for c in chs:
                            if isinstance(c, dict):
                                add(dev, kind, c.get("number"), c.get("name"))
        return found

    def tx_channels(self):
        """Every device's transmit channels -> {device: {number: name}}."""
        return {d: c["tx"] for d, c in self.channels().items() if c["tx"]}

    def all_routes(self):
        """Every route on the network, not just into this server -> [{"rx_device", "rx_channel", "tx_device", "tx_channel"}]"""
        try:
            out = self.run_netaudio("--json", "--timeout", "3", "subscription", "list")
            subs = json.loads(out.stdout or "[]")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        if isinstance(subs, dict):
            subs = [x for v in subs.values() for x in (v if isinstance(v, list) else [v])]
        return [{"rx_device": str(x.get("rx_device") or ""), "rx_channel": str(x.get("rx_channel") or "%02d" % (x.get("rx_channel_number") or 0)),
                 "tx_device": str(x.get("tx_device") or ""), "tx_channel": str(x.get("tx_channel"))}
                for x in subs if isinstance(x, dict) and x.get("tx_channel") and x.get("tx_device")]

    def config(self, *args):
        """netaudio config (rename a device or a channel). -> error or None"""
        try:
            out = self.run_netaudio("config", *args)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "netaudio isn't installed or didn't answer (%s)." % e.__class__.__name__
        if out.returncode != 0:
            return "Dante didn't take it: %s" % ((out.stderr or out.stdout or "no reason given").strip().splitlines() or ["no reason given"])[-1][:200]
        return None

    def route_any(self, rx_device, rx_channel, tx_device, tx_channel):
        """Route any device's channel into any receiver's channel (to put back routes after a rename). -> error or None"""
        try:
            out = self.run_netaudio("subscription", "add", "--tx-device-name", tx_device, "--tx-channel-name", tx_channel,
                                    "--rx-device-name", rx_device, "--rx-channel-name", rx_channel)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "netaudio isn't installed or didn't answer (%s)." % e.__class__.__name__
        if out.returncode != 0:
            return "Dante didn't take it: %s" % ((out.stderr or out.stdout or "no reason given").strip().splitlines() or ["no reason given"])[-1][:200]
        return None

    def route(self, tx_device, tx_channel, rx_channel_number):
        """Route tx_device's tx_channel into this server's DVS channel. -> error or None, then reads the routes again."""
        rx = self.data.get("channels", {}).get(str(rx_channel_number), {}).get("rx_channel") or "%02d" % int(rx_channel_number)
        try:
            out = self.run_netaudio("subscription", "add", "--tx-device-name", tx_device, "--tx-channel-name", tx_channel,
                                    "--rx-device-name", self.data.get("device_name") or self.cfg.get("dante_dvs_name") or socket.gethostname().split(".")[0], "--rx-channel-name", rx)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "netaudio isn't installed or didn't answer (%s)." % e.__class__.__name__
        if out.returncode != 0:
            return "Dante didn't take it: %s" % ((out.stderr or out.stdout or "no reason given").strip().splitlines() or ["no reason given"])[-1][:200]
        self.refresh()
        return None

    def unroute(self, rx_channel_number):
        """Take the route off this server's DVS channel (it had nothing before). -> error or None"""
        rx = self.data.get("channels", {}).get(str(rx_channel_number), {}).get("rx_channel") or "%02d" % int(rx_channel_number)
        try:
            out = self.run_netaudio("subscription", "remove", "--rx-device-name", self.data.get("device_name") or self.cfg.get("dante_dvs_name") or socket.gethostname().split(".")[0], "--rx-channel-name", rx)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "netaudio isn't installed or didn't answer (%s)." % e.__class__.__name__
        if out.returncode != 0:
            return "Dante didn't take it: %s" % ((out.stderr or out.stdout or "no reason given").strip().splitlines() or ["no reason given"])[-1][:200]
        self.refresh()
        return None

    def dvs_name(self):
        # Dante Virtual Soundcard is named after the computer unless someone renames it in Dante Controller.
        return (self.cfg.get("dante_dvs_name") or socket.gethostname().split(".")[0]).lower()

    def read_once(self):
        try:
            out = subprocess.run(self.command(), capture_output=True, text=True, timeout=25,
                                 creationflags=(0x08000000 if os.name == "nt" else 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"status": "unavailable", "reason": "netaudio isn't installed or didn't answer (%s)" % e.__class__.__name__}
        try:
            subs = json.loads(out.stdout or "[]")
        except ValueError:
            return {"status": "unavailable", "reason": (out.stderr or "netaudio gave an answer that wasn't understood").strip()[:200]}
        if isinstance(subs, dict):  # some versions group them by device
            subs = [x for v in subs.values() for x in (v if isinstance(v, list) else [v])]
        want, chans, devices = self.dvs_name(), {}, set()
        for s in subs:
            if not isinstance(s, dict):
                continue
            rx_dev = str(s.get("rx_device") or "")
            devices.add(rx_dev)
            if rx_dev.lower() != want or not s.get("tx_channel"):
                continue
            real = rx_dev  # as Dante spells it, for routing
            n = s.get("rx_channel_number")
            if not isinstance(n, int):
                continue
            chans[str(n)] = {"tx_device": str(s.get("tx_device") or ""), "tx_channel": str(s.get("tx_channel")), "rx_channel": str(s.get("rx_channel") or ""),
                             "status": (s.get("status") or {}).get("label", "") if isinstance(s.get("status"), dict) else str(s.get("status") or "")}
        if not chans:
            seen = ", ".join(sorted(d for d in devices if d)) or "none"
            return {"status": "unavailable", "reason": "No routes into %s found on the Dante network (devices seen: %s)." % (want, seen)}
        return {"status": "ok", "reason": "", "device": want, "device_name": real, "channels": chans}

    def refresh(self):
        d = self.read_once()
        d["at"] = time.strftime("%H:%M")
        with self.lock:
            if d["status"] != "ok" and self.data.get("channels"):
                d["channels"], d["stale"] = self.data["channels"], True  # keep the last good read
            self.data = d

    def run(self):
        while True:
            self.refresh()
            time.sleep(REFRESH_S)

    def get(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.run, daemon=True).start()
            time.sleep(0.05)
        with self.lock:
            return dict(self.data)
