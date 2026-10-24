"""Tests for server power (power.py): what's asked of Windows and the smart plug, with both made up.
Run: python3 -m unittest -v test_power.py"""

import logging
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import power as pw


class Plug(BaseHTTPRequestHandler):
    gen1_only = False
    calls = []

    def do_GET(self):
        Plug.calls.append(self.path)
        ok = self.path.startswith("/relay/0") or (not Plug.gen1_only and self.path.startswith("/rpc/"))
        self.send_response(200 if ok else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class Power(unittest.TestCase):
    def setUp(self):
        self.ran, self.exited, self.saved = [], [], []
        self.p = pw.Power({"smart_plug": ""}, logging.getLogger("test"), before=lambda: self.saved.append(1),
                          run=self.ran.append, exit_fn=lambda: self.exited.append(1))

    def test_restart_shut_down_and_stop_it(self):
        self.assertIsNone(self.p.do("restart", "FOH"))
        self.assertEqual(self.ran[-1][:4], ["shutdown", "/r", "/t", "15"])
        self.assertEqual((self.saved, self.p.pending), ([1], "restart"))  # a recording is stopped and saved first
        self.assertIsNone(self.p.do("abort", "FOH"))
        self.assertEqual(self.ran[-1], ["shutdown", "/a"])
        self.assertIn("Nothing is waiting", self.p.do("abort", "FOH"))
        self.assertIsNone(self.p.do("off", "Admin"))
        self.assertEqual(self.ran[-1][:2], ["shutdown", "/s"])
        self.assertIn("Bad request", self.p.do("format", "Admin"))

    def test_restart_c3_life(self):
        self.assertIsNone(self.p.do("app", "FOH"))
        self.assertIn('schtasks /run /tn "C3 Life"', self.ran[-1][-1])
        time.sleep(1.7)
        self.assertEqual(self.exited, [1])

    def test_smart_plug(self):
        self.assertIn("No smart plug", self.p.do("cycle", "Admin"))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Plug)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.p.cfg["smart_plug"] = "127.0.0.1:%d" % srv.server_address[1]
        Plug.calls = []
        self.assertIsNone(self.p.do("cycle", "Admin"))
        self.assertEqual(Plug.calls, ["/rpc/Switch.Set?id=0&on=false&toggle_after=10"])  # a Gen2 Shelly
        Plug.gen1_only, Plug.calls = True, []
        self.assertIsNone(self.p.do("cycle", "Admin"))
        self.assertEqual(Plug.calls[-1], "/relay/0?turn=off&timer=10")  # an older one
        srv.shutdown(); srv.server_close()
        self.assertIn("didn't answer", self.p.do("cycle", "Admin"))

    def test_only_on_windows_for_real(self):
        real = pw.Power({}, logging.getLogger("test"))
        if real.run is pw.Power._run and __import__("os").name != "nt":
            self.assertIn("Only the Windows", real.do("restart", "FOH"))


if __name__ == "__main__":
    unittest.main()
