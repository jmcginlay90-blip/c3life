"""Tests for name tags: Planning Center's 9 - Ministry team matched to ProPresenter props.
Run: python3 -m unittest -v test_nametags.py

A fake Planning Center and a fake ProPresenter run on this computer, using the shape of the real
plan for Sunday 18 October 2026 (Sunday Morning)."""

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import nametags as nt

TODAY = time.strftime("%Y-%m-%d")
ITEMS = [  # (id, type, title, [(note category, note)])
    ("10", "item", "Pre & Post Service", []), ("11", "item", "Countdown", []),
    ("12", "media", "Welcome", [("Hosting", "Billy & Sophie")]), ("13", "header", "Worship", []),
    ("14", "song", "Washed", []), ("15", "header", "PRAYER", []),
    ("16", "media", "60 Secs of Prayer", [("Hosting", "Sophie")]), ("17", "header", "Pastoral / Offering", []),
    ("18", "media", "PASTORAL Welcome", [("Hosting", "Billy")]), ("19", "media", "Offering", [("Hosting", "Billy")]),
    ("20", "song", "Reign Above It All", [("Sound", "Lead: Akin")]), ("21", "header", "Message", []),
    ("22", "item", "Message", [("Hosting", "Joel Burden - Chris to intro")]), ("23", "media", "Salvation Prayer", []),
    ("24", "header", "CLOSE", []), ("25", "item", "CLOSE", [("Hosting", "Chris")]),
]
PEOPLE = [  # (name, position, team, status)
    ("Guest Preacher", "Preach", "9", "U"), ("Chris Denham", "Meeting Leader", "9", "C"), ("Chris Denham", "Close", "9", "C"),
    ("Sophie Tagg", "Welcome", "9", "C"), ("Billy Tagg", "Welcome", "9", "C"), ("Billy Tagg", "Pastoral", "9", "C"),
    ("Billy Tagg", "Offering (3 min Encouragement)", "9", "C"), ("Sophie Tagg", "Prayer (post 2nd song)", "9", "C"),
    ("Jonathan McGinlay", "Sound", "2", "C"), ("Elly Fraser-Watts", "Set Leader", "1", "C"), ("Ezra Davidson", "Vocals", "1", "D"),
]
PROPS = ["Chris Denham", "Billy Tagg", "Sophie Tagg", "Logo bug"]


class Fake:
    """What the fake Planning Center and ProPresenter answer with, and what they were asked to do."""
    def __init__(self):
        self.items, self.people, self.props, self.messages = list(ITEMS), list(PEOPLE), list(PROPS), ["Name tag", "Welcome"]
        self.plan_date, self.down, self.pp_down, self.calls, self.auth = "2099-01-01", False, False, [], ""
        self.lengths = {}


