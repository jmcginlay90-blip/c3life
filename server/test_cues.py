"""Tests for slide cues (audio changes when a ProPresenter slide goes live). Run: python3 -m unittest -v test_cues.py"""

import copy
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import c3life_server as s
import cues as cm


class Unit(unittest.TestCase):
    def test_the_starting_cues_are_valid(self):
        self.assertEqual(cm.valid(cm.DEFAULT), "")
        self.assertEqual(cm.DEFAULT["default_how"], "count5")

    def test_bad_cues_say_why(self):
        def bad(change):
            d = copy.deepcopy(cm.DEFAULT)
            change(d)
            return cm.valid(d)
        self.assertIn("name", bad(lambda d: d["cues"][0].update(name="")))
        self.assertIn("which presentation", bad(lambda d: d["cues"][0]["when"].update(text=" ")))
        self.assertIn("between -90 and +10", bad(lambda d: d["cues"][0]["do"][1].update(db=20)))
        self.assertIn("fade", bad(lambda d: d["cues"][2]["do"][0].update(fade=4)))  # a fade-out too
        self.assertIn("fade", bad(lambda d: d["cues"][0]["do"][1].update(fade=7)))
        self.assertIn("at least one", bad(lambda d: d["cues"][0].update(do=[])))
        self.assertIn("own id", bad(lambda d: d["cues"][1].update(id="c1")))
        self.assertIn("how it runs", bad(lambda d: d["cues"][0].update(how="later")))
        self.assertIn("Settings", bad(lambda d: d.update(default_how="now")))  # the default is never "straight away"

    def test_matching_a_slide(self):
        c = cm.DEFAULT["cues"]
        self.assertTrue(cm.Cues.matches(c[0], {"presentation": "message", "group": "", "label": ""}))
        self.assertTrue(cm.Cues.matches(c[1], {"presentation": "Washed", "group": "Bridge"}))
        self.assertFalse(cm.Cues.matches(c[1], {"presentation": "Washed", "group": "Chorus"}))
        off = dict(c[1], on=False)
        self.assertFalse(cm.Cues.matches(off, {"group": "Bridge"}))


