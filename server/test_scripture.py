"""Tests for Bible Scripture. Run: python3 -m unittest -v test_scripture.py

KJV and WEBBE are read from the real files in bibles\\. ESV, API.Bible (NIV), NLT and ProPresenter are fakes
on this computer, answering the way their documentation says the real ones do."""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c3life_server as s
import scripture as sc

NIV = {"JHN.3.16-JHN.3.17": "[16] For God so loved the world that he gave his one and only Son, that whoever believes in him shall not perish but have eternal life. [17] For God did not send his Son into the world to condemn the world, but to save the world through him.",
       "JHN.3.16": "[16] For God so loved the world that he gave his one and only Son, that whoever believes in him shall not perish but have eternal life."}


class Fake:
    def __init__(self):
        self.messages = ["Name tag", "Scripture - Full screen", "Scripture - Lower third"]
        self.playlists = [{"id": {"uuid": "G1", "name": "Sundays", "index": 0}, "field_type": "group", "children": [
            {"id": {"uuid": "PL18", "name": "18 October", "index": 0}, "field_type": "playlist", "children": []}]},
            {"id": {"uuid": "PLX", "name": "Christmas", "index": 1}, "field_type": "playlist", "children": []}]
        self.items = {"PL18": [{"id": {"uuid": "A", "name": "Welcome", "index": 0}, "type": "presentation", "is_hidden": False, "is_pco": True,
                                "presentation_info": {"presentation_uuid": "P1"}},
                               {"id": {"uuid": "B", "name": "Bible Scripture", "index": 1}, "type": "placeholder", "is_hidden": False, "is_pco": True},
                               {"id": {"uuid": "C", "name": "Washed", "index": 2}, "type": "presentation", "is_hidden": False, "is_pco": True,
                                "presentation_info": {"presentation_uuid": "P2"}}], "PLX": []}
        self.calls, self.pp_down, self.web_down, self.ignore_put, self.esv_auth, self.api_key = [], False, False, False, "", ""


def fake_server(f):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body=None, raw=None):
            data = raw.encode() if raw is not None else (json.dumps(body).encode() if body is not None else b"")
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null")

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
            if p.startswith("/v1/") and not p.startswith("/v1/bibles/"):
                if f.pp_down:
                    return self.send(503)
                if p == "/v1/messages":
                    return self.send(200, [{"id": {"uuid": "M-" + m.split(" - ")[-1].replace(" ", ""), "name": m, "index": i}} for i, m in enumerate(f.messages)])
                if p == "/v1/props":  # so the same fake does for name tags in the browser tests
                    return self.send(200, [{"id": {"uuid": "prop-" + n.replace(" ", ""), "name": n, "index": i}} for i, n in enumerate(["Chris Denham", "Billy Tagg", "Sophie Tagg"])])
                if p == "/v1/playlists":
                    return self.send(200, f.playlists)
                if p == "/v1/playlist/active":
                    return self.send(200, {"presentation": {"playlist": {"uuid": "PL18", "name": "18 October", "index": 0}}})
                if p.startswith("/v1/presentation/"):
                    groups = {"P1": [{"name": "Welcome", "slides": [{"label": "Hello"}]}],
                              "P2": [{"name": "Verse 1", "slides": [{"label": ""}]}, {"name": "Bridge", "slides": [{"label": "Big build"}]}]}.get(p.split("/")[-1], [])
                    return self.send(200, {"presentation": {"id": {"uuid": p.split("/")[-1]}, "groups": groups}})
                if p.startswith("/v1/playlist/"):
                    pid = p.split("/")[-1]
                    return self.send(200, {"id": {"uuid": pid}, "items": f.items.get(pid, [])})
                f.calls.append(("GET", p, None))
                return self.send(204)
            if f.web_down:
                return self.send(503)
            if p == "/v3/passage/text/":
                f.esv_auth = self.headers.get("Authorization", "")
                text = {"Romans 8:38-9:1": " [38] For I am sure that neither death nor life, [39] nor height nor depth, will be able to separate us. [9:1] I am speaking the truth in Christ—I am not lying;",
                        "John 3:16": " [16] “For God so loved the world, that he gave his only Son, that whoever believes in him should not perish but have eternal life.”"}.get(q["q"])
                return self.send(200, {"query": q["q"], "passages": [text] if text else []})
            if p.startswith("/v1/bibles/"):
                f.api_key = self.headers.get("api-key", "")
                pid = p.split("/")[-1]
                if pid not in NIV:
                    return self.send(404, {"statusCode": 404})
                return self.send(200, {"data": {"id": pid, "content": NIV[pid], "copyright": "THE HOLY BIBLE, NEW INTERNATIONAL VERSION® NIV® Copyright © 2011 Biblica"}})
            if p == "/api/passages":
                if q.get("ref") != "John.3.16-17" or q.get("key") != "nltkey":
                    return self.send(200, raw="<html></html>")
                return self.send(200, raw='<div><verse_export orig="john_3_16" bk="John" ch="3" vn="16"><h3 class="subhead">God’s Love</h3><span class="vn">16</span>“For this is how God loved the world: He gave his one and only Son, so that everyone who believes in him will not perish but have eternal life.<span class="tn">Or <em>the only Son</em></span></verse_export>'
                                          '<verse_export orig="john_3_17" bk="John" ch="3" vn="17"><span class="vn">17</span>God sent his Son into the world not to judge the world, but to save the world through him.</verse_export></div>')
            self.send(404, {})

        def do_PUT(self):
            pid = self.path.split("/")[-1]
            b = self.body()
            f.calls.append(("PUT", self.path, b))
            if not f.ignore_put:
                f.items[pid] = b
            self.send(204)

        def do_POST(self):
            if f.pp_down:
                return self.send(503)
            f.calls.append(("POST", self.path, self.body()))
            self.send(204)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Parse(unittest.TestCase):
    def test_how_people_type_references(self):
        self.assertEqual(sc.parse("john3:16"), ("JHN", 3, 16, 3, 16))
        self.assertEqual(sc.parse(" John 3 : 16 – 18 "), ("JHN", 3, 16, 3, 18))
        self.assertEqual(sc.parse("1 Cor. 13:4-7"), ("1CO", 13, 4, 13, 7))
        self.assertEqual(sc.parse("first john 4:8"), ("1JN", 4, 8, 4, 8))
        self.assertEqual(sc.parse("Ps 23"), ("PSA", 23, None, 23, None))
        self.assertEqual(sc.parse("Rom 8:38-9:1"), ("ROM", 8, 38, 9, 1))
        self.assertEqual(sc.parse("jn 3.16"), ("JHN", 3, 16, 3, 16))
        self.assertEqual(sc.parse("phil 4:13")[0], "PHP")
        self.assertEqual(sc.parse("Philemon 1:4")[0], "PHM")
        self.assertEqual(sc.parse("Judg 6:12")[0], "JDG")
        self.assertEqual(sc.parse("Jude 1:24")[0], "JUD")
        self.assertEqual(sc.parse("Song of Songs 2:4")[0], "SNG")

    def test_bad_references_say_why(self):
        for bad, why in (("", "Type a book"), ("Hezekiah 3:1", "Which book"), ("John 3:18-16", "backwards"), ("J 3:16", "Which book")):
            with self.assertRaises(sc.NotFound) as e:
                sc.parse(bad)
            self.assertIn(why, str(e.exception))


