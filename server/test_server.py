"""Tests for the C3 Life web server. Run: python3 -m unittest -v test_server.py"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "project"))
        with open(os.path.join(self.app, "player.html"), "w") as f:
            f.write("<!doctype html><title>C3 Life</title>")
        with open(os.path.join(self.app, "project", "Main.dc.html"), "w") as f:
            f.write("<x-dc>Home</x-dc>")
        with open(os.path.join(self.tmp, "secret.txt"), "w") as f:
            f.write("not for phones")
        self.start(["127.0.0.0/8"])

    def start(self, nets):
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=self.app, data_dir=os.path.join(self.tmp, "data"), allowed_networks=nets)
        cfg["_nets"] = [s.ipaddress.ip_network(n) for n in nets]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path, method="GET", body=None, token=None):
        opener = urllib.request.build_opener(NoRedirect)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_root_goes_to_the_app(self):
        code, _, h = self.get("/")
        self.assertEqual(code, 302)
        self.assertEqual(h["Location"], "/player.html?p=Listen.dc.html&start=1")  # Listen first, with Sign in at the top

    def test_serves_app_files(self):
        code, body, h = self.get("/player.html")
        self.assertEqual(code, 200)
        self.assertIn(b"C3 Life", body)
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.get("/project/Main.dc.html")[0], 200)

    def test_health(self):
        code, body, _ = self.get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_no_folder_listing(self):
        self.assertEqual(self.get("/project/")[0], 404)

    def test_nothing_outside_the_app_folder(self):
        for p in ["/../secret.txt", "/..%2fsecret.txt", "/project/../../secret.txt", "/%2e%2e/secret.txt"]:
            code, body, _ = self.get(p)
            self.assertNotIn(b"not for phones", body, p)

    def test_no_writes(self):
        for m in ["POST", "PUT", "DELETE"]:
            self.assertEqual(self.get("/player.html", m)[0], 405, m)

    # --- sync -------------------------------------------------------------------------------------------
    def login(self, name="Keys (musician)", pin="1234"):
        code, body, _ = self.get("/api/login", "POST", {"name": name, "pin": pin})
        return code, json.loads(body)

    def test_health_says_sync(self):
        self.assertTrue(json.loads(self.get("/api/health")[1])["sync"])

    def test_sign_in_with_pin(self):
        code, b = self.login()
        self.assertEqual((code, b["name"], b["role"]), (200, "Keys (musician)", "Musician"))
        self.assertEqual(self.login(pin="9999")[0], 401)
        self.assertEqual(self.login(name="Nobody")[0], 401)
        self.assertEqual(self.login(name="Viewer", pin="")[0], 200)

    def test_admin_signs_in_with_a_password(self):
        code, b, _ = self.get("/api/login", "POST", {"name": "Admin", "password": "c3life-admin"})
        self.assertEqual((code, json.loads(b)["role"]), (200, "Admin"))
        self.assertEqual(self.login(name="Admin", pin="1234")[0], 401)  # the old PIN no longer works
        self.assertTrue(s.valid_password("church-2026"))
        for bad in ("short1", "longbutnonumber", "12345678", ""):
            self.assertFalse(s.valid_password(bad), bad)

    def test_pins_are_not_stored_plain(self):
        self.login()
        raw = open(os.path.join(self.tmp, "data", "users.json")).read()
        self.assertNotIn('"1234"', raw)
        self.assertIn("pbkdf2$", raw)

    def test_lockout_after_5_wrong_pins(self):
        for _ in range(5):
            self.login(pin="0000")
        self.assertEqual(self.login()[0], 429)  # even the right PIN waits
        self.assertEqual(self.login(name="Admin", pin="c3life-admin")[0], 200)  # other people are not locked

    def test_mixes_need_sign_in(self):
        self.assertEqual(self.get("/api/mixes")[0], 401)
        self.assertEqual(self.get("/api/mixes", "PUT", {"saves": []}, token="made-up")[0], 401)

    def test_two_devices_stay_in_sync(self):
        a = self.login()[1]["token"]   # phone
        b = self.login()[1]["token"]   # iPad, same person
        saves = [{"id": "m1", "name": "Sunday AM", "at": "now", "mix": {"ch": {"c1": {"lv": 50}}}}]
        code, body, _ = self.get("/api/mixes", "PUT", {"saves": saves, "mymix": {"ch": {"c1": {"lv": 60}}}}, token=a)
        v = json.loads(body)["version"]
        self.assertEqual(code, 200)
        got = json.loads(self.get("/api/mixes", token=b)[1])
        self.assertEqual((got["saves"][0]["name"], got["mymix"]["ch"]["c1"]["lv"], got["version"]), ("Sunday AM", 60, v))
        self.assertFalse(json.loads(self.get("/api/mixes?since=%d" % v, token=b)[1])["changed"])
        self.get("/api/mixes", "PUT", {"mymix": {"ch": {"c1": {"lv": 70}}}}, token=b)
        got = json.loads(self.get("/api/mixes?since=%d" % v, token=a)[1])
        self.assertTrue(got["changed"])
        self.assertEqual((got["mymix"]["ch"]["c1"]["lv"], got["saves"][0]["name"]), (70, "Sunday AM"))  # saves kept

    def test_people_only_see_their_own_mixes(self):
        k = self.login()[1]["token"]
        f = self.login(name="FOH sound")[1]["token"]
        self.get("/api/mixes", "PUT", {"saves": [{"id": "m1", "name": "Mine", "mix": {}}]}, token=k)
        self.assertIsNone(json.loads(self.get("/api/mixes", token=f)[1])["saves"])

    def test_bad_mix_data_refused(self):
        t = self.login()[1]["token"]
        for bad in [{"saves": "x"}, {"saves": [{"id": "1", "name": "n" * 41, "mix": {}}]}, {"saves": [{"id": "1", "name": "n"}]}, {"mymix": [1]},
                    {"saves": [{"id": str(i), "name": "n", "mix": {}} for i in range(51)]}]:
            self.assertEqual(self.get("/api/mixes", "PUT", bad, token=t)[0], 400, bad)

    def test_sign_out(self):
        t = self.login()[1]["token"]
        self.assertEqual(self.get("/api/logout", "POST", {}, token=t)[0], 200)
        self.assertEqual(self.get("/api/mixes", token=t)[0], 401)

    def test_mixes_survive_a_restart(self):
        t = self.login()[1]["token"]
        self.get("/api/mixes", "PUT", {"saves": [{"id": "m1", "name": "Keep me", "mix": {}}]}, token=t)
        self.httpd.shutdown(); self.httpd.server_close()
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=self.app, data_dir=os.path.join(self.tmp, "data"))
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg); self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.assertEqual(self.get("/api/mixes", token=t)[0], 401)  # sessions end with a restart
        t = self.login()[1]["token"]
        self.assertEqual(json.loads(self.get("/api/mixes", token=t)[1])["saves"][0]["name"], "Keep me")

    def test_data_folder_not_served(self):
        self.login()
        self.assertEqual(self.get("/data/users.json")[0], 404)
        self.assertEqual(self.get("/../data/users.json")[0], 404)

    def test_other_networks_refused(self):
        self.tearDown()
        self.setUp_only_booth()
        self.assertEqual(self.get("/player.html")[0], 403)

    def setUp_only_booth(self):
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(self.app)
        self.start(["192.168.10.0/24"])


if __name__ == "__main__":
    unittest.main()


class ConfigFile(unittest.TestCase):
    def test_lists_written_by_powershell_still_load(self):
        # Install-C3Life.ps1 once wrote an empty list as null and a list of one as a plain string
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8-sig") as f:  # PowerShell's UTF-8 has a BOM
                json.dump({"remote_networks": None, "allowed_networks": "192.168.10.0/24", "remote_support_roles": None}, f)
            cfg = s.load_config(p)
            self.assertEqual((cfg["remote_networks"], cfg["_remote"], cfg["remote_support_roles"]), ([], [], []))
            self.assertEqual([str(n) for n in cfg["_nets"]][0], "192.168.10.0/24")  # then the network this PC is on now
        finally:
            shutil.rmtree(d, ignore_errors=True)
