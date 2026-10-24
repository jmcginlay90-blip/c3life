"""Tests for the hardware check, against the fake Wing and fake Tio: it reads them and changes nothing.
Run: python3 -m unittest -v test_hwcheck.py"""

import shutil
import tempfile
import time
import unittest

import hwcheck as hw
import wing as wm
from test_tio import FakeTio
from test_wing import FakeWing


class HardwareCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        hw.WATCH = 4

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_wing_is_read_and_nothing_is_changed(self):
        fw = FakeWing()
        for i in range(1, 65):
            fw.vals.update({"/io/out/CRD/%d/grp" % i: "OFF", "/io/out/CRD/%d/in" % i: 0})
        fw.vals.update({"/io/out/CRD/33/grp": "MAIN", "/io/out/CRD/33/in": 1})
        hw.WATCH = 8  # gain and 48 V are read on the second round
        try:
            r = hw.Report()
            hw.check_wing(r, "127.0.0.1", self.tmp, {"wing_osc_port": fw.osc_port, "wing_native_port": fw.native_port})
            text = "\n".join(r.lines)
            self.assertIn("OK    Answers.", text)
            self.assertIn("Faders read: 40 of 40 channels", text)
            self.assertRegex(text, r"Ch 01 Lead Vox\s+fader \+0\.0 dB")
            self.assertIn("Ch 15 Pastor HH", text)
            self.assertIn("OK    Preamps read (gain, 48 V)", text)
            self.assertIn("Input LCL/1    gain 20.0 dB  48V off", text)
            self.assertIn("Nothing changed on the Wing: 0 changes refused", text)
            self.assertIn("Dante card outputs read: 64 of 64", text)
            self.assertIn("Card out 33  sends MAIN 1", text)
            self.assertEqual(fw.sets, [])  # the fake Wing was sent no value at all
        finally:
            fw.close()

    def test_a_change_is_refused_even_if_asked(self):
        w = wm.Wing(self.tmp, {"wing_ip": "127.0.0.1", "wing_osc_port": 9}, None)
        hw.guard_wing(w)
        self.assertFalse(w.send(wm.osc("/ch/1/fdr", 0.0)))
        self.assertEqual(w.refused, ["/ch/1/fdr"])
        self.assertIn("doesn't change", w.set("ch", 1, "fdr", 0.0))

    def test_the_tio_is_read_and_nothing_is_changed(self):
        ft = FakeTio()
        try:
            r = hw.Report()
            hw.check_tio(r, "127.0.0.1", ft.port, self.tmp)
            text = "\n".join(r.lines)
            self.assertIn("Inputs read: 16 of 16", text)
            self.assertIn("Input  1 gain 20 dB  48V off  low cut off at 80 Hz", text)
            self.assertIn("Nothing changed on the Tio: 0 changes refused", text)
            self.assertEqual(ft.sets, [])
        finally:
            ft.close()

    def test_nothing_there_says_so(self):
        r = hw.Report()
        hw.check_tio(r, "127.0.0.1", 9, self.tmp)
        self.assertEqual(r.problems, 1)
        self.assertIn("No answer on TCP 9", r.lines[-1])


class FindTalkback(unittest.TestCase):
    def test_prints_what_changed_while_tb_was_pressed(self):
        import threading
        from test_wing import FakeWing
        f = FakeWing()
        try:
            f.vals["/cfg/talk/A/$on"] = 0
            def press():
                def go():
                    for v in (1, 0, 1, 0):
                        time.sleep(0.3)
                        f.change("/cfg/talk/A/$on", v)
                threading.Thread(target=go, daemon=True).start()
            r = hw.Report()
            got = hw.find_talkback(r, "127.0.0.1", secs=2, port=f.osc_port, say_press=press)
            self.assertEqual(got, ["/cfg/talk/A/$on"])
            self.assertTrue(any("/cfg/talk/A/$on" in x and "1 → 0 → 1 → 0" in x for x in r.lines))
            self.assertFalse(f.sets)  # nothing was set on the Wing
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
