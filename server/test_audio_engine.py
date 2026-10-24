"""Tests for the audio engine (audio_engine.py) with made-up tones in place of Dante Virtual Soundcard.
Run: python3 -m unittest -v test_audio_engine.py"""

import array
import logging
import re
import shutil
import subprocess
import time
import unittest

import audio_engine as ae

LOG = logging.getLogger("test")


def wait(cond, secs=5):
    t = time.time() + secs
    while time.time() < t:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Engine(unittest.TestCase):
    def setUp(self):
        self.e = ae.Engine({"audio_engine": "fake", "engine_port": 0, "engine_channels": 64}, LOG)
        self.e.start()
        self.assertTrue(wait(lambda: self.e.status == "running"))

    def tearDown(self):
        self.e.stop()

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_ffmpeg_hears_one_channel_by_number(self):
        # DVS 40 is c39: a 4 kHz tone at -20 dBFS; DVS 39 is a different tone. Listen and the RTA pick channels like this.
        for c, f in ((39, 4000), (38, 3900)):
            p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "info"] + self.e.input_args() +
                               ["-t", "1", "-af", "pan=mono|c0=c%d,volumedetect" % c, "-f", "null", "-"], capture_output=True, text=True, timeout=30)
            peak = float(re.search(r"max_volume: (-?[\d.]+) dB", p.stderr).group(1))
            self.assertAlmostEqual(peak, -20.0, delta=0.5)
        self.assertTrue(wait(lambda: self.e.view()["listeners"] == 0))  # ffmpeg gone: let go of it

    def test_every_block_reaches_the_recorder_and_playback_goes_out(self):
        got = []
        self.e.subscribe(lambda data, first, ch: got.append((len(data), first, ch)))
        self.assertTrue(wait(lambda: len(got) >= 3))
        n, first, ch = got[0]
        self.assertEqual((n, ch), (ae.BLOCK * 4 * 64, 64))
        self.assertEqual(got[1][1] - got[0][1], ae.BLOCK)  # frame numbers run on
        one = array.array("i", [12345]) * (ae.BLOCK * 64)
        self.e.play(lambda frames: one.tobytes())
        self.assertTrue(wait(lambda: self.e.backend.written and self.e.backend.written[-1] == one.tobytes()))
        self.e.play(None)
        self.assertTrue(wait(lambda: self.e.backend.written[-1] == bytes(ae.BLOCK * 4 * 64)))  # silence again
        self.assertTrue(wait(lambda: self.e.peaks()[0] > 0.09))  # meters: -20 dBFS is 0.1
        self.assertAlmostEqual(self.e.peaks()[0], 0.1, delta=0.01)


class NoAsio(unittest.TestCase):
    def test_says_why_and_keeps_trying(self):
        class Broken:
            pass
        e = ae.Engine({"audio_engine": "asio", "engine_port": 0}, LOG)
        e._open = lambda: (_ for _ in ()).throw(OSError("No ASIO device called Dante."))
        e.start()
        self.assertTrue(wait(lambda: e.status == "error"))
        self.assertIn("No ASIO device", e.view()["reason"])
        e.stop()
        off = ae.Engine({}, LOG)
        off.start()
        self.assertEqual((off.on, off.view()["status"]), (False, "off"))


if __name__ == "__main__":
    unittest.main()
