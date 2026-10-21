"""Tests for remote support: Admins using the whole app from outside the building (over Tailscale).
Run: python3 -m unittest -v test_remote.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s


class Remote(unittest.TestCase):
    support = ["Admin"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        for n in ("player.html", "project/Listen.dc.html", "project/Main.dc.html", "project/User.dc.html"):
            with open(os.path.join(app, n), "w") as f:
                f.write("<x-dc>%s</x-dc>" % n)
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), remote_support_roles=self.support)
        cfg["_nets"], cfg["_remote"] = [], [s.ipaddress.ip_network("127.0.0.0/8")]  # this test is "at home"
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, token=None, cookie=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if cookie:
            req.add_header("Cookie", "c3s=" + cookie)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "1234"}
        code, raw, h = self.call("/api/login", "POST", body)
        return json.loads(raw), h


class SupportOn(Remote):
    def test_admin_gets_the_whole_app(self):
        j, h = self.login("Admin")
        self.assertTrue(j["remote_support"])
        self.assertIn("c3s=" + j["token"], h.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertEqual(self.call("/project/Main.dc.html", cookie=j["token"])[0], 200)  # a page: the cookie
        self.assertEqual(self.call("/api/access", token=j["token"])[0], 200)  # the app's calls: the token
        self.assertEqual(self.call("/api/nametags", token=j["token"])[0], 200)

    def test_without_signing_in_only_listen(self):
        self.assertEqual(self.call("/project/Main.dc.html")[0], 403)
        self.assertEqual(self.call("/project/Listen.dc.html")[0], 200)
        self.assertEqual(self.call("/project/User.dc.html")[0], 200)  # to sign in

    def test_other_roles_still_listen_only(self):
        j, h = self.login("FOH sound")
        self.assertFalse(j["remote_support"])
        self.assertNotIn("Set-Cookie", h)
        self.assertEqual(self.call("/project/Main.dc.html", cookie=j["token"])[0], 403)
        self.assertEqual(self.call("/api/access", token=j["token"])[0], 403)

    def test_a_made_up_cookie_does_nothing(self):
        self.assertEqual(self.call("/project/Main.dc.html", cookie="not-a-real-token")[0], 403)

    def test_sign_out_ends_it(self):
        j, _ = self.login("Admin")
        code, _, h = self.call("/api/logout", "POST", {}, token=j["token"])
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", h.get("Set-Cookie", ""))
        self.assertEqual(self.call("/project/Main.dc.html", cookie=j["token"])[0], 403)

    def test_home_page_goes_to_the_app_for_support(self):
        j, _ = self.login("Admin")
        # remote support gets the app's start page: Listen, which takes someone signed in on to their own first screen
        self.assertEqual(self.call("/", cookie=j["token"])[2]["Location"], "/player.html?p=Listen.dc.html&start=1")
        self.assertEqual(self.call("/")[2]["Location"], "/player.html?p=Listen.dc.html")  # everyone else from outside: Listen only

    def test_it_is_logged(self):
        j, _ = self.login("Admin")
        with self.assertLogs("c3life", "INFO") as lg:
            self.call("/project/Main.dc.html", cookie=j["token"])
        self.assertTrue(any("remote support: Admin opened /project/Main.dc.html" in x for x in lg.output))

    def test_health_says_it_is_on(self):
        self.assertTrue(json.loads(self.call("/api/health")[1])["remote_support"])


class SupportOff(Remote):
    support = []

    def test_admin_from_home_is_listen_only(self):
        j, h = self.login("Admin")
        self.assertFalse(j["remote_support"])
        self.assertNotIn("Set-Cookie", h)
        self.assertEqual(self.call("/project/Main.dc.html", cookie=j["token"])[0], 403)
        self.assertEqual(self.call("/api/access", token=j["token"])[0], 403)
        self.assertEqual(self.call("/project/User.dc.html")[0], 403)
        self.assertFalse(json.loads(self.call("/api/health")[1])["remote_support"])


if __name__ == "__main__":
    unittest.main()
