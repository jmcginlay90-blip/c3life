"""Tests for MIDI buttons (midi_buttons.py, through midi_in): learn, off until switched on, a press does one thing.
Run: python3 -m unittest -v test_midi_buttons.py"""

import logging
import shutil
import tempfile
import unittest

import midi_buttons as mb


class Buttons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.done = []
        self.b = mb.Buttons(self.tmp, logging.getLogger("test"), actions={
            "pp_next": lambda arg, who: self.done.append(("next", who)),
            "macro": lambda arg, who: "Message is still running. Stop it first." if arg == "busy" else self.done.append(("macro", arg))})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_learn_then_off_then_on(self):
        self.b.learn()
        self.assertEqual(self.b.handle(0x90, [36, 100]), ("Note 36", "learned for the new button", False))
        self.assertEqual(self.b.view()["learned"], "note:36")
        self.assertIsNone(self.b.save({"on": False, "maps": [{"msg": "note:36", "label": "Pad 1", "do": "pp_next"},
                                                             {"msg": "cc:64", "label": "Foot", "do": "macro", "arg": "m1"}]}, "Admin"))
        self.assertIn("not sent: MIDI buttons are off", self.b.handle(0x90, [36, 100])[1])
        self.assertEqual(self.done, [])
        self.b.d["on"] = True
        self.assertTrue(self.b.handle(0x90, [36, 100])[2])
        self.assertIsNone(self.b.handle(0x90, [36, 0]))  # the release isn't a press
        self.assertTrue(self.b.handle(0xB0, [64, 127])[2])
        self.assertIsNone(self.b.handle(0xB0, [64, 127]))  # held down: once
        self.assertIsNone(self.b.handle(0xB0, [64, 0]))
        self.assertEqual(self.done, [("next", "MIDI button"), ("macro", "m1")])
        self.assertIsNone(self.b.handle(0x90, [50, 100]))  # not a button: left for Lights

    def test_bad_maps(self):
        self.assertIn("two buttons", self.b.save({"on": True, "maps": [{"msg": "note:1", "do": "pp_next"}, {"msg": "note:1", "do": "marker"}]}, "A"))
        self.assertIn("which macro", self.b.save({"on": True, "maps": [{"msg": "note:1", "do": "macro"}]}, "A"))
        self.assertIn("Learn", self.b.save({"on": True, "maps": [{"msg": "note:200", "do": "marker"}]}, "A"))

    def test_through_the_midi_session(self):
        import midi_in
        L = type("L", (), {"show": {"cues": []}})()
        m = midi_in.Midi(self.tmp, {}, logging.getLogger("test"), L)
        m.buttons = self.b
        self.b.save({"on": True, "maps": [{"msg": "note:36", "do": "pp_next"}]}, "A")
        m.handle(0x90, [36, 100], who="MacBook Pro")
        h = m.view()["heard"][0]
        self.assertEqual((h["from"], h["msg"], h["did"], h["sent"]), ("MacBook Pro", "Note 36", "pp_next", True))


if __name__ == "__main__":
    unittest.main()
