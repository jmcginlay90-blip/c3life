"""The C3 Life server's own network connections, on Windows (Settings › Network connections): what's connected, the
Wi-Fi networks it can hear, and joining, forgetting or ordering the saved ones. It uses Windows' own tools:

  PowerShell Get-NetAdapter / Get-NetIPAddress    the connections, their addresses and state
  netsh wlan show interfaces                      the Wi-Fi it's on now
  netsh wlan show networks mode=bssid             the Wi-Fi networks it can hear
  netsh wlan show profiles                        the saved ones, in the order Windows tries them
  netsh wlan add profile / connect                join one (the password goes in a profile file that's deleted after)
  netsh wlan delete profile / set profileorder    forget one, or change the order

Windows uses the wired connection first whenever it's plugged in (its own automatic metrics), and Wi-Fi when it isn't.
Joining, forgetting and the order are Admin only, and the screen asks first. Passwords are never logged or kept.
**Not tried on the HP yet**, and netsh's words are English: on a Windows in another language the lists come back empty.
"""

import json
import os
import re
import subprocess
import tempfile
from xml.sax.saxutils import escape

PS_ADAPTERS = ("Get-NetAdapter | ForEach-Object { $ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
               "-ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress; [pscustomobject]@{name=$_.Name; "
               "desc=$_.InterfaceDescription; status=[string]$_.Status; speed=[string]$_.LinkSpeed; media=[string]$_.PhysicalMediaType; ip=$ip} } "
               "| ConvertTo-Json")
SSID_OK = re.compile(r"^[^\"<>&\x00-\x1f]{1,32}$")


def run(args, timeout=15):
    flags = 0x08000000 if os.name == "nt" else 0  # no console window
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    return p.stdout or ""


def _kv(line):
    k, sep, v = line.partition(":")
    return (k.strip(), v.strip()) if sep else (None, None)


def parse_adapters(text):
    try:
        d = json.loads(text or "[]")
    except ValueError:
        return []
    if isinstance(d, dict):
        d = [d]
    out = []
    for a in d:
        wifi = "802.11" in (a.get("media") or "") or re.search(r"wi-?fi|wireless|wlan", a.get("desc") or "", re.I) is not None
        out.append({"name": a.get("name") or "", "desc": a.get("desc") or "", "up": (a.get("status") or "").lower() == "up",
                    "status": a.get("status") or "", "speed": a.get("speed") or "", "ip": a.get("ip") or "", "wifi": bool(wifi)})
    return out


def parse_interfaces(text):
    """netsh wlan show interfaces -> {"ssid", "signal", "state"} of the first Wi-Fi."""
    out = {}
    for line in text.splitlines():
        k, v = _kv(line)
        if k == "SSID" and "ssid" not in out:
            out["ssid"] = v
        elif k == "Signal" and "signal" not in out:
            out["signal"] = int(v.rstrip("%")) if v.rstrip("%").isdigit() else None
        elif k == "State" and "state" not in out:
            out["state"] = v
        elif k == "Name" and "iface" not in out:
            out["iface"] = v
    return out


def parse_networks(text):
    """netsh wlan show networks mode=bssid -> [{"ssid", "auth", "signal", "band"}], strongest first."""
    nets, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^SSID \d+\s*:\s*(.*)$", line.strip())
        if m:
            cur = {"ssid": m.group(1).strip(), "auth": "", "signal": 0, "band": ""}
            nets.append(cur)
            continue
        k, v = _kv(line)
        if not cur or not k:
            continue
        if k == "Authentication":
            cur["auth"] = v
        elif k == "Signal":
            cur["signal"] = max(cur["signal"], int(v.rstrip("%")) if v.rstrip("%").isdigit() else 0)
        elif k == "Band" or k == "Radio type":
            cur["band"] = cur["band"] or v
    return sorted([n for n in nets if n["ssid"]], key=lambda n: -n["signal"])


def parse_profiles(text):
    return [v for k, v in (_kv(line) for line in text.splitlines()) if k and k.endswith("User Profile") and v]


