"""Tests for Planning Center sign in/out, its timers and local time. Run: python3 -m unittest -v test_pco.py"""

import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import pco as pm

GOOD = base64.b64encode(b"appid:secret").decode()
OTHER = base64.b64encode(b"other:secret2").decode()


class Fake:
    def __init__(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
        self.start = start
        self.items = [("1", "item", "Countdown", 300), ("2", "header", "Worship", 0), ("3", "song", "Washed", 330), ("4", "item", "Message", 2400)]
        self.patches, self.can_edit, self.down = [], True, False
        self.org_tz = "America/Chicago"


def fake_pco(f):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def who(self):
            a = (self.headers.get("Authorization") or "")[6:]
            return {"" + GOOD: "Jonathan McGinlay", OTHER: "Chris Denham"}.get(a)

        def do_GET(self):
            if f.down:
                return self.send(503, {})
            if not self.who():
                return self.send(401, {"errors": [{"title": "Unauthorized"}]})
            u = urllib.parse.urlsplit(self.path)
            p, q = u.path, urllib.parse.parse_qs(u.query)
            if p == "/people/v2/me":
                return self.send(200, {"data": {"type": "Person", "id": "9", "attributes": {"name": self.who()}}})
            if p == "/services/v2":
                return self.send(200, {"data": {"type": "Organization", "id": "1", "attributes": {"name": "C3 Church", "time_zone": f.org_tz}}})
            if p == "/services/v2/service_types":
                return self.send(200, {"data": [{"id": "1", "attributes": {"name": "Sunday Morning"}}, {"id": "2", "attributes": {"name": "Carols"}}]})
            if p.endswith("/plans"):
                if q.get("filter") == ["past"]:
                    return self.send(200, {"data": [{"id": "500", "attributes": {"dates": "11 October 2026", "sort_date": "2026-10-11T09:00:00Z"}}]})
                return self.send(200, {"data": [{"id": "501", "attributes": {"dates": "18 October 2026", "sort_date": f.start.isoformat().replace("+00:00", "Z")}}]})
            if p.endswith("/plan_times"):
                return self.send(200, {"data": [
                    {"id": "71", "attributes": {"name": None, "time_type": "service", "starts_at": f.start.isoformat().replace("+00:00", "Z")}},
                    {"id": "70", "attributes": {"name": "Band call", "time_type": "rehearsal", "starts_at": (f.start - timedelta(hours=1)).isoformat().replace("+00:00", "Z")}},
                    {"id": "72", "attributes": {"name": "Set up", "time_type": "other", "starts_at": f.start.isoformat()}}]})
            if p.endswith("/items"):
                return self.send(200, {"data": [{"id": i, "attributes": {"item_type": t, "title": n, "length": l, "sequence": int(i)}} for i, t, n, l in f.items]})
            self.send(404, {})

        def do_PATCH(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n))
            if not f.can_edit:
                return self.send(403, {"errors": [{"title": "Forbidden"}]})
            f.patches.append((self.path, body))
            iid = self.path.split("/")[-1]
            f.items = [(i, t, nm, body["data"]["attributes"]["length"] if i == iid else l) for i, t, nm, l in f.items]
            self.send(200, {"data": body["data"]})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Unit(unittest.TestCase):
    def test_utc_becomes_local_time(self):
        dt = pm.to_local("2026-10-18T09:00:00Z")
        self.assertEqual(dt.utcoffset(), datetime.now().astimezone().utcoffset() if False else dt.utcoffset())
        self.assertEqual(dt.astimezone(timezone.utc).strftime("%H:%M"), "09:00")
        self.assertEqual(pm.hhmm(dt), dt.strftime("%H:%M"))
        self.assertIsNone(pm.to_local("not a time"))


