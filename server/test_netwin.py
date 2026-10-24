"""Tests for the server's own network on Windows (netwin.py): reading netsh and PowerShell, joining a Wi-Fi.
Run: python3 -m unittest -v test_netwin.py"""

import json
import logging
import os
import unittest

import netwin as nw

IFACES = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Dual Band Wireless-AC 8265
    State                  : connected
    SSID                   : Church Office
    BSSID                  : aa:bb:cc:dd:ee:ff
    Signal                 : 88%
"""
NETS = """
Interface name : Wi-Fi
There are 2 networks currently visible.

SSID 1 : Church Office
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : aa:bb:cc:dd:ee:ff
         Signal             : 61%
         Radio type         : 802.11ac
         Band               : 5 GHz
SSID 2 : Booth Control
    Authentication          : WPA3-Personal
    BSSID 1                 : aa:bb:cc:dd:ee:00
         Signal             : 90%
"""
PROFILES = """
User profiles
-------------
    All User Profile     : Church Office
    All User Profile     : Booth iPhone
"""


class Parse(unittest.TestCase):
    def test_netsh(self):
        self.assertEqual(nw.parse_interfaces(IFACES), {"iface": "Wi-Fi", "state": "connected", "ssid": "Church Office", "signal": 88})
        n = nw.parse_networks(NETS)
        self.assertEqual([(x["ssid"], x["auth"], x["signal"]) for x in n], [("Booth Control", "WPA3-Personal", 90), ("Church Office", "WPA2-Personal", 61)])
        self.assertEqual(nw.parse_profiles(PROFILES), ["Church Office", "Booth iPhone"])
        a = nw.parse_adapters(json.dumps([{"name": "Ethernet", "desc": "Intel(R) Ethernet I219-LM", "status": "Up", "speed": "1 Gbps", "media": "802.3", "ip": "192.168.10.10"},
                                          {"name": "Wi-Fi", "desc": "Intel Wireless-AC", "status": "Disconnected", "media": "Native 802.11"}]))
        self.assertEqual([(x["name"], x["up"], x["wifi"]) for x in a], [("Ethernet", True, False), ("Wi-Fi", False, True)])

    def test_profile_xml(self):
        x = nw.profile_xml("Booth & Co", "pass<word>", "WPA3-Personal")
        self.assertIn("<name>Booth &amp; Co</name>", x)
        self.assertIn("WPA3SAE", x)
        self.assertIn("pass&lt;word&gt;", x)
        self.assertIn("<authentication>open</authentication>", nw.profile_xml("Guest", "", "Open"))


class Join(unittest.TestCase):
    def test_join_deletes_the_password_file(self):
        ran, files = [], []

        def fake(args, timeout=15):
            ran.append(args)
            if args[:3] == ["netsh", "wlan", "add"]:
                path = args[4].split("=", 1)[1]
                files.append(path)
                with open(path, encoding="utf-8") as fh:
                    self.assertIn("hunter22", fh.read())
                return "Profile Church Office is added on interface Wi-Fi."
            if args[:3] == ["netsh", "wlan", "connect"]:
                return "Connection request was completed successfully."
            return IFACES if "interfaces" in args else (PROFILES if "profiles" in args else "")
        n = nw.Network(logging.getLogger("test"), run_fn=fake)
        self.assertIn("8 to 63", n.join("Church Office", "short", "WPA2-Personal", "Admin"))
        self.assertIsNone(n.join("Church Office", "hunter22", "WPA2-Personal", "Admin"))
        self.assertFalse(os.path.exists(files[0]))
        self.assertFalse(any("hunter22" in " ".join(a) for a in ran))  # never on a command line
        self.assertIn("Type the password", n.join("Nope", "", "WPA2-Personal", "Admin"))
        self.assertIsNone(n.order(["Church Office", "Booth iPhone"], "Admin"))
        self.assertIn("priority=2", ran[-1][-1])

    def test_only_on_windows_for_real(self):
        if os.name != "nt":
            self.assertIn("Only the Windows", nw.Network(logging.getLogger("test")).view()["error"])


if __name__ == "__main__":
    unittest.main()