def profile_xml(ssid, password, auth):
    """A Wi-Fi profile for netsh wlan add profile. auth: what the scan said (WPA2-Personal, WPA3-Personal, Open)."""
    a = (auth or "").lower()
    if a.startswith("open") or not password:
        sec = "<authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption>"
    else:
        kind = "WPA3SAE" if "wpa3" in a else "WPA2PSK"
        sec = ("<authEncryption><authentication>%s</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>"
               "<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>%s</keyMaterial></sharedKey>" % (kind, escape(password)))
    s = escape(ssid)
    return ('<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1"><name>%s</name>'
            '<SSIDConfig><SSID><name>%s</name></SSID></SSIDConfig><connectionType>ESS</connectionType><connectionMode>auto</connectionMode>'
            '<MSM><security>%s</security></MSM></WLANProfile>' % (s, s, sec))


class Network:
    def __init__(self, log, run_fn=None):
        self.log = log
        self.run = run_fn or run

    def ok(self):
        return os.name == "nt" or self.run is not run

    def view(self):
        if not self.ok():
            return {"error": "Only the Windows C3 Life server can show its connections.", "adapters": [], "wifi": {}, "saved": []}
        try:
            return {"error": "", "adapters": parse_adapters(self.run(["powershell", "-NoProfile", "-Command", PS_ADAPTERS])),
                    "wifi": parse_interfaces(self.run(["netsh", "wlan", "show", "interfaces"])),
                    "saved": parse_profiles(self.run(["netsh", "wlan", "show", "profiles"]))}
        except (OSError, subprocess.SubprocessError) as e:
            return {"error": "Windows didn't answer (%s)." % e.__class__.__name__, "adapters": [], "wifi": {}, "saved": []}

    def scan(self):
        if not self.ok():
            return []
        try:
            return parse_networks(self.run(["netsh", "wlan", "show", "networks", "mode=bssid"]))
        except (OSError, subprocess.SubprocessError):
            return []

    def join(self, ssid, password, auth, who):
        """-> error or None"""
        if not self.ok():
            return "Only the Windows C3 Life server can join a network."
        if not isinstance(ssid, str) or not SSID_OK.match(ssid) or not isinstance(password, str) or len(password) > 63 or \
                (password and len(password) < 8):
            return "Check the network name, and the password (8 to 63 letters)."
        if not password and not (auth or "").lower().startswith("open"):
            # a saved one: Windows already has its password
            if ssid not in self.view()["saved"]:
                return "Type the password for %s." % ssid
            out = self.run(["netsh", "wlan", "connect", "name=" + ssid])
            self.log.warning("network: %s joined the saved Wi-Fi %r", who, ssid)
            return None if "successfully" in out.lower() else "Windows couldn't connect: %s" % (out.strip().splitlines() or ["no answer"])[-1][:160]
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(profile_xml(ssid, password, auth))
            out = self.run(["netsh", "wlan", "add", "profile", "filename=" + path, "user=all"])
        finally:
            try:
                os.remove(path)  # the password was in it
            except OSError:
                pass
        if "added" not in out.lower() and "updated" not in out.lower():
            return "Windows didn't save it: %s" % (out.strip().splitlines() or ["no answer"])[-1][:160]
        out = self.run(["netsh", "wlan", "connect", "name=" + ssid])
        self.log.warning("network: %s joined the Wi-Fi %r", who, ssid)
        if "successfully" not in out.lower():
            return "Saved, but Windows couldn't connect yet: %s" % (out.strip().splitlines() or ["no answer"])[-1][:160]
        return None

    def forget(self, ssid, who):
        if not self.ok():
            return "Only the Windows C3 Life server can do this."
        if not isinstance(ssid, str) or not SSID_OK.match(ssid):
            return "Bad request"
        out = self.run(["netsh", "wlan", "delete", "profile", "name=" + ssid])
        self.log.warning("network: %s forgot the Wi-Fi %r", who, ssid)
        return None if "deleted" in out.lower() else "Windows didn't forget it: %s" % out.strip()[:160]

    def order(self, ssids, who):
        if not self.ok():
            return "Only the Windows C3 Life server can do this."
        if not isinstance(ssids, list) or not ssids or not all(isinstance(x, str) and SSID_OK.match(x) for x in ssids):
            return "Bad request"
        iface = self.view()["wifi"].get("iface") or "Wi-Fi"
        for i, ssid in enumerate(ssids, 1):
            self.run(["netsh", "wlan", "set", "profileorder", "name=" + ssid, "interface=" + iface, "priority=%d" % i])
        self.log.warning("network: %s put the Wi-Fi in this order: %s", who, ", ".join(ssids))
        return None