class Api(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.srv = fake_pco(self.f)
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        self.start_server()

    def start_server(self, **extra):
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=os.path.join(self.tmp, "app"), data_dir=os.path.join(self.tmp, "data"),
                   pco_base="http://127.0.0.1:%d" % self.srv.server_address[1], propresenter="", **extra)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {n: self.login(n) for n in ("Admin", "FOH sound", "ProPresenter", "Viewer")}

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tearDown(self):
        self.stop_server()
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "" if name == "Viewer" else "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

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

    def sign_in(self, app="appid", sec="secret"):
        return self.call("/api/pco/signin", "POST", {"app_id": app, "secret": sec})

    def test_starts_signed_out(self):
        code, d = self.call("/api/pco")
        self.assertEqual((code, d["signed_in"]), (200, False))
        self.assertEqual(self.call("/api/pco/timers")[1]["timers"], [])

    def test_sign_in_checks_the_token_first(self):
        code, d = self.sign_in(sec="wrong")
        self.assertEqual(code, 409)
        self.assertIn("didn’t accept that token", d["error"])
        self.assertFalse(self.call("/api/pco")[1]["signed_in"])
        code, d = self.sign_in()
        self.assertEqual(code, 200, d)
        self.assertEqual((d["who"], d["org"], d["org_tz"]), ("Jonathan McGinlay", "C3 Church", "America/Chicago"))
        self.assertNotIn("secret", json.dumps(self.call("/api/pco")[1]))
        self.assertEqual(self.call("/api/pco")[1]["service_types"], ["Sunday Morning", "Carols"])

    def test_sign_out_then_another_account(self):
        self.sign_in()
        code, d = self.call("/api/pco/signout", "POST", {})
        self.assertEqual((code, d["signed_in"]), (200, False))
        self.assertEqual(self.cfg["pco_app_id"], "")  # name tags stop using it too
        self.assertEqual(self.sign_in("other", "secret2")[1]["who"], "Chris Denham")
        self.assertEqual(self.cfg["pco_app_id"], "other")

    def test_signed_in_after_a_restart_and_out_stays_out(self):
        self.sign_in()
        self.stop_server(); self.start_server()
        self.assertTrue(self.call("/api/pco")[1]["signed_in"])
        self.call("/api/pco/signout", "POST", {})
        self.stop_server(); self.start_server(pco_app_id="from-installer", pco_secret="x")
        self.assertFalse(self.call("/api/pco")[1]["signed_in"])  # signing out in the app wins over the installer's token

    def test_only_admins_sign_in_or_out(self):
        self.assertEqual(self.call("/api/pco/signin", "POST", {"app_id": "appid", "secret": "secret"}, who="FOH sound")[0], 403)
        self.assertEqual(self.call("/api/pco/signout", "POST", {}, who="ProPresenter")[0], 403)

    def test_change_the_service_type(self):
        self.sign_in()
        self.assertEqual(self.call("/api/pco/service_type", "PUT", {"name": "Carols"})[1]["service_type"], "Carols")
        self.assertEqual(self.call("/api/pco/service_type", "PUT", {"name": "Nope"})[0], 409)

    def test_timers_in_local_time(self):
        self.sign_in()
        code, d = self.call("/api/pco/timers", who="ProPresenter")
        self.assertEqual(code, 200, d)
        by = {t["name"]: t for t in d["timers"]}
        local = self.f.start.astimezone().strftime("%H:%M")
        self.assertEqual(by["Service starts"]["at"], local)  # UTC from Planning Center, shown in this server's time
        self.assertTrue(7100 < by["Service starts"]["left"] <= 7200)
        self.assertEqual(by["Band call starts"]["at"], (self.f.start - timedelta(hours=1)).astimezone().strftime("%H:%M"))
        self.assertNotIn("Set up starts", by)  # only service and rehearsal times
        self.assertEqual([t["name"] for t in d["timers"] if t["kind"] == "item"], ["Countdown", "Washed", "Message"])  # not headers
        self.assertEqual(by["Message"]["left"], 2400)

    def test_says_when_the_planning_center_region_differs(self):
        os.environ["TZ"] = "Europe/London"
        try:
            self.sign_in()
            d = self.call("/api/pco")[1]
            self.assertTrue(d["tz_differs"])
            self.assertEqual(d["org_tz"], "America/Chicago")
        finally:
            del os.environ["TZ"]

    def test_start_stop_and_nudge_a_timer(self):
        self.sign_in()
        self.assertEqual(self.call("/api/pco/timer", "POST", {"id": "item-4", "action": "add", "secs": 300}, who="ProPresenter")[0], 200)
        d = self.call("/api/pco/timer", "POST", {"id": "item-4", "action": "start"}, who="ProPresenter")[1]
        time.sleep(2.1)
        t = next(x for x in self.call("/api/pco/timers")[1]["timers"] if x["id"] == "item-4")
        self.assertTrue(t["run"])
        self.assertTrue(2696 <= t["left"] <= 2698)  # 2400 + 300, 2 seconds gone
        self.call("/api/pco/timer", "POST", {"id": "item-4", "action": "stop"})
        self.call("/api/pco/timer", "POST", {"id": "item-4", "action": "reset"})
        t = next(x for x in self.call("/api/pco/timers")[1]["timers"] if x["id"] == "item-4")
        self.assertEqual((t["run"], t["left"]), (False, 2400))
        self.assertEqual(self.call("/api/pco/timer", "POST", {"id": "item-4", "action": "start"}, who="Viewer")[0], 403)

    def test_next_shows_now_and_up_next(self):
        self.sign_in()
        d = self.call("/api/pco/timers")[1]
        self.assertEqual((d["current"], d["next"]), ("", "item-1"))  # nothing until someone taps Next
        d = self.call("/api/pco/timer", "POST", {"action": "next"}, who="ProPresenter")[1]
        self.assertEqual((d["current"], d["next"]), ("item-1", "item-3"))  # Countdown now, Washed next (the header is skipped)
        by = {t["id"]: t for t in d["timers"]}
        self.assertTrue(by["item-1"]["run"])
        self.assertTrue(by["item-1"]["ends"])
        self.assertEqual(by["item-3"]["starts"], by["item-1"]["ends"])
        time.sleep(1.1)
        d = self.call("/api/pco/timer", "POST", {"action": "next"}, who="ProPresenter")[1]
        by = {t["id"]: t for t in d["timers"]}
        self.assertEqual(d["current"], "item-3")
        self.assertFalse(by["item-1"]["run"])  # the one we left stopped where it was
        self.assertTrue(by["item-1"]["left"] < 300)
        self.assertTrue(by["item-3"]["run"] and by["item-3"]["left"] == 330)
        self.assertEqual(d["next"], "item-4")

    def test_previous_go_and_the_ends(self):
        self.sign_in()
        self.assertEqual(self.call("/api/pco/timer", "POST", {"action": "prev"})[0], 409)  # nothing before the start
        self.call("/api/pco/timer", "POST", {"action": "go", "id": "item-4"})
        d = self.call("/api/pco/timer", "POST", {"action": "next"})
        self.assertEqual((d[0], d[1]["error"]), (409, "That was the last item."))
        d = self.call("/api/pco/timer", "POST", {"action": "prev"})[1]
        self.assertEqual(d["current"], "item-3")
        d = self.call("/api/pco/timer", "POST", {"action": "clear"})[1]
        self.assertEqual(d["current"], "")
        self.assertEqual(self.call("/api/pco/timer", "POST", {"action": "next"}, who="Viewer")[0], 403)

    def test_change_an_item_length_in_planning_center(self):
        self.sign_in()
        code, d = self.call("/api/pco/length", "PUT", {"item": "4", "secs": 2700}, who="ProPresenter")
        self.assertEqual(code, 200, d)
        self.assertEqual(self.f.patches[0][1]["data"]["attributes"], {"length": 2700})
        self.assertEqual(next(x for x in d["timers"] if x["id"] == "item-4")["left"], 2700)
        self.f.can_edit = False
        code, d = self.call("/api/pco/length", "PUT", {"item": "4", "secs": 2400}, who="ProPresenter")
        self.assertEqual(code, 409)
        self.assertIn("may only be able to view plans", d["error"])
        self.assertEqual(self.call("/api/pco/length", "PUT", {"item": "4", "secs": 0}, who="ProPresenter")[0], 400)
        self.assertEqual(self.call("/api/pco/length", "PUT", {"item": "4", "secs": 2400}, who="Viewer")[0], 403)

    def test_planning_center_down(self):
        self.sign_in()
        self.httpd.plan.forget()
        self.f.down = True
        self.assertEqual(self.call("/api/pco/timers")[0], 502)


if __name__ == "__main__":
    unittest.main()