class Offline(unittest.TestCase):
    def setUp(self):
        self.b = sc.Scripture(tempfile.mkdtemp(), {}, s.log)

    def test_kjv_from_the_file_without_brackets(self):
        p = self.b.passage("Psalm 23:1", "KJV")
        self.assertEqual(p["reference"], "Psalm 23:1")
        self.assertEqual(p["verses"][0]["text"], "A Psalm of David. The LORD is my shepherd; I shall not want.")
        self.assertEqual(p["slides"], [{"ref": "Psalm 23:1 (KJV)", "text": "A Psalm of David. The LORD is my shepherd; I shall not want."}])

    def test_webbe_says_the_lord(self):
        p = self.b.passage("Ps 23:1", "WEBBE")
        self.assertIn("The LORD is my shepherd", p["verses"][0]["text"])

    def test_a_whole_chapter_becomes_several_slides_with_verse_numbers(self):
        p = self.b.passage("Psalm 23", "WEBBE")
        self.assertEqual((p["reference"], len(p["verses"])), ("Psalm 23:1–6", 6))
        self.assertTrue(p["slides"][0]["text"].startswith("¹ A Psalm by David."))
        self.assertTrue(all(len(x["text"]) <= 222 for x in p["slides"]))
        self.assertEqual(p["slides"][0]["ref"], "Psalm 23:1–2 (WEBBE)")

    def test_a_long_verse_is_split(self):
        p = self.b.passage("Esther 8:9", "KJV")
        self.assertGreater(len(p["slides"]), 1)
        self.assertTrue(all(x["ref"] == "Esther 8:9 (KJV)" for x in p["slides"]))
        self.assertEqual(" ".join(x["text"] for x in p["slides"]).replace("  ", " "), p["verses"][0]["text"])

    def test_across_two_chapters(self):
        p = self.b.passage("Romans 8:38-9:1", "KJV")
        self.assertEqual(p["reference"], "Romans 8:38–9:1")
        self.assertEqual([(v["c"], v["v"]) for v in p["verses"]], [(8, 38), (8, 39), (9, 1)])

    def test_no_such_chapter_or_too_long(self):
        with self.assertRaises(sc.NotFound):
            self.b.passage("Jude 2:1", "KJV")
        with self.assertRaises(sc.NotFound) as e:
            self.b.passage("Psalm 119", "KJV")
        self.assertIn("176 verses", str(e.exception))

    def test_versions_without_keys_say_what_they_need(self):
        vs = {v["code"]: v for v in self.b.versions()}
        self.assertTrue(vs["KJV"]["ok"] and vs["WEBBE"]["ok"])
        self.assertFalse(vs["NIV"]["ok"])
        self.assertIn("NIV", vs["NIV"]["why"])
        self.assertIn("api.esv.org", vs["ESV"]["why"])
        self.assertEqual(self.b.default_version(), "KJV")  # NIV isn't set up, so the first one that is
        with self.assertRaises(sc.NotFound) as e:
            self.b.passage("John 3:16", "NIV")
        self.assertIn("isn’t set up", str(e.exception))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.ext = fake_server(self.f)
        url = "http://127.0.0.1:%d" % self.ext.server_address[1]
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        open(os.path.join(app, "player.html"), "w").write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter=url,
                   esv_key="esvkey", esv_base=url, nlt_key="nltkey", nlt_base=url, apibible_key="abkey", apibible_base=url,
                   apibible_ids={"NIV": "78a9f6124f344018-01"})
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {n: self.login(n) for n in ("Admin", "ProPresenter", "Viewer", "Keys (musician)")}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.ext.shutdown()
        self.ext.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "" if name == "Viewer" else "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

    def call(self, path, method="GET", body=None, who="ProPresenter"):
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

    def look(self, ref, v):
        return self.call("/api/scripture/passage?" + urllib.parse.urlencode({"ref": ref, "v": v}))

    def add(self, ref="John 3:16-17", v="NIV", look="M-Fullscreen", playlist="PL18", after=1):
        return self.call("/api/scripture/add", "POST", {"ref": ref, "v": v, "look": look, "playlist": playlist, "after": after})

    def test_the_screen_gets_versions_looks_and_playlists(self):
        code, d = self.call("/api/scripture")
        self.assertEqual(code, 200, d)
        self.assertEqual([v["code"] for v in d["versions"]], ["NIV", "ESV", "NLT", "KJV", "WEBBE"])
        self.assertTrue(all(v["ok"] for v in d["versions"]))
        self.assertEqual(d["default"], "NIV")
        self.assertEqual([x["name"] for x in d["looks"]], ["Scripture - Full screen", "Scripture - Lower third"])  # not the Name tag message
        self.assertEqual(d["playlists"], [{"uuid": "PL18", "name": "Sundays › 18 October"}, {"uuid": "PLX", "name": "Christmas"}])
        self.assertEqual(d["active_playlist"], "PL18")
        self.assertNotIn("abkey", json.dumps(d))

    def test_niv_from_api_bible(self):
        code, p = self.look("john3:16-17", "NIV")
        self.assertEqual(code, 200, p)
        self.assertEqual(self.f.api_key, "abkey")
        self.assertEqual(p["reference"], "John 3:16–17")
        self.assertEqual(p["verses"][1], {"c": 3, "v": 17, "text": "For God did not send his Son into the world to condemn the world, but to save the world through him."})
        self.assertIn("Biblica", p["copyright"])
        code, p = self.look("John 3:16", "NIV")
        self.assertEqual(p["slides"][0]["ref"], "John 3:16 (NIV)")
        self.assertFalse(p["slides"][0]["text"].startswith("¹"))  # one verse: no number

    def test_esv_with_its_key_and_across_chapters(self):
        code, p = self.look("Rom 8:38-9:1", "ESV")
        self.assertEqual(code, 200, p)
        self.assertEqual(self.f.esv_auth, "Token esvkey")
        self.assertEqual([(v["c"], v["v"]) for v in p["verses"]], [(8, 38), (8, 39), (9, 1)])
        self.assertIn("Crossway", p["copyright"])

    def test_nlt_without_headings_or_notes(self):
        code, p = self.look("John 3:16-17", "NLT")
        self.assertEqual(code, 200, p)
        self.assertEqual(p["verses"][0]["text"], "“For this is how God loved the world: He gave his one and only Son, so that everyone who believes in him will not perish but have eternal life.")
        self.assertEqual(p["verses"][1]["v"], 17)

    def test_internet_down_says_kjv_still_works(self):
        self.f.web_down = True
        code, p = self.look("John 3:16", "ESV")
        self.assertEqual(code, 404)
        self.assertIn("KJV and WEBBE work with no internet", p["error"])
        self.assertEqual(self.look("John 3:16", "KJV")[0], 200)

    def test_add_puts_a_line_in_the_playlist_after_bible_scripture(self):
        code, d = self.add()
        self.assertEqual(code, 200, d)
        names = [x["id"]["name"] for x in self.f.items["PL18"]]
        self.assertEqual(names, ["Welcome", "Bible Scripture", "Scripture: John 3:16–17 (NIV)", "Washed"])
        new = self.f.items["PL18"][2]
        self.assertEqual((new["type"], new["id"]["index"]), ("header", 2))
        self.assertEqual(self.f.items["PL18"][3]["presentation_info"], {"presentation_uuid": "P2"})  # the rest kept as they were
        self.assertEqual(d["added"]["look"]["name"], "Scripture - Full screen")
        self.assertEqual(d["passages"][0]["slide_count"], 2)

    def test_the_playlist_items_for_choosing_where(self):
        code, d = self.call("/api/scripture/playlist?id=PL18")
        self.assertEqual([x["name"] for x in d["items"]], ["Welcome", "Bible Scripture", "Washed"])

    def test_add_at_the_start_or_end(self):
        self.add(after=-1)
        self.assertEqual(self.f.items["PL18"][0]["id"]["name"], "Scripture: John 3:16–17 (NIV)")
        self.call("/api/scripture/add", "POST", {"ref": "Psalm 23:1", "v": "KJV", "look": "M-Lowerthird", "playlist": "PL18"})
        self.assertEqual(self.f.items["PL18"][-1]["id"]["name"], "Scripture: Psalm 23:1 (KJV)")

    def test_add_checks_propresenter_kept_it(self):
        self.f.ignore_put = True
        code, d = self.add()
        self.assertEqual(code, 409)
        self.assertIn("didn’t keep", d["error"])
        self.assertEqual(self.call("/api/scripture/list")[1]["passages"], [])

    def test_nothing_goes_on_screen_until_show(self):
        self.add()
        self.assertEqual([c for c in self.f.calls if c[0] == "POST"], [])

    def test_show_each_slide_then_hide(self):
        pid = self.add()[1]["added"]["id"]
        code, d = self.call("/api/scripture/show", "POST", {"id": pid, "slide": 0})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.f.calls[-1], ("POST", "/v1/message/M-Fullscreen/trigger", [
            {"name": "Reference", "text": {"text": "John 3:16 (NIV)"}},
            {"name": "Text", "text": {"text": "¹⁶ For God so loved the world that he gave his one and only Son, that whoever believes in him shall not perish but have eternal life."}}]))
        self.call("/api/scripture/show", "POST", {"id": pid, "slide": 1})
        self.assertEqual(self.f.calls[-1][2][0]["text"]["text"], "John 3:17 (NIV)")
        self.assertEqual(self.call("/api/scripture/list")[1]["showing"]["slide"], 1)
        self.assertEqual(self.call("/api/scripture/show", "POST", {"id": pid, "slide": 5})[0], 409)
        self.call("/api/scripture/hide", "POST", {})
        self.assertEqual(self.f.calls[-1], ("GET", "/v1/message/M-Fullscreen/clear", None))
        self.assertIsNone(self.call("/api/scripture/list")[1]["showing"])

    def test_remove_from_the_list(self):
        pid = self.add()[1]["added"]["id"]
        code, d = self.call("/api/scripture/" + pid, "DELETE")
        self.assertEqual((code, d["passages"]), (200, []))
        self.assertEqual(self.call("/api/scripture/" + pid, "DELETE")[0], 404)

    def test_kept_after_a_restart(self):
        self.add()
        again = sc.Scripture(self.cfg["data_dir"], self.cfg, s.log)
        self.assertEqual(again.list()[0]["reference"], "John 3:16–17")

    def test_who_can_do_what(self):
        self.assertEqual(self.look("John 3:16", "KJV")[0], 200)
        self.assertEqual(self.call("/api/scripture/passage?ref=John+3:16&v=KJV", who="Viewer")[0], 200)  # look only
        self.assertEqual(self.call("/api/scripture/add", "POST", {"ref": "John 3:16", "v": "KJV", "look": "M-Fullscreen", "playlist": "PL18"}, who="Viewer")[0], 403)
        self.assertEqual(self.call("/api/scripture", who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("/api/scripture", who=None)[0], 401)

    def test_propresenter_down(self):
        self.f.pp_down = True
        code, d = self.call("/api/scripture")
        self.assertEqual((code, d["pp"]), (200, "unavailable"))
        self.assertIn("ProPresenter didn’t answer", d["pp_reason"])
        self.assertEqual(self.look("John 3:16", "KJV")[0], 200)  # the preview still works
        self.assertEqual(self.add(ref="John 3:16", v="KJV")[0], 502)

    def test_no_scripture_message_says_so(self):
        self.f.messages = ["Name tag"]
        d = self.call("/api/scripture")[1]
        self.assertEqual(d["looks"], [])
        self.assertIn("starts with “Scripture”", d["pp_reason"])


if __name__ == "__main__":
    unittest.main()
