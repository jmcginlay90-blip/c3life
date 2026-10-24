"""Tests for Help's system check and the support report (syscheck.py, GET /api/check, POST /api/support).
Run: python3 -m unittest -v test_syscheck.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import syscheck as sc


class Health:
    def devices(self):
        return [{"id": "desk", "name": "The Wing", "ok": False, "why": "No answer at 192.168.10.20.", "left": False},
                {"id": "lightkey", "name": "Lightkey", "ok": False, "why": "", "left": True}]


class Engine:
    def view(self):
        return {"status": "running", "reason": "", "channels": 64}


class Check(unittest.TestCase):
    def test_what_it_says(self):
        r = {x["name"]: x for x in sc.check({"version": "9", "start": 0, "health": Health(), "engine": Engine(), "cfg": {}},
                                            now=lambda: 3700, online=lambda: False)}
        self.assertIn("running 1h 01m", r["C3 Life app on the C3 Life server"]["state"])
        self.assertEqual((r["Sound (Dante Virtual Soundcard)"]["ok"], r["The Wing"]["ok"], r["The Wing"]["fix"]), (True, False, "No answer at 192.168.10.20."))
        self.assertEqual((r["Lightkey"]["ok"], r["Lightkey"]["state"]), (None, "Left out on Devices"))
        self.assertIn("No internet", r["Internet"]["state"])
        self.assertIn("free", r["Storage on the C3 Life server"]["state"])

    def test_the_report_has_no_secrets(self):
        cfg = {"smtp_password": "hunter2", "monday_token": "abc", "admin_pin_hash": "x", "wing_ip": "192.168.10.20", "_nets": [1],
               "people": [{"name": "Jo", "pin": "1234"}]}
        t = sc.report_text("FOH", "Wing dropped out", [{"name": "The Wing", "ok": False, "state": "Not answering", "fix": "Check it"}],
                           [{"at": "10:00:00", "level": "warn", "text": "wing: no answer"}], cfg)
        for secret in ("hunter2", "abc", "1234"):
            self.assertNotIn(secret, t)
        self.assertIn("192.168.10.20", t)
        self.assertIn("!! The Wing: Not answering (Check it)", t)
        self.assertIn("wing: no answer", t)


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", desk="none",
                   smtp_password="hunter2")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, who, body=None):
        b = {"name": who, "password": "c3life-admin"} if who == "Admin" else {"name": who, "pin": "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(b).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            tok = json.loads(r.read())["token"]
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_check_and_support_report(self):
        code, j = self.call("/api/check", "FOH sound")
        self.assertEqual(code, 200)
        self.assertIn("C3 Life app on the C3 Life server", [r["name"] for r in j["results"]])
        self.assertEqual(self.call("/api/check", "Foyer team")[0], 403)
        code, j = self.call("/api/support", "FOH sound", {"note": "The Wing dropped out"})
        self.assertEqual(code, 200)
        rid = j["report"]["id"]
        with open(os.path.join(self.tmp, "data", "reports", rid, "report.json"), encoding="utf-8") as fh:
            r = json.load(fh)
        self.assertIn("The Wing dropped out", r["text"])
        self.assertNotIn("hunter2", r["text"])


if __name__ == "__main__":
    unittest.main()
