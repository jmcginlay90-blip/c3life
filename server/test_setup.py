"""Tests for Saved setup (setup.py, /api/setup...): the app's name, backups on the server and a drive, download, restore.
Run: python3 -m unittest -v test_setup.py"""

import io
import json
import logging
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile

import c3life_server as s
import setup as su


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "reports", "r1"))
        os.makedirs(os.path.join(self.tmp, "USB"))
        with open(os.path.join(self.data, "macros.json"), "w") as fh:
            fh.write('{"macros": ["first"]}')
        with open(os.path.join(self.data, "reports", "r1", "photo.jpg"), "wb") as fh:
            fh.write(b"x" * 10)
        self.drives = [{"id": "u1", "label": "USB", "root": os.path.join(self.tmp, "USB")}]
        self.su = su.Setup(self.data, {}, logging.getLogger("test"), drives=lambda: self.drives, version="9")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_name_is_kept(self):
        self.assertEqual(self.su.name(), "C3 Life")
        self.assertIn("1 to 30", self.su.set_name("  ", "Admin"))
        self.assertIsNone(self.su.set_name("C3 Life Church", "Admin"))
        self.assertEqual(su.Setup(self.data, {}, logging.getLogger("test")).name(), "C3 Life Church")

    def test_backup_to_the_drive_and_restore(self):
        err, where = self.su.backup("Admin", to_drive=True)
        self.assertEqual((err, where), (None, "on the C3 Life server and on USB"))
        b = self.su.list()
        self.assertEqual(sorted(x["where"] for x in b), ["C3 Life server", "USB"])
        with zipfile.ZipFile(self.su.file(b[0]["id"])) as z:
            names = z.namelist()
        self.assertIn("data/macros.json", names)
        self.assertFalse(any("reports" in n for n in names))  # photos aren't in a backup
        with open(os.path.join(self.data, "macros.json"), "w") as fh:
            fh.write('{"macros": ["changed"]}')
        usb = next(x for x in b if x["where"] == "USB")
        self.assertIsNone(self.su.restore(usb["id"], "Admin"))
        with open(os.path.join(self.data, "macros.json")) as fh:
            self.assertIn("first", fh.read())
        self.assertTrue(any("before-restore" in x["name"] for x in self.su.list()))  # what was there was kept first

    def test_nothing_else_can_be_read_or_restored(self):
        for bad in ("server/../macros.json", "server/x.txt", "drive:nope/a.c3backup", "nothing"):
            self.assertIsNone(self.su.file(bad))
        os.makedirs(self.su.dir, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"c3life": True}))
            z.writestr("data/../../evil.txt", "no")
        with open(os.path.join(self.su.dir, "bad.c3backup"), "wb") as fh:
            fh.write(buf.getvalue())
        self.assertIn("won't restore", self.su.restore("server/bad.c3backup", "Admin"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        self.assertIn("isn't there", self.su.restore("server/none.c3backup", "Admin"))


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="", desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("Admin", "FOH sound"):
            b = {"name": n, "password": "c3life-admin"} if n == "Admin" else {"name": n, "pin": "1234"}
            self.tok[n] = self.call("/api/login", None, b)[1]["token"]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, who, body=None, raw=False):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, (r.read() if raw else json.loads(r.read()))
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_admin_only_and_the_password_to_restore(self):
        self.assertEqual(self.call("/api/setup/name", "FOH sound", {"name": "X"})[0], 403)
        self.assertEqual(self.call("/api/setup/name", "Admin", {"name": "C3 Life Church"})[1]["name"], "C3 Life Church")
        self.assertEqual(self.call("/api/setup", "FOH sound")[1]["name"], "C3 Life Church")
        self.assertEqual(self.call("/api/setup", "FOH sound")[1]["backups"], [])
        code, j = self.call("/api/setup/backup", "Admin", {})
        self.assertEqual((code, j["where"]), (200, "on the C3 Life server"))
        bid = j["backups"][0]["id"]
        self.assertEqual(self.call("/api/setup/download?t=%s" % self.tok["FOH sound"], None, raw=True)[0], 403)
        code, data = self.call("/api/setup/download?t=%s&id=%s" % (self.tok["Admin"], bid), None, raw=True)
        self.assertEqual(code, 200)
        self.assertIn("manifest.json", zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertEqual(self.call("/api/setup/restore", "Admin", {"id": bid, "password": "wrong"})[0], 401)
        code, j = self.call("/api/setup/restore", "Admin", {"id": bid, "password": "c3life-admin"})
        self.assertEqual(code, 200)
        if os.name != "nt":
            self.assertIn("Only the Windows", j["note"])  # here it can't restart itself


if __name__ == "__main__":
    unittest.main()