def fake_server(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            p = u.path
            if p.startswith("/v1/"):
                return self.pp(p)
            fake.auth = self.headers.get("Authorization", "")
            if fake.down:
                return self.send(503, {"errors": []})
            if p == "/people/v2/me":  # (for the browser tests' Planning Center sign-in)
                return self.send(200, {"data": {"type": "Person", "id": "9", "attributes": {"name": "Jonathan McGinlay"}}})
            if p == "/services/v2":
                return self.send(200, {"data": {"type": "Organization", "id": "1", "attributes": {"name": "C3 Church", "time_zone": "Europe/London"}}})
            if p.endswith("/plan_times"):
                return self.send(200, {"data": [{"id": "71", "attributes": {"name": None, "time_type": "service", "starts_at": fake.plan_date + "T09:00:00Z"}}]})
            if p == "/services/v2/service_types":
                return self.send(200, {"data": [{"id": "1", "type": "ServiceType", "attributes": {"name": "Sunday Morning"}},
                                                {"id": "2", "type": "ServiceType", "attributes": {"name": "Carols"}}]})
            if p == "/services/v2/service_types/1/plans":
                if q.get("filter") == ["past"]:
                    return self.send(200, {"data": [{"id": "500", "type": "Plan", "attributes": {"dates": "11 October 2026", "sort_date": "2026-10-11T10:00:00Z"}}]})
                return self.send(200, {"data": [{"id": "501", "type": "Plan", "attributes": {"dates": "18 October 2026", "sort_date": fake.plan_date + "T10:00:00Z", "title": None}}]})
            if p.endswith("/items"):
                data, inc = [], []
                for iid, typ, title, notes in fake.items:
                    refs = []
                    for k, (cat, text) in enumerate(notes):
                        nid = iid + "n" + str(k)
                        refs.append({"type": "ItemNote", "id": nid})
                        inc.append({"type": "ItemNote", "id": nid, "attributes": {"category_name": cat, "content": text}})
                    data.append({"id": iid, "type": "Item", "attributes": {"title": title, "item_type": typ, "sequence": int(iid), "length": 0 if typ == "header" else fake.lengths.get(iid, 180)},
                                 "relationships": {"item_notes": {"data": refs}}})
                # two pages, like Planning Center does for long plans
                half = len(data) // 2
                if q.get("offset") == ["1"]:
                    return self.send(200, {"data": data[half:], "included": inc, "links": {}})
                return self.send(200, {"data": data[:half], "included": inc, "links": {"next": "https://api.planningcenteronline.com" + p + "?offset=1"}})
            if p.endswith("/team_members"):
                data = [{"id": str(i), "type": "PlanPerson", "attributes": {"name": n, "team_position_name": pos, "status": st},
                         "relationships": {"team": {"data": {"type": "Team", "id": t}}}} for i, (n, pos, t, st) in enumerate(fake.people)]
                inc = [{"type": "Team", "id": "9", "attributes": {"name": "9 - Ministry"}}, {"type": "Team", "id": "1", "attributes": {"name": "1 - Creative: Band"}},
                       {"type": "Team", "id": "2", "attributes": {"name": "2 - Creative: Production"}}]
                return self.send(200, {"data": data, "included": inc})
            self.send(404, {})

        def do_PATCH(self):  # an item's length changed from the app
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            fake.lengths[self.path.split("/")[-1]] = body["data"]["attributes"]["length"]
            self.send(200, body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            if fake.pp_down:
                return self.send(503)
            fake.calls.append(("POST", self.path, body))
            self.send(204)

        def pp(self, p):
            if fake.pp_down:
                return self.send(503)
            if p == "/v1/props":
                return self.send(200, [{"id": {"uuid": "prop-" + n.replace(" ", ""), "name": n, "index": i}} for i, n in enumerate(fake.props)])
            if p == "/v1/messages":
                return self.send(200, [{"id": {"uuid": "msg-" + n.replace(" ", ""), "name": n, "index": i}, "tokens": [{"name": "Name"}]} for i, n in enumerate(fake.messages)])
            fake.calls.append(("GET", p, None))
            self.send(204)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Unit(unittest.TestCase):
    def test_names_match_props_whatever_the_case_or_title(self):
        self.assertEqual(nt.norm("Ps  Chris Denham"), nt.norm("chris denham"))
        self.assertEqual(nt.norm("Billy and Sophie Tagg"), nt.norm("Billy & Sophie Tagg"))

    def test_couples_with_one_surname(self):
        self.assertEqual(nt.join_names(["Billy Tagg", "Sophie Tagg"]), "Billy & Sophie Tagg")
        self.assertEqual(nt.join_names(["Kathi Kelly", "Chris Denham"]), "Kathi Kelly & Chris Denham")
        self.assertEqual(nt.join_names(["Chris Denham"]), "Chris Denham")

    def test_bad_links_refused(self):
        self.assertTrue(nt.valid_rules(nt.DEFAULT_RULES))
        self.assertFalse(nt.valid_rules([{"position": "Welcome", "item": "("}]))
        self.assertFalse(nt.valid_rules("Welcome"))
        self.assertFalse(nt.valid_rules([{"position": "", "item": "x"}]))


class NameTagTest(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.ext = fake_server(self.fake)
        url = "http://127.0.0.1:%d" % self.ext.server_address[1]
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as f:
            f.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"),
                   pco_app_id="app123", pco_secret="sec456", pco_base=url, propresenter=url)
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {n: self.login(n) for n in ("Admin", "ProPresenter", "Viewer", "Keys (musician)", "FOH sound")}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.ext.shutdown()
        self.ext.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who="Admin"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "1234" if name != "Viewer" else ""}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

    def tags(self, who="Admin"):
        code, d = self.call("/api/nametags/refresh", "POST", {}, who=who)
        self.assertEqual(code, 200, d)
        return d, {t["position"]: t for t in d["tags"]}

    def test_this_weeks_names_from_9_ministry(self):
        d, by = self.tags()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["plan"]["dates"], "18 October 2026")
        self.assertEqual(by["Welcome"]["item"], "Welcome")
        self.assertEqual(by["Welcome"]["text"], "Billy & Sophie Tagg")
        self.assertEqual(by["Prayer (post 2nd song)"]["item"], "60 Secs of Prayer")  # not Salvation Prayer
        self.assertEqual(by["Prayer (post 2nd song)"]["text"], "Sophie Tagg")
        self.assertEqual(by["Pastoral"]["item"], "PASTORAL Welcome")
        self.assertEqual(by["Offering (3 min Encouragement)"]["text"], "Billy Tagg")
        self.assertEqual(by["Close"]["item"], "CLOSE")
        self.assertEqual(by["Close"]["item_id"], "25")  # the item, not the CLOSE header
        self.assertEqual(by["Close"]["text"], "Chris Denham")
        self.assertEqual(d["leader"]["text"], "Chris Denham")
        self.assertNotIn("Communion (post 2nd song)", by)  # nobody on it this week: no tag
        self.assertTrue(self.fake.auth.startswith("Basic "))

    def test_guest_preacher_uses_the_hosting_note(self):
        _, by = self.tags()
        self.assertEqual(by["Preach"]["item"], "Message")
        self.assertEqual(by["Preach"]["text"], "Joel Burden")  # "Joel Burden - Chris to intro"
        self.assertEqual(by["Preach"]["source"], "note")
        self.assertEqual(by["Preach"]["placeholder"], "Guest Preacher")

    def test_guest_preacher_with_no_note_is_flagged(self):
        self.fake.items = [i if i[0] != "22" else ("22", "item", "Message", []) for i in ITEMS]
        _, by = self.tags()
        self.assertEqual(by["Preach"]["text"], "")
        self.assertEqual(by["Preach"]["source"], "missing")
        self.assertEqual(by["Preach"]["match"]["kind"], "none")

    def test_first_names_in_notes_become_full_names(self):
        self.fake.people = [p for p in PEOPLE if p[1] != "Close"]  # nobody on Close: the note "Chris" is used
        _, by = self.tags()
        self.assertEqual(by["Close"]["text"], "Chris Denham")
        self.assertEqual(by["Close"]["source"], "note")

    def test_declined_people_are_left_out(self):
        self.fake.people = PEOPLE + [("Mark Kelly", "Pastoral", "9", "D")]
        _, by = self.tags()
        self.assertEqual(by["Pastoral"]["text"], "Billy Tagg")

    def test_props_and_the_name_tag_message(self):
        _, by = self.tags()
        self.assertEqual(by["Close"]["match"], {"kind": "prop", "uuid": "prop-ChrisDenham", "name": "Chris Denham"})
        self.assertEqual(by["Welcome"]["match"]["kind"], "message")  # no prop for the couple
        self.assertEqual(by["Preach"]["match"]["kind"], "message")  # guest: no prop
        self.fake.props = PROPS + ["Billy & Sophie Tagg"]
        _, by = self.tags()
        self.assertEqual(by["Welcome"]["match"]["kind"], "prop")

    def test_no_message_in_propresenter_says_why(self):
        self.fake.messages = ["Welcome"]
        _, by = self.tags()
        self.assertEqual(by["Preach"]["match"]["kind"], "none")
        self.assertIn("No prop called Joel Burden", by["Preach"]["match"]["why"])

    def test_nothing_goes_on_screen_by_itself(self):
        self.tags()
        self.call("/api/nametags")
        time.sleep(0.3)
        self.assertEqual([c for c in self.fake.calls if "trigger" in c[1]], [])

    def test_show_a_prop_then_hide_it(self):
        _, by = self.tags()
        code, d = self.call("/api/nametags/show", "POST", {"id": by["Close"]["id"]}, who="ProPresenter")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["showing"], "25")
        self.assertIn(("GET", "/v1/prop/prop-ChrisDenham/trigger", None), self.fake.calls)
        code, d = self.call("/api/nametags/hide", "POST", {}, who="ProPresenter")
        self.assertEqual((code, d["showing"]), (200, ""))
        self.assertIn(("GET", "/v1/prop/prop-ChrisDenham/clear", None), self.fake.calls)

    def test_show_fills_in_the_message(self):
        _, by = self.tags()
        code, d = self.call("/api/nametags/show", "POST", {"id": by["Welcome"]["id"]}, who="ProPresenter")
        self.assertEqual(code, 200, d)
        self.assertIn(("POST", "/v1/message/msg-Nametag/trigger", [{"name": "Name", "text": {"text": "Billy & Sophie Tagg"}}]), self.fake.calls)

    def test_showing_another_takes_the_first_off(self):
        _, by = self.tags()
        self.call("/api/nametags/show", "POST", {"id": by["Close"]["id"]})
        self.call("/api/nametags/show", "POST", {"id": by["Welcome"]["id"]})
        self.assertIn(("GET", "/v1/prop/prop-ChrisDenham/clear", None), self.fake.calls)

    def test_meeting_leader_button(self):
        self.tags()
        code, d = self.call("/api/nametags/show", "POST", {"id": "leader"})
        self.assertEqual(code, 200, d)
        self.assertIn(("GET", "/v1/prop/prop-ChrisDenham/trigger", None), self.fake.calls)

    def test_a_missing_name_cant_be_shown(self):
        self.fake.items = [i if i[0] != "22" else ("22", "item", "Message", []) for i in ITEMS]
        _, by = self.tags()
        code, d = self.call("/api/nametags/show", "POST", {"id": by["Preach"]["id"]})
        self.assertEqual(code, 409)
        self.assertEqual(d["error"], "Needs a name.")

    def test_type_a_name_and_put_it_back(self):
        _, by = self.tags()
        code, d = self.call("/api/nametags/name", "PUT", {"item": "22", "text": "  Ps Sarah   Seibert "}, who="ProPresenter")
        self.assertEqual(code, 200, d)
        t = next(x for x in d["tags"] if x["item_id"] == "22")
        self.assertEqual((t["text"], t["source"], t["match"]["kind"]), ("Ps Sarah Seibert", "typed", "message"))
        _, by = self.tags()  # survives a re-read of Planning Center
        self.assertEqual(by["Preach"]["text"], "Ps Sarah Seibert")
        code, d = self.call("/api/nametags/name", "PUT", {"item": "22", "text": ""})
        self.assertEqual(next(x for x in d["tags"] if x["item_id"] == "22")["text"], "Joel Burden")

    def test_typed_names_survive_a_restart(self):
        self.tags()
        self.call("/api/nametags/name", "PUT", {"item": "22", "text": "Sarah Seibert"})
        again = nt.NameTags(self.cfg["data_dir"], self.cfg, s.log)
        by = {t["position"]: t for t in again.refresh()["tags"]}
        self.assertEqual(by["Preach"]["text"], "Sarah Seibert")

    def test_who_can_do_what(self):
        self.tags()
        self.assertEqual(self.call("/api/nametags", who="Viewer")[0], 200)  # look only
        self.assertEqual(self.call("/api/nametags/show", "POST", {"id": "leader"}, who="Viewer")[0], 403)
        self.assertEqual(self.call("/api/nametags/name", "PUT", {"item": "22", "text": "x"}, who="Viewer")[0], 403)
        self.assertEqual(self.call("/api/nametags", who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/nametags", who=None)[0], 401)
        self.assertEqual(self.call("/api/nametags/rules", "PUT", {"rules": None}, who="ProPresenter")[0], 403)

    def test_admin_changes_which_item_goes_with_a_position(self):
        rules = [dict(r) for r in nt.DEFAULT_RULES]
        next(r for r in rules if r["position"] == "Offering (3 min Encouragement)")["item"] = "^countdown$"
        code, d = self.call("/api/nametags/rules", "PUT", {"rules": rules})
        self.assertEqual(code, 200, d)
        by = {t["position"]: t for t in d["tags"]}
        self.assertEqual(by["Offering (3 min Encouragement)"]["item"], "Countdown")
        self.assertFalse(d["default_rules"])
        self.assertEqual(self.call("/api/nametags/rules", "PUT", {"rules": [{"position": "x", "item": "("}]})[0], 400)
        code, d = self.call("/api/nametags/rules", "PUT", {"rules": None})  # back to the defaults
        self.assertTrue(d["default_rules"])

    def test_on_sunday_morning_todays_plan_is_used(self):
        self.fake.plan_date = "2099-01-01"
        old = nt.time.strftime
        nt.time.strftime = lambda f, *a: "2026-10-11" if f == "%Y-%m-%d" else old(f, *a)
        try:
            d, _ = self.tags()
        finally:
            nt.time.strftime = old
        self.assertEqual(d["plan"]["dates"], "11 October 2026")

    def test_planning_center_down_keeps_the_last_read(self):
        self.tags()
        self.fake.down = True
        d, by = self.tags()
        self.assertEqual(d["status"], "unavailable")
        self.assertTrue(d["stale"])
        self.assertEqual(by["Close"]["text"], "Chris Denham")
        self.assertNotIn("sec456", json.dumps(d))

    def test_propresenter_down(self):
        self.fake.pp_down = True
        d, by = self.tags()
        self.assertEqual(by["Close"]["match"]["kind"], "none")
        self.assertIn("ProPresenter didn't answer", d["pp_reason"])

    def test_not_set_up(self):
        self.httpd.tags.cfg = dict(self.cfg, pco_app_id="")
        d, _ = self.tags()
        self.assertEqual(d["status"], "off")
        self.assertIn("isn't set up", d["reason"])


if __name__ == "__main__":
    unittest.main()
