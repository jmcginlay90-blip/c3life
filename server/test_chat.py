"""Tests for team chat (chat.py). Run: python3 -m unittest -v test_chat.py"""

import logging
import shutil
import tempfile
import unittest

import chat as cm
import propresenter as ppm
from test_propresenter import FakePP, fake_server


class Chat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakePP()
        self.srv = fake_server(self.fake)
        self.pp = ppm.ProPresenter({"propresenter": "http://127.0.0.1:%d" % self.srv.server_address[1]}, None)
        self.c = cm.Chat(self.tmp, logging.getLogger("test"), self.pp)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_messages_kept_and_the_stage_display(self):
        m, err = self.c.send("FOH sound", "FOH sound", "Stand by", 0)
        self.assertEqual((err, m["id"], m["stage"]), (None, 1, False))
        m, err = self.c.send("ProPresenter", "ProPresenter", "Extra chorus", 2)
        self.assertEqual((err, m["stage"], self.fake.stage), (None, True, "Extra chorus"))
        self.assertEqual([x["text"] for x in self.c.since(1)], ["Extra chorus"])
        self.assertEqual(len(cm.Chat(self.tmp, logging.getLogger("test")).since(0)), 2)  # kept after a restart
        self.assertIn("1 to 500", self.c.send("FOH sound", "FOH sound", "   ")[1])
        self.c.pp = ppm.ProPresenter({"propresenter": "http://127.0.0.1:1"}, None)
        m, err = self.c.send("FOH sound", "FOH sound", "Go", 0, stage=True)
        self.assertEqual(m["stage"], False)
        self.assertIn("not to the stage display", err)


if __name__ == "__main__":
    unittest.main()
