"""Tests for ProPresenter live (propresenter.py) against a made-up ProPresenter with two songs, a playlist and timers.
FakePP is used by the browser tests' server too. Run: python3 -m unittest -v test_propresenter.py"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import propresenter as ppm

JPEG = b"\xff\xd8\xff\xe0" + b"slide" + b"\xff\xd9"


class FakePP:
    """A pretend ProPresenter 7: presentations with groups and slides, the playlist, triggers, clears, timers."""

    def __init__(self):
        self.pres = {
            "P1": {"name": "Way Maker", "groups": [("Verse 1", ["You are here", "Moving in our midst"]), ("Chorus", ["Way maker", "Miracle worker"])]},
            "P2": {"name": "Message", "groups": [("Title", ["Hope that holds"]), ("Points", ["1. Anchor", "2. Shore"])]},
        }
        self.playlist = [("P1", "Song 1: Way Maker"), ("P2", "Message")]
        self.at = ("P1", 0)
        self.cleared = []
        self.timers = {"T1": {"name": "Service", "duration": 600, "left": 600.0, "running": False, "since": 0},
                       "T2": {"name": "Song", "elapsed": True, "left": 0.0, "running": True, "since": time.time()}}
        self.lock = threading.Lock()
        self.calls = []

    def slides(self, uid):
        return [(g, t) for g, ts in self.pres[uid]["groups"] for t in ts]

    def left(self, t):
        if not t["running"]:
            return t["left"]
        d = time.time() - t["since"]
        return t["left"] + d if t.get("elapsed") else t["left"] - d

    def handle(self, method, path, body):
        self.calls.append((method, path))
        uid, i = self.at
        if path == "/v1/presentation/slide_index":
            return {"presentation_index": {"index": i, "presentation_id": {"uuid": uid, "name": self.pres[uid]["name"]}}}
        if path == "/v1/status/slide":
            s = self.slides(uid)
            return {"current": {"text": s[i][1]}, "next": {"text": s[i + 1][1] if i + 1 < len(s) else ""}}
        if path == "/v1/playlist/active":
            return {"presentation": {"playlist": {"name": "Sunday"}, "item": {"name": dict(self.playlist)[uid]}}}
        if path.startswith("/v1/presentation/") and "/thumbnail/" in path:
            return JPEG
        if path.startswith("/v1/presentation/") and path.endswith("/trigger"):
            _, _, _, u, n, _ = path.split("/")
            self.at = (u, int(n))
            return None
        if path.startswith("/v1/presentation/"):
            u = path.split("/")[3]
            p = self.pres[u]
            return {"presentation": {"id": {"uuid": u, "name": p["name"]},
                                     "groups": [{"name": g, "slides": [{"label": "", "text": t} for t in ts]} for g, ts in p["groups"]]}}
        if path == "/v1/trigger/next":
            n = len(self.slides(uid))
            self.at = (uid, min(n - 1, i + 1))
            return None
        if path == "/v1/trigger/previous":
            self.at = (uid, max(0, i - 1))
            return None
        if path.startswith("/v1/clear/layer/"):
            self.cleared.append(path.rsplit("/", 1)[1])
            return None
        if path == "/v1/timers/current":
            out = []
            for k, t in self.timers.items():
                s = int(self.left(t))
                out.append({"id": {"uuid": k, "name": t["name"]}, "time": "%02d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60),
                            "state": "running" if t["running"] else "stopped"})
            return out
        if path == "/v1/timers":
            return [dict({"id": {"uuid": k, "name": t["name"]}}, **({"elapsed": {}} if t.get("elapsed") else {"countdown": {"duration": t["duration"]}}))
                    for k, t in self.timers.items()]
        if path.startswith("/v1/timer/"):
            parts = path.split("/")
            t = self.timers[parts[3]]
            if len(parts) == 5:
                op = parts[4]
                if op == "start" and not t["running"]:
                    t.update(running=True, since=time.time())
                elif op == "stop" and t["running"]:
                    t.update(left=self.left(t), running=False)
                elif op == "reset":
                    t.update(left=0.0 if t.get("elapsed") else float(t["duration"]), since=time.time())
                return None
            if method == "PUT":
                t["duration"] = body["countdown"]["duration"]
                t.update(left=float(t["duration"]), since=time.time())
                return None
            return {"id": {"uuid": parts[3], "name": t["name"]}, "countdown": {"duration": t.get("duration", 0)}} if not t.get("elapsed") else {"id": {"uuid": parts[3]}, "elapsed": {}}
        raise KeyError(path)


def fake_server(fake):
    class H(BaseHTTPRequestHandler):
        def go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            try:
                with fake.lock:
                    out = fake.handle(method, self.path.split("?")[0], body)
            except KeyError:
                self.send_response(404); self.end_headers(); return
            b = out if isinstance(out, bytes) else (json.dumps(out).encode() if out is not None else b"")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg" if isinstance(out, bytes) else "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self.go("GET")

        def do_PUT(self):
            self.go("PUT")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Live(unittest.TestCase):
    def setUp(self):
        self.fake = FakePP()
        self.srv = fake_server(self.fake)
        self.p = ppm.ProPresenter({"propresenter": "http://127.0.0.1:%d" % self.srv.server_address[1]}, None)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()

    def test_now_next_and_moving(self):
        st = self.p.status()
        self.assertEqual((st["presentation"], st["index"], st["count"]), ("Way Maker", 0, 4))
        self.assertEqual((st["current"]["group"], st["current"]["text"], st["next"]["text"], st["after"]["group"]), ("Verse 1", "You are here", "Moving in our midst", "Chorus"))
        self.assertIsNone(self.p.trigger("next", who="FOH"))
        self.assertEqual(self.p.status()["index"], 1)
        self.assertIsNone(self.p.trigger("go", 3, who="FOH"))
        st = self.p.status()
        self.assertEqual((st["index"], st["current"]["text"], st["next"]), (3, "Miracle worker", None))
        self.assertIn("no slide 9", self.p.trigger("go", 9))
        self.assertIsNone(self.p.trigger("previous"))
        self.assertEqual(self.p.status()["index"], 2)
        self.assertIsNone(self.p.trigger("clear", layer="all"))
        self.assertEqual(self.fake.cleared, list(ppm.LAYERS))
        self.assertIn("can't be cleared", self.p.trigger("clear", layer="everything"))
        self.assertEqual(self.p.thumb("P1", 2, 320), JPEG)

    def test_timers(self):
        t = {x["id"]: x for x in self.p.timers()}
        self.assertEqual((t["T1"]["name"], t["T1"]["kind"], t["T1"]["time"], t["T1"]["running"]), ("Service", "Countdown", "10:00", False))
        self.assertEqual((t["T2"]["kind"], t["T2"]["running"]), ("Elapsed", True))
        self.assertIsNone(self.p.timer("T1", "start"))
        self.assertTrue({x["id"]: x for x in self.p.timers()}["T1"]["running"])
        self.assertIsNone(self.p.timer("T1", "add", 300))
        t1 = {x["id"]: x for x in self.p.timers()}["T1"]
        self.assertAlmostEqual(t1["secs"], 900, delta=3)  # +5 minutes
        self.assertTrue(t1["running"])
        self.assertIsNone(self.p.timer("T1", "set", 120))
        self.assertAlmostEqual({x["id"]: x for x in self.p.timers()}["T1"]["secs"], 120, delta=3)
        self.assertIn("Only a countdown", self.p.timer("T2", "add", 60))
        self.assertIsNone(self.p.timer("T1", "stop"))
        self.assertIsNone(self.p.timer("T1", "reset"))
        self.assertEqual({x["id"]: x for x in self.p.timers()}["T1"]["running"], False)

    def test_not_found_yet_or_not_answering(self):
        self.assertIn("hasn't been found", ppm.ProPresenter({}, None).status()["error"])
        gone = ppm.ProPresenter({"propresenter": "http://127.0.0.1:1"}, None).status()
        self.assertEqual((gone["connected"], gone["error"][:26]), (False, "ProPresenter didn't answer"))


class Api(unittest.TestCase):
    def setUp(self):
        import c3life_server as s
        self.fake = FakePP()
        self.pps = fake_server(self.fake)
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        with open(os.path.join(app, "player.html"), "w") as fh:
            fh.write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"),
                   propresenter="http://127.0.0.1:%d" % self.pps.server_address[1], desk="none")
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {}
        for n in ("ProPresenter", "Viewer", "Keys (musician)"):
            self.tok[n] = self.call("/api/login", "POST", {"name": n, "pin": "" if n == "Viewer" else "1234"})[1].get("token")

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.pps.shutdown(); self.pps.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, who=None, raw=False):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if who:
            req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                b = r.read()
                return r.status, (b if raw else json.loads(b))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_who_may_and_next_slide(self):
        code, j = self.call("/api/pp/status", who="Viewer")
        self.assertEqual((code, j["current"]["text"]), (200, "You are here"))
        self.assertEqual(self.call("/api/pp/trigger", "POST", {"do": "next"}, who="Viewer")[0], 403)
        self.assertEqual(self.call("/api/pp/status", who="Keys (musician)")[0], 403)
        code, j = self.call("/api/pp/trigger", "POST", {"do": "next"}, who="ProPresenter")
        self.assertEqual((code, j["index"]), (200, 1))
        code, img = self.call("/api/pp/thumb?u=P1&i=1&w=320&t=" + self.tok["Viewer"], raw=True)
        self.assertEqual((code, img), (200, JPEG))
        self.assertEqual(self.call("/api/pp/thumb?u=P1&i=1&t=nope")[0], 401)
        code, j = self.call("/api/pp/timer", "POST", {"id": "T1", "do": "start"}, who="ProPresenter")
        self.assertEqual((code, {t["id"]: t for t in j["timers"]}["T1"]["running"]), (200, True))


if __name__ == "__main__":
    unittest.main()
