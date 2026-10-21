"""Tests for issue reports. Run: python3 -m unittest -v test_reports.py

A tiny fake email server and a fake monday.com run on this computer."""

import base64
import email
import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import reports as rp

PNG = base64.b64encode(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")).decode()


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self):
        self.mails, self.down = [], False
        super().__init__(("127.0.0.1", 0), SMTPHandler)


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        if srv.down:
            return  # closes at once, like no internet
        w = lambda line: self.wfile.write((line + "\r\n").encode())
        w("220 fake")
        rcpt, data = [], None
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            if not line:
                return
            cmd = line[:4].upper()
            if cmd in ("EHLO", "HELO"):
                w("250-fake"); w("250 AUTH PLAIN LOGIN")
            elif cmd == "AUTH":
                w("235 ok")
            elif cmd == "MAIL":
                w("250 ok")
            elif cmd == "RCPT":
                rcpt.append(line.split(":", 1)[1].strip(" <>")); w("250 ok")
            elif cmd == "DATA":
                w("354 go")
                buf = []
                while True:
                    l2 = self.rfile.readline()
                    if l2 in (b".\r\n", b".\n", b""):
                        break
                    buf.append(l2)
                srv.mails.append({"to": rcpt, "msg": email.message_from_bytes(b"".join(buf), policy=policy.default)})
                w("250 queued")
            elif cmd == "QUIT":
                w("221 bye"); return
            else:
                w("250 ok")


