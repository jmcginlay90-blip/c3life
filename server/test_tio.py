"""Tests for the Tio1608-D link, against a fake Tio speaking Yamaha's text protocol.
Run: python3 -m unittest -v test_tio.py"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import tio as tm


class FakeTio:
    """Answers get/set like a Tio1608-D and sends NOTIFY when 'R Remote' changes something."""

    def __init__(self):
        self.vals = {(p, n): v for n in range(16) for p, v in (("HAGain", 20), ("48VOn", 0), ("HPFOn", 0), ("HPFFreq", 800))}
        self.sets, self.clients = [], []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(0.2)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            self.clients.append(c)
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        c.settimeout(0.3)
        buf = b""
        while not self.stop.is_set():
            try:
                d = c.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                t = line.decode().split()
                if len(t) >= 4 and t[0] in ("get", "set") and t[1].startswith("IO:Current/InCh/"):
                    p, n = t[1].rsplit("/", 1)[1], int(t[2])
                    if (p, n) not in self.vals:
                        c.sendall(b"ERROR %s UnknownAddress\n" % t[0].encode())
                        continue
                    if t[0] == "set":
                        self.vals[(p, n)] = int(t[4])
                        self.sets.append((p, n, int(t[4])))
                    c.sendall(("OK %s %s %d 0 %d\n" % (t[0], t[1], n, self.vals[(p, n)])).encode())

    def r_remote(self, p, n, v):
        """Someone changes it in R Remote: every connected app is told."""
        self.vals[(p, n)] = v
        for c in self.clients:
            try:
                c.sendall(("NOTIFY set IO:Current/InCh/%s %d 0 %d\n" % (p, n, v)).encode())
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.srv.close()
        for c in self.clients:
            c.close()


def wait(cond, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class _Log:
    def info(self, *a):
        pass

    warning = error = info


class Link(unittest.TestCase):
    def setUp(self):
        self.f = FakeTio()
        self.tmp = tempfile.mkdtemp()
        self.t = tm.Tio(self.tmp, {"tio_ip": "127.0.0.1", "tio_port": self.f.port}, _Log())
        self.t.start()

    def tearDown(self):
        self.t.close()
        self.f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_every_input(self):
        self.assertTrue(wait(lambda: self.t.status()["status"] == "connected" and self.t.inputs[16].get("hpff") == 80.0))
        self.assertEqual(self.t.inputs[1], {"gain": 20, "vph": 0, "hpf": 0, "hpff": 80.0})
        self.assertEqual(self.t.settings["mode"], "watch")

    def test_watch_only_sends_nothing(self):
        wait(lambda: self.t.status()["status"] == "connected")
        self.assertIn("watch the Tio only", self.t.set(1, "gain", 30))
        time.sleep(0.2)
        self.assertEqual(self.f.sets, [])

    def test_control(self):
        self.t.configure(mode="control")
        self.assertTrue(wait(lambda: self.t.status()["status"] == "connected"))
        self.assertIsNone(self.t.set(3, "gain", 34.4))
        self.assertIsNone(self.t.set(3, "vph", True))
        self.assertIsNone(self.t.set(3, "hpf", True))
        self.assertIsNone(self.t.set(3, "hpff", 120))
        self.assertTrue(wait(lambda: self.t.inputs[3] == {"gain": 34, "vph": 1, "hpf": 1, "hpff": 120.0}))
        self.assertIn(("HAGain", 2, 34), self.f.sets)  # input 3 is 2 on the wire
        self.assertIn(("HPFFreq", 2, 1200), self.f.sets)  # tenths of a hertz
        self.assertIn("−6 to +66", self.t.set(3, "gain", 70))
        self.assertIn("20 to 600 Hz", self.t.set(3, "hpff", 1000))
        self.assertIn("inputs 1 to 16", self.t.set(17, "gain", 10))

    def test_r_remote_changes_show_at_once(self):
        self.assertTrue(wait(lambda: self.t.inputs[5].get("gain") == 20))
        self.f.r_remote("HAGain", 4, 41)
        self.assertTrue(wait(lambda: self.t.inputs[5]["gain"] == 41, 1))

    def test_no_answer_and_settings_kept(self):
        self.t.configure(ip="10.9.9.9", mode="control", card_from=17)
        self.assertEqual(self.t.status()["status"], "no answer")
        self.assertIn("isn't answering", self.t.set(1, "vph", True))
        t2 = tm.Tio(self.tmp, {}, _Log())
        self.assertEqual(t2.settings, {"ip": "10.9.9.9", "mode": "control", "card_from": 17})
        self.assertIn("1 to 49", self.t.configure(card_from=60))


class Api(unittest.TestCase):
    def setUp(self):
        self.f = FakeTio()
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", tio_ip="127.0.0.1", tio_port=self.f.port)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound", "ProPresenter"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.tio.close()
        self.httpd.wing.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="Admin"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_can_see_and_change(self):
        self.assertTrue(wait(lambda: self.call("/api/tio")[1].get("status") == "connected"))
        self.assertEqual(self.call("/api/tio", who="ProPresenter")[0], 403)
        self.assertEqual(self.call("/api/tio/settings", "PUT", {"mode": "control"}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/tio/set", "POST", {"n": 1, "field": "vph", "value": True}, who="FOH sound")[0], 409)
        self.assertEqual(self.call("/api/tio/settings", "PUT", {"mode": "control", "card_from": 1})[0], 200)
        self.assertEqual(self.call("/api/tio/set", "POST", {"n": 1, "field": "vph", "value": True}, who="FOH sound")[0], 200)
        self.assertTrue(wait(lambda: self.call("/api/tio")[1]["inputs"]["1"].get("vph") == 1))
        self.assertEqual(self.call("/api/tio/settings", "PUT", {"ip": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