class Api(unittest.TestCase):
    def setUp(self):
        import test_scripture as ts
        self.f = ts.Fake()  # ProPresenter's playlist: Welcome, and Washed with a Bridge
        self.pp = ts.fake_server(self.f)
        self.tmp = tempfile.mkdtemp()
        app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(app, "project"))
        open(os.path.join(app, "player.html"), "w").write("<!doctype html>")
        cfg = s.load_config(None)
        cfg.update(port=0, bind="127.0.0.1", app_dir=app, data_dir=os.path.join(self.tmp, "data"), propresenter="http://127.0.0.1:%d" % self.pp.server_address[1])
        cfg["_nets"] = [s.ipaddress.ip_network("127.0.0.0/8")]
        self.cfg = cfg
        self.httpd = s.serve(cfg)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {n: self.login(n) for n in ("Admin", "FOH sound", "Viewer", "Keys (musician)")}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.pp.shutdown()
        self.pp.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cues(self):
        d = self.call()[1]
        d["cues"][0]["when"]["text"] = "Washed"  # this playlist has no Message
        return d

    def login(self, name):
        body = {"name": name, "password": "c3life-admin"} if name == "Admin" else {"name": name, "pin": "" if name == "Viewer" else "1234"}
        req = urllib.request.Request("http://127.0.0.1:%d/api/login" % self.port, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["token"]

    def call(self, method="GET", body=None, who="FOH sound"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d/api/cues" % self.port, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.tok[who])
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_everyone_with_the_mixer_sees_the_same_cues(self):
        code, d = self.call()
        self.assertEqual(code, 200)
        self.assertEqual([c["name"] for c in d["cues"]][0], "Pastor mic on for the Message")

    def test_foh_saves_and_it_is_kept(self):
        d = self.cues()
        d["cues"][0]["how"] = "count10"
        d["cues"].append({"id": "c9", "name": "Band down for prayer", "on": True, "when": {"kind": "label", "text": "Big build"}, "how": "ask",
                          "do": [{"ch": 2, "act": "fader", "db": -20, "fade": 5}, {"ch": 3, "act": "off"}]})
        self.assertEqual(self.call("PUT", d)[0], 200)
        self.assertEqual(cm.Cues(self.cfg["data_dir"]).get()["cues"][-1]["name"], "Band down for prayer")
        self.assertEqual(self.call(who="Admin")[1]["cues"][0]["how"], "count10")

    def test_only_an_admin_can_choose_straight_away(self):
        d = self.cues()
        d["cues"][1]["how"] = "now"
        code, j = self.call("PUT", d)
        self.assertEqual(code, 403)
        self.assertIn("Only an Admin", j["error"])
        self.assertEqual(self.call("PUT", d, who="Admin")[0], 200)
        d = self.call()[1]
        d["cues"][0]["name"] = "Pastor mic on"  # (saved with Washed above)
        self.assertEqual(self.call("PUT", d)[0], 200)  # FOH can still change the others once an Admin has set one

    def test_a_crossfade_is_kept(self):
        d = self.cues()
        d["cues"][2]["do"] = [{"ch": 10, "act": "off", "fade": 5}, {"ch": 15, "act": "on", "fade": 5}]
        self.assertEqual(self.call("PUT", d)[0], 200)
        self.assertEqual(self.call()[1]["cues"][2]["do"][1], {"ch": 15, "act": "on", "fade": 5})

    def test_nothing_saves_if_the_run_check_fails(self):
        d = self.call()[1]  # the Message cue: no Message in this playlist
        d["cues"][1]["name"] = "Changed"
        code, j = self.call("PUT", d)
        self.assertEqual(code, 409)
        self.assertIn("Run check found problems, so nothing was saved", j["error"])
        self.assertTrue(any("No presentation called" in x["text"] for x in j["check"]["lines"]))
        self.assertEqual(self.call()[1]["cues"][1]["name"], "Lead Vox up for the bridge")  # unchanged

    def test_switching_the_problem_cue_off_lets_it_save(self):
        d = self.call()[1]
        d["cues"][0]["on"] = False
        self.assertEqual(self.call("PUT", d)[0], 200)

    def test_propresenter_down_means_no_saving(self):
        self.f.pp_down = True
        code, j = self.call("PUT", self.cues())
        self.assertEqual(code, 409)
        self.assertFalse(next(x for x in j["check"]["lines"] if x["what"] == "ProPresenter")["ok"])

    def test_checking_changes_before_saving(self):
        d = self.call()[1]
        req = urllib.request.Request("http://127.0.0.1:%d/api/cues/check" % self.port, data=json.dumps(d).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.tok["FOH sound"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read())
        self.assertFalse(j["ok_all"])
        d["cues"][0]["when"]["text"] = "Welcome"
        req = urllib.request.Request("http://127.0.0.1:%d/api/cues/check" % self.port, data=json.dumps(d).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.tok["FOH sound"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok_all"])

    def test_bad_cues_are_refused(self):
        d = self.call()[1]
        d["cues"][0]["do"][1]["db"] = 40
        code, j = self.call("PUT", d)
        self.assertEqual(code, 400)
        self.assertIn("between -90 and +10", j["error"])

    def test_who_can_do_what(self):
        self.assertEqual(self.call(who="Viewer")[0], 403)  # Viewer has neither Mixer nor Macros
        self.assertEqual(self.call(who="Keys (musician)")[0], 403)
        self.assertEqual(self.call("PUT", self.call()[1], who="Viewer")[0], 403)


if __name__ == "__main__":
    unittest.main()


class RunCheck(unittest.TestCase):
    """The Run check reads ProPresenter's playlist (a fake here) and says which cues will work."""
    def setUp(self):
        import test_scripture as ts
        self.f = ts.Fake()
        self.srv = ts.fake_server(self.f)
        self.cfg = {"propresenter": "http://127.0.0.1:%d" % self.srv.server_address[1]}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def lines(self, d=None):
        r = cm.check(d or cm.DEFAULT, self.cfg)
        return r, {x["what"]: x for x in r["lines"]}

    def test_what_will_run_and_what_wont(self):
        r, by = self.lines()
        self.assertTrue(by["ProPresenter"]["ok"])
        self.assertIn("2 presentations", by["ProPresenter"]["text"])
        self.assertIsNone(by["Wing"]["ok"])  # said honestly: not connected yet
        self.assertTrue(by["Lead Vox up for the bridge"]["ok"])
        self.assertIn("Bridge (Washed)", by["Lead Vox up for the bridge"]["text"])
        self.assertTrue(by["Walk-in music off at the Welcome"]["ok"])
        self.assertFalse(by["Pastor mic on for the Message"]["ok"])  # no Message in this playlist
        self.assertIn("No presentation called “Message”", by["Pastor mic on for the Message"]["text"])
        self.assertFalse(r["ok_all"])

    def test_a_spelling_mistake_gets_a_suggestion(self):
        d = copy.deepcopy(cm.DEFAULT)
        d["cues"][1]["when"]["text"] = "Brige"
        _, by = self.lines(d)
        self.assertIn("Did you mean Bridge (Washed)?", by["Lead Vox up for the bridge"]["text"])

    def test_slide_labels(self):
        d = copy.deepcopy(cm.DEFAULT)
        d["cues"][0]["when"] = {"kind": "label", "text": "big build"}
        _, by = self.lines(d)
        self.assertTrue(by["Pastor mic on for the Message"]["ok"])

    def test_off_cues_and_switched_off(self):
        d = copy.deepcopy(cm.DEFAULT)
        d["enabled"] = False
        d["cues"][0]["on"] = False
        r, by = self.lines(d)
        self.assertIsNone(by["Slide cues"]["ok"])  # off isn't a problem: nothing will run
        self.assertIsNone(by["Pastor mic on for the Message"]["ok"])

    def test_propresenter_down(self):
        self.f.pp_down = True
        r, by = self.lines()
        self.assertFalse(by["ProPresenter"]["ok"])
        self.assertIn("Can’t check without ProPresenter", by["Lead Vox up for the bridge"]["text"])