def fake_monday(calls):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            calls.append((self.path, self.headers.get("Authorization"), body))
            if self.path.endswith("/file"):
                out = {"data": {"add_file_to_update": {"id": "f1"}}}
            else:
                q = json.loads(body)["query"]
                out = {"data": {"create_item": {"id": "777"}}} if "create_item" in q else {"data": {"create_update": {"id": "888"}}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Valid(unittest.TestCase):
    def ok(self, **k):
        b = {"subject": "Lead vox crackles", "text": "Only on the handheld", "who": "Jonathan", **k}
        return rp.valid_report(b)

    def test_what_a_report_needs(self):
        self.assertEqual(self.ok(), "")
        self.assertIn("subject", self.ok(subject=" "))
        self.assertIn("who is reporting", self.ok(who=""))
        self.assertIn("what happened", self.ok(text=""))
        self.assertIn("email address", self.ok(to="not an address"))
        self.assertEqual(self.ok(to="pastor@example.org"), "")
        self.assertIn("Up to 4", self.ok(photos=[{"type": "image/png", "data": PNG}] * 5))
        self.assertIn("pictures", self.ok(photos=[{"type": "application/pdf", "data": PNG}]))
        self.assertIn("too big", self.ok(photos=[{"type": "image/jpeg", "data": "A" * (5 * 1024 * 1024)}]))


class Api(unittest.TestCase):
    def setUp(self):
        self.smtp = FakeSMTP()
        threading.Thread(target=self.smtp.serve_forever, daemon=True).start()
        self.calls = []
        self.mon = fake_monday(self.calls)
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as f:
            f.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"),
                   report_to="tech@c3.example", smtp_host="127.0.0.1", smtp_port=self.smtp.server_address[1], smtp_plain=True,
                   smtp_user="booth@c3.example", smtp_password="pw", monday_token="tok123", monday_base="http://127.0.0.1:%d" % self.mon.server_address[1])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.admin = self.login("Admin")

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.smtp.shutdown(); self.smtp.server_close()
        self.mon.shutdown(); self.mon.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

    def call(self, path, method="GET", body=None, tok=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def report(self, **k):
        b = {"subject": "Lead vox crackles", "text": "Only on the handheld,\nfrom the second song.", "who": "Jonathan", "page": "Mixer", "device": "iPhone",
             "photos": [{"type": "image/png", "data": PNG}, {"type": "image/png", "data": "data:image/png;base64," + PNG}], **k}
        return self.call("/api/reports", "POST", b, tok=k.pop("tok", None))

    def wait(self, f, secs=8):
        t0 = time.time()
        while time.time() - t0 < secs:
            if f():
                return True
            time.sleep(0.1)
        return False

    def test_anyone_can_report_and_it_is_emailed_with_photos(self):
        code, j = self.report()  # not signed in
        self.assertEqual(code, 200, j)
        self.assertTrue(self.wait(lambda: self.smtp.mails))
        m = self.smtp.mails[0]
        self.assertEqual(m["to"], ["tech@c3.example"])
        self.assertEqual(m["msg"]["Subject"], "C3 Life issue: Lead vox crackles")
        body = m["msg"].get_body(("plain",)).get_content()
        self.assertIn("from the second song", body)
        self.assertIn("Reported by: Jonathan", body)
        self.assertIn("Screen: Mixer", body)
        self.assertEqual([a.get_filename() for a in m["msg"].iter_attachments()], ["photo-1.png", "photo-2.png"])
        self.assertTrue(self.wait(lambda: self.call("/api/reports/mine?ids=" + j["report"]["id"])[1]["reports"][0]["email"] == "sent"))

    def test_each_report_is_its_own_email(self):
        self.report(); self.report(subject="Foyer speaker buzz")
        self.assertTrue(self.wait(lambda: len(self.smtp.mails) == 2))
        self.assertEqual(sorted(m["msg"]["Subject"] for m in self.smtp.mails), ["C3 Life issue: Foyer speaker buzz", "C3 Life issue: Lead vox crackles"])

    def test_send_to_a_different_address(self):
        self.report(to="pastor@c3.example")
        self.assertTrue(self.wait(lambda: self.smtp.mails))
        self.assertEqual(self.smtp.mails[0]["to"], ["pastor@c3.example"])

    def test_no_internet_waits_then_sends(self):
        self.smtp.down = True
        code, j = self.report()
        rid = j["report"]["id"]
        self.assertTrue(self.wait(lambda: "Trying again every minute" in self.call("/api/reports/mine?ids=" + rid)[1]["reports"][0]["email_error"]))
        self.assertEqual(self.call("/api/reports/mine?ids=" + rid)[1]["reports"][0]["email"], "waiting")
        self.smtp.down = False
        self.httpd.reports.wake.set()  # as the minute comes round
        self.assertTrue(self.wait(lambda: self.call("/api/reports/mine?ids=" + rid)[1]["reports"][0]["email"] == "sent"))

    def test_kept_after_a_restart(self):
        self.smtp.down = True
        rid = self.report()[1]["report"]["id"]
        again = rp.Reports(self.cfg["data_dir"], self.cfg, s.log)
        self.assertEqual(again.get(rid)["subject"], "Lead vox crackles")
        self.assertEqual(len(again.get(rid)["photos"]), 2)

    def test_admin_changes_the_address(self):
        code, j = self.call("/api/reports/settings", "PUT", {"to": "new-tech@c3.example"}, tok=self.admin)
        self.assertEqual((code, j["to"]), (200, "new-tech@c3.example"))
        self.report()
        self.assertTrue(self.wait(lambda: self.smtp.mails))
        self.assertEqual(self.smtp.mails[0]["to"], ["new-tech@c3.example"])
        self.assertEqual(self.call("/api/reports/settings", "PUT", {"to": "nope"}, tok=self.admin)[0], 400)

    def test_settings_never_show_the_password_or_token(self):
        j = self.call("/api/reports/settings", tok=self.admin)[1]
        self.assertTrue(j["email_ready"] and j["monday_ready"])
        self.assertNotIn("pw", json.dumps(j).replace("smtp", ""))
        self.assertNotIn("tok123", json.dumps(j))

    def test_only_admins_see_the_list_and_settings(self):
        foh = self.login("FOH sound")
        self.assertEqual(self.call("/api/reports", tok=foh)[0], 403)
        self.assertEqual(self.call("/api/reports/settings", "PUT", {"to": "x@y.org"}, tok=foh)[0], 403)
        self.report()
        self.assertEqual(self.call("/api/reports", tok=self.admin)[1]["reports"][0]["subject"], "Lead vox crackles")

    def test_signed_in_name_is_recorded(self):
        foh = self.login("FOH sound")
        rid = self.report(who="Jared", tok=foh)[1]["report"]["id"]
        self.assertEqual(self.httpd.reports.get(rid)["signed_in"], "FOH sound")
        self.assertTrue(self.wait(lambda: self.smtp.mails))
        self.assertIn("Reported by: Jared (signed in as FOH sound)", self.smtp.mails[0]["msg"].get_body(("plain",)).get_content())

    def test_monday_board_when_turned_on(self):
        self.assertEqual(self.call("/api/reports/settings", "PUT", {"monday": True}, tok=self.admin)[0], 400)  # needs a board
        self.assertEqual(self.call("/api/reports/settings", "PUT", {"monday": True, "monday_board": "1234567890", "monday_group": "topics"}, tok=self.admin)[0], 200)
        rid = self.report()[1]["report"]["id"]
        self.assertTrue(self.wait(lambda: self.call("/api/reports/mine?ids=" + rid)[1]["reports"][0]["monday"] == "added"))
        gql = [json.loads(b) for p, a, b in self.calls if not p.endswith("/file")]
        self.assertTrue(all(a == "tok123" for _, a, _ in self.calls))
        self.assertEqual(gql[0]["variables"], {"b": "1234567890", "g": "topics", "n": "Lead vox crackles"})
        self.assertIn("Reported by:</b> Jonathan", gql[1]["variables"]["t"])
        self.assertEqual(len([1 for p, _, _ in self.calls if p.endswith("/file")]), 2)  # the two photos
        self.assertEqual(self.httpd.reports.get(rid)["monday_item"], "777")

    def test_monday_off_by_default(self):
        self.report()
        self.assertTrue(self.wait(lambda: self.smtp.mails))
        time.sleep(0.3)
        self.assertEqual(self.calls, [])

    def test_bad_report_refused(self):
        code, j = self.call("/api/reports", "POST", {"subject": "", "text": "x", "who": "me"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
