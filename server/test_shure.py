"""Tests for the wireless mics (shure.py, /api/mics): what a Shure receiver says, and that it only ever asks.
Run: python3 -m unittest -v test_shure.py"""

import logging
import shutil
import socket
import tempfile
import threading
import unittest

import shure as sh


class FakeReceiver:
    """Answers like a QLX-D: two channels, one switched off. Remembers everything it was sent."""
    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port, self.got = self.srv.getsockname()[1], []
        self.vals = {1: {"CHAN_NAME": "{Pastor HH                       }", "TX_TYPE": "QLXD2", "BATT_BARS": "004", "BATT_CHARGE": "022",
                         "BATT_RUN_TIME": "00050", "RX_RF_LVL": "-065"},
                     2: {"CHAN_NAME": "{BV 1}", "TX_TYPE": "UNKN", "BATT_BARS": "255", "BATT_CHARGE": "255", "BATT_RUN_TIME": "65535"}}
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            data = c.recv(65536).decode()
            self.got.append(data)
            out = ""
            for ch, k in sh.re.findall(r"< GET (\d+) ([A-Z_]+) >", data):
                v = self.vals.get(int(ch), {}).get(k)
                if v is not None:
                    out += "< REP %s %s %s >" % (ch, k, v)
            c.sendall(out.encode())
            c.close()

    def close(self):
        self.srv.close()


class Mics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rx = FakeReceiver()
        self.m = sh.Mics(self.tmp, {}, logging.getLogger("test"), connect=lambda ip: socket.create_connection(("127.0.0.1", self.rx.port), timeout=2))

    def tearDown(self):
        self.rx.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_battery_signal_names_and_only_gets(self):
        self.assertIsNone(self.m.set({"receivers": [{"ip": "192.168.10.60", "name": "QLX-D rack", "chans": [15, 2]}], "warn": 25}, "FOH"))
        self.m.poll()
        v = self.m.view({"15": "Pastor HH", "2": "BV 1"})
        a, b = v["mics"]
        self.assertEqual((a["wing_name"], a["pct"], a["mins"], a["rf"], a["rechargeable"], a["off"]), ("Pastor HH", 22, 50, 4, True, False))
        self.assertEqual((b["off"], b["pct"]), (True, None))
        self.assertEqual(v["low"], [{"name": "Pastor HH", "pct": 22, "mins": 50}])
        self.assertTrue(self.rx.got and all("SET" not in g for g in self.rx.got))  # watch only

    def test_bad_settings_and_a_receiver_that_doesnt_answer(self):
        self.assertIn("address", self.m.set({"receivers": [{"ip": "nope", "chans": [1]}]}, "FOH"))
        self.assertEqual(self.m.set({"warn": 7}, "FOH"), "Bad request")
        self.rx.close()
        self.m.connect = lambda ip: socket.create_connection(("127.0.0.1", 1), timeout=1)
        self.assertIsNone(self.m.set({"receivers": [{"ip": "192.168.10.61", "chans": [1]}]}, "FOH"))
        self.m.poll()
        self.assertIn("No answer at 192.168.10.61", self.m.view()["problems"][0]["error"])

    def test_parse(self):
        self.assertEqual(sh.parse("< REP 1 CHAN_NAME {A B } >< REP MODEL {QLXD4} >"), {1: {"CHAN_NAME": "A B"}, 0: {"MODEL": "QLXD4"}})


if __name__ == "__main__":
    unittest.main()
