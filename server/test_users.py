"""Tests for Users & PINs on the C3 Life server (Store.save_all and /api/users). Run: python3 -m unittest -v test_users.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s


class Users(unittest.TestCase):
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
        self.admin = self.login("Admin", "c3life-admin")[1]["token"]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, tok=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, name, pin):
        return self.call("/api/login", "POST", {"name": name, "password": pin} if name == "Admin" else {"name": name, "pin": pin})

    def test_create_change_switch_off_and_delete(self):
        code, j = self.call("/api/users", tok=self.admin)
        self.assertEqual(code, 200)
        self.assertNotIn("pin", j["users"][0])  # never the hashes
        foh = self.login("FOH sound", "1234")[1]["token"]
        self.assertEqual(self.call("/api/users", tok=foh)[0], 403)
        people = [dict(old=u["name"], **{k: u[k] for k in ("name", "role", "active", "chat")}) for u in j["users"]]
        new = people + [{"old": "", "name": "Sam (bass)", "role": "Musician", "pin": "4321", "active": True, "chat": True}]
        self.assertEqual(self.call("/api/users", "PUT", {"password": "nope", "users": new}, tok=self.admin)[0], 401)  # the password again
        self.assertIn("4 to 6 digits", self.call("/api/users", "PUT", {"password": "c3life-admin", "users": people + [dict(new[-1], pin="12")]}, tok=self.admin)[1]["error"])
        code, j = self.call("/api/users", "PUT", {"password": "c3life-admin", "users": new}, tok=self.admin)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.login("Sam (bass)", "4321")[0], 200)
        # switch FOH sound off: they can't sign in; delete Sam
        people = [dict(old=u["name"], **{k: u[k] for k in ("name", "role", "active", "chat")}) for u in j["users"]]
        for p in people:
            if p["name"] == "FOH sound":
                p["active"] = False
        people = [p for p in people if p["name"] != "Sam (bass)"]
        code, j = self.call("/api/users", "PUT", {"password": "c3life-admin", "users": people, "removed": ["Sam (bass)"]}, tok=self.admin)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.login("FOH sound", "1234")[0], 401)
        self.assertEqual(self.login("Sam (bass)", "4321")[0], 401)
        # the last Admin stays
        for p in people:
            if p["role"] == "Admin":
                p["active"] = False
        self.assertIn("at least one Admin", self.call("/api/users", "PUT", {"password": "c3life-admin", "users": people}, tok=self.admin)[1]["error"])

    def test_simple_or_full_view_is_each_persons_own(self):
        code, j = self.login("FOH sound", "1234")
        self.assertEqual((code, j["view"]), (200, ""))  # not chosen yet: the app asks
        foh = j["token"]
        self.assertEqual(self.call("/api/me/view", "POST", {"view": "simple"})[0], 401)  # signed in only
        self.assertEqual(self.call("/api/me/view", "POST", {"view": "tiny"}, tok=foh)[0], 400)
        self.assertEqual(self.call("/api/me/view", "POST", {"view": "simple"}, tok=foh), (200, {"ok": True, "error": "", "view": "simple"}))
        self.assertEqual(self.login("FOH sound", "1234")[1]["view"], "simple")  # on every device they sign in on
        self.assertEqual(self.login("Keys (musician)", "1234")[1]["view"], "")  # nobody else's changes
        # Users & PINs saving everyone keeps it
        users = self.call("/api/users", tok=self.admin)[1]["users"]
        people = [dict(old=u["name"], **{k: u[k] for k in ("name", "role", "active", "chat")}) for u in users]
        self.assertEqual(self.call("/api/users", "PUT", {"password": "c3life-admin", "users": people}, tok=self.admin)[0], 200)
        self.assertEqual(self.login("FOH sound", "1234")[1]["view"], "simple")
        self.assertEqual(self.call("/api/me/view", "POST", {"view": "full"}, tok=foh)[0], 200)
        self.assertEqual(self.login("FOH sound", "1234")[1]["view"], "full")

    def test_monitors_lists_the_musicians_for_whos_on_which_mix(self):
        foh = self.login("FOH sound", "1234")[1]["token"]
        code, j = self.call("/api/monitors", tok=foh)
        self.assertEqual(code, 200)
        self.assertIn("Keys (musician)", j["musicians"])
        self.assertNotIn("FOH sound", j["musicians"])
        keys = self.login("Keys (musician)", "1234")[1]["token"]
        self.assertNotIn("musicians", self.call("/api/monitors", tok=keys)[1])  # a musician sees only their own mixes
        code, j = self.call("/api/monitors", "PUT", {"people": {"Keys (musician)": {"own": 1, "extra": [2]}}}, tok=foh)
        self.assertEqual(code, 200, j)
        self.assertEqual(j["people"]["Keys (musician)"], {"own": 1, "extra": [2]})
        self.assertIn("Keys (musician)", j["musicians"])
        self.assertEqual(self.call("/api/monitors", "PUT", {"people": {"Keys (musician)": {"own": 9}}}, tok=foh)[0], 400)  # not a monitor mix
        self.assertEqual(self.call("/api/monitors", "PUT", {"people": {}}, tok=keys)[0], 403)


if __name__ == "__main__":
    unittest.main()
