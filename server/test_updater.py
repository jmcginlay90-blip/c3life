"""Tests for updates as the app sees them (the GitHub side is Update-C3Life.ps1, tested with a pretend GitHub on
GitHub's Windows computer). Run: python3 -m unittest -v test_updater.py"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import updater as U

NEW = "b" * 40


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ran = os.path.join(self.tmp, "ran.txt")
        self.u = U.Updater(self.tmp, {"update_task": [sys.executable, "-c", "open(%r, 'a').write('x')" % self.ran]}, s.log)
        self.st = {"key": True, "state": "idle", "latest": {"sha": NEW, "title": "Simple pages"}, "behind": True}
        self.monday, self.sunday = time.strptime("2026-10-26", "%Y-%m-%d"), time.strptime("2026-10-25", "%Y-%m-%d")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_when_it_can_and_cant(self):
        self.assertEqual(self.u.why_not(self.st, now=self.monday), "")
        self.assertIn("Sunday", self.u.why_not(self.st, now=self.sunday))
        self.assertIn("GitHub key", self.u.why_not(dict(self.st, key=False), now=self.monday))
        self.assertIn("already running", self.u.why_not(dict(self.st, state="installing"), now=self.monday))
        self.assertIn("newest version", self.u.why_not(dict(self.st, behind=False), now=self.monday))
        self.assertIn("Look for updates", self.u.why_not(dict(self.st, latest=None), now=self.monday))
        rec = type("R", (), {"rec": object(), "player": None})()
        self.assertIn("recording", self.u.why_not(self.st, rec, now=self.monday))
        rec.rec, rec.player = None, object()
        self.assertIn("Back to stage", self.u.why_not(self.st, rec, now=self.monday))
        self.assertIn("on the HP", U.Updater(self.tmp, {}, s.log).why_not(self.st, now=self.monday) if os.name != "nt" else "on the HP")


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ran = os.path.join(self.tmp, "ran.txt")
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", update_any_day=True,
                   update_task=[sys.executable, "-c", "open(%r, 'a').write('x')" % self.ran])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.up = os.path.join(self.tmp, "data", "update")
        os.makedirs(self.up, exist_ok=True)
        self.tok = {}
        for n in ("Admin", "FOH sound"):
            body = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.tok[n] = json.loads(r.read())["token"]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
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

    def status(self, **kw):
        d = {"key": True, "state": "idle", "checked": "2026-10-26 04:23", "ok": True,
             "latest": {"sha": NEW, "date": "2026-10-27T10:00:00Z", "title": "Simple pages"}, "behind": True,
             "new": [{"sha": NEW, "title": "Simple pages"}, {"sha": "d" * 40, "title": "Marker tools"}]}
        d.update(kw)
        with open(os.path.join(self.up, "status.json"), "w") as f:
            json.dump(d, f)

    def test_admins_only(self):
        self.assertEqual(self.call("/api/update", who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/update/apply", "POST", {"sha": NEW}, who="FOH sound")[0], 403)

    def test_shows_whats_new(self):
        self.status()
        with open(os.path.join(self.up, "installed.json"), "w") as f:
            json.dump({"sha": "a" * 40, "title": "Patch update", "when": "2026-10-20 10:00"}, f)
        code, j = self.call("/api/update")
        self.assertEqual(code, 200)
        self.assertEqual((j["installed"]["title"], j["latest"]["title"], [n["title"] for n in j["new"]], j["why_not"]), ("Patch update", "Simple pages", ["Simple pages", "Marker tools"], ""))

    def test_look_now_starts_the_task(self):
        code, j = self.call("/api/update/check", "POST", {})
        self.assertEqual(code, 200, j)
        self.assertTrue(os.path.exists(self.ran))
        with open(os.path.join(self.up, "request.json")) as f:
            self.assertEqual(json.load(f)["action"], "check")

    def test_update_asks_the_task_for_the_newest_only(self):
        self.status()
        self.assertEqual(self.call("/api/update/apply", "POST", {"sha": "e" * 40})[0], 409)  # not the newest
        code, j = self.call("/api/update/apply", "POST", {"sha": NEW})
        self.assertEqual(code, 200, j)
        with open(os.path.join(self.up, "request.json")) as f:
            r = json.load(f)
        self.assertEqual((r["action"], r["sha"], r["by"]), ("apply", NEW, "Admin"))
        self.assertTrue(self.call("/api/update")[1]["busy"])

    def test_refused_without_the_key_or_while_busy(self):
        self.status(key=False)
        code, j = self.call("/api/update/apply", "POST", {"sha": NEW})
        self.assertEqual(code, 409)
        self.assertIn("GitHub key", j["error"])
        self.status(state="installing")
        self.assertEqual(self.call("/api/update/check", "POST", {})[0], 409)
        self.assertFalse(os.path.exists(self.ran))
