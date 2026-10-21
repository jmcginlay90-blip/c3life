"""The RTA, heard by the C3 Life server: the room mic and the desk reference, from the channels in rta_inputs.

While someone has the RTA open (or the RTA bar on any screen), ffmpeg reads those channels from Dante Virtual Soundcard
(or AES67, or the Wing's USB audio), the same way Listen does, and this works out:
  - the spectrum in 120 bands, 1/12 octave apart from 20 Hz (the RTA screen's grid), for the room mic and the reference;
  - the room level in dB(A) and dB(Z), and its averages (Leq) over 1 minute to 8 hours, kept on the server so every
    device shows the same history.
It stops 30 seconds after the last screen stops asking.

Levels: the mic's are dB SPL once it's calibrated (Calibrate: a 94 dB calibrator, or match a sound level meter). Until
then they're an estimate (0 dBFS = 120 dB SPL). The desk reference is digital, so it's moved up or down to sit on the
room's curve (matched between 250 Hz and 4 kHz); its shape is what matters.

Plain Python (no numpy needed): a 4096-point FFT five times a second, two channels at a time.

data/rta_live.json: {"offset": 120.0, "calibrated": "2026-10-21 10:30", "how": "94 dB calibrator"}
"""

import array
import cmath
import collections
import json
import math
import os
import subprocess
import threading
import time

import ringout
import rta_inputs as rta_inputs_mod

RATE = 48000
N = 4096
HOP = RATE // 5          # a new spectrum five times a second
BANDS = 120              # 1/12 octave from 20 Hz
FREQS = [20 * 2 ** (i / 12) for i in range(BANDS)]
IDLE = 30                # seconds after the last ask
DEFAULT_OFFSET = 120.0   # dB SPL at 0 dBFS, until it's calibrated
LEQS = (("1m", 60), ("15m", 900), ("30m", 1800), ("1h", 3600), ("8h", 28800))
SILENT = -100.0          # dBFS: nothing there


def a_weight(f):
    """dB(A) correction at f Hz (IEC 61672)."""
    f2 = f * f
    ra = (12194.0 ** 2 * f2 * f2) / ((f2 + 20.6 ** 2) * math.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
    return 20 * math.log10(ra) + 2.0


class Spectrum:
    """Hann-windowed FFT, two real channels at once, into band powers relative to a full-scale sine (0 dBFS)."""

    def __init__(self, n=N, rate=RATE):
        self.n, self.rate = n, rate
        self.win = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
        self.norm = 2.0 / (n * sum(w * w for w in self.win)) / 0.5   # one-sided power, so a full-scale sine is 1.0
        bits = n.bit_length() - 1
        self.rev = [int(format(i, "0%db" % bits)[::-1], 2) for i in range(n)]
        self.tw = {}
        size = 2
        while size <= n:
            self.tw[size] = [cmath.exp(-2j * math.pi * k / size) for k in range(size // 2)]
            size *= 2
        df = rate / n
        self.df = df
        self.bands = []  # (first bin, last bin, centre bin, width in bins)
        for f in FREQS:
            lo, hi = f * 2 ** (-1 / 24), f * 2 ** (1 / 24)
            self.bands.append((int(math.ceil(lo / df)), int(math.floor(hi / df)), f / df, (hi - lo) / df))
        self.aw = [10 ** (a_weight(f) / 10) for f in FREQS]

    def fft(self, a):
        n = self.n
        a = [a[r] for r in self.rev]
        size = 2
        while size <= n:
            half, w = size // 2, self.tw[size]
            for st in range(0, n, size):
                for k in range(half):
                    i = st + k
                    v = a[i + half] * w[k]
                    u = a[i]
                    a[i] = u + v
                    a[i + half] = u - v
            size *= 2
        return a

    def powers(self, x, y=None):
        """Per-bin power for one or two channels (lists of n floats): ([...], [...] or None)."""
        n, w = self.n, self.win
        z = self.fft([complex(x[i] * w[i], (y[i] * w[i]) if y is not None else 0.0) for i in range(n)])
        px, py = [0.0] * (n // 2), ([0.0] * (n // 2) if y is not None else None)
        for k in range(1, n // 2):
            a, b = z[k], z[n - k].conjugate()
            xk = (a + b) * 0.5
            px[k] = (xk.real * xk.real + xk.imag * xk.imag) * self.norm
            if py is not None:
                yk = (a - b) * -0.5j
                py[k] = (yk.real * yk.real + yk.imag * yk.imag) * self.norm
        return px, py

    def to_bands(self, p):
        """Bin powers -> 120 band powers. A band narrower than a bin (the low end) takes its share of the nearest bins."""
        out = []
        top = len(p) - 1
        for k0, k1, c, width in self.bands:
            if k1 - k0 >= 1:
                out.append(sum(p[max(1, k0):min(top, k1) + 1]))
            else:
                i = min(top - 1, max(1, int(c)))
                f = min(1.0, max(0.0, c - i))
                out.append((p[i] * (1 - f) + p[i + 1] * f) * width)
        return out


def db(p):
    return 10 * math.log10(p) if p > 1e-14 else -140.0


class Capture:
    """One ffmpeg, reading some channels of one sound device as 32-bit floats."""

    def __init__(self, cmd, nch, log):
        self.cmd, self.nch, self.log = cmd, nch, log
        self.proc = None
        self.err = ""

    def start(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=flags)
        threading.Thread(target=self._errors, daemon=True).start()

    def _errors(self):
        for line in self.proc.stderr:
            self.err = line.decode("utf-8", "replace").strip()[:200]

    def read(self, frames):
        """The next `frames` samples of every channel, interleaved; None when ffmpeg has stopped."""
        want = frames * self.nch * 4
        buf = b""
        while len(buf) < want:
            d = self.proc.stdout.read(want - len(buf))
            if not d:
                return None
            buf += d
        a = array.array("f")
        a.frombytes(buf)
        return a

    def stop(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass


class Analyser:
    def __init__(self, data_dir, cfg, log, inputs):
        self.path = os.path.join(data_dir, "rta_live.json")
        self.cfg, self.log, self.inputs = cfg, log, inputs
        self.lock = threading.Lock()
        self.cal = {"offset": DEFAULT_OFFSET, "calibrated": "", "how": ""}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("offset"), (int, float)) and 60 <= d["offset"] <= 180:
                self.cal = {"offset": float(d["offset"]), "calibrated": str(d.get("calibrated", ""))[:40], "how": str(d.get("how", ""))[:60]}
        except (OSError, ValueError, AttributeError):
            pass
        self.spec = Spectrum()
        self.asked = 0.0
        self.thread = None
        self.stop_ev = threading.Event()
        self.status, self.reason = "off", ""
        self.mic = self.ref = None      # band powers (fast)
        self.mic_db = None              # (dB(A), dB(Z)) in dBFS, fast
        self.secs = collections.deque(maxlen=28800)   # (time, dB(A) in dBFS) once a second
        self._acc = []
        self.t = 0.0
        self.source = ""
        self.caps = {}
        self.ring = ringout.Watch(data_dir, RATE / N, log)  # Ring-Out: the feedback watch on the room mic

    # ---- what to listen to ----------------------------------------------------------------------

    def device(self, via):
        """ffmpeg's input arguments for a way in."""
        if via == "usb":
            return self.cfg.get("rta_usb_input") or ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + self.cfg.get("rta_usb_device", "WING USB Audio")]
        # DVS, and AES67 (which Dante Virtual Soundcard receives too)
        return self.cfg.get("listen_input") or ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + self.cfg.get("listen_device", "Dante Virtual Soundcard (1-16)")]

    def plan(self):
        """{via: [channel numbers]}, and which of those are the mic, the mains and the subs."""
        d = self.inputs.get()
        mic, ref = d["mic"], d["ref"]
        roles = {"mic": [(mic["via"], c) for c in ([mic["ch"], mic["ch2"]] if mic.get("stereo") else [mic["ch"]])],
                 "mains": [(ref["via"], c) for c in ([ref["ch"], ref["ch2"]] if ref.get("stereo") else [ref["ch"]])],
                 "subs": [(ref["via"], c) for c in ([ref["subs"]["ch"], ref["subs"]["ch2"]] if (ref.get("subs") or {}).get("on") else [])]}
        per = collections.OrderedDict()
        for key in ("mic", "mains", "subs"):
            for via, c in roles[key]:
                per.setdefault(via, [])
                if c not in per[via]:
                    per[via].append(c)
        src = "%s %d" % ({"dvs": "DVS", "aes67": "AES67", "usb": "Wing USB"}[mic["via"]], mic["ch"]) + (" and %d" % mic["ch2"] if mic.get("stereo") else "")
        return per, roles, src

    def command(self, via, chans):
        pan = "pan=%dc|" % len(chans) + "|".join("c%d=c%d" % (i, c - 1) for i, c in enumerate(chans))
        return [self.cfg.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error"] + self.device(via) + \
            ["-af", pan, "-ar", str(RATE), "-f", "f32le", "-"]

    # ---- running while someone looks ------------------------------------------------------------

    def ask(self):
        """A screen wants the RTA: keep it running, starting it if it isn't."""
        with self.lock:
            self.asked = time.time()
            if self.thread and self.thread.is_alive():
                return
            self.stop_ev.clear()
            self.status, self.reason = "starting", ""
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def run(self):
        while not self.stop_ev.is_set():
            per, roles, src = self.plan()
            self.source = src
            caps = self.caps = {}
            try:
                for via, chans in per.items():
                    caps[via] = Capture(self.command(via, chans), len(chans), self.log)
                    caps[via].start()
            except OSError as e:
                with self.lock:
                    self.status, self.reason = "error", "ffmpeg isn't installed (%s), so the RTA can't hear the room." % e
                for c in caps.values():
                    c.stop()
                self.stop_ev.wait(10)
                if time.time() - self.asked > IDLE:
                    break
                continue
            want = self.inputs.get()
            stopped = None
            while not self.stop_ev.is_set():
                if time.time() - self.asked > IDLE:
                    self.stop_ev.set()
                    break
                if self.inputs.get() != want:  # someone chose other channels: start again with those
                    break
                got = {}
                for via, cap in caps.items():
                    a = cap.read(HOP)
                    if a is None:
                        stopped = cap
                        break
                    got[via] = a
                if stopped:
                    break
                self.analyse(got, per, roles)
            for c in caps.values():
                c.stop()
            if stopped and not self.stop_ev.is_set():
                with self.lock:
                    self.status = "error"
                    self.reason = "The sound stopped" + (": " + stopped.err if stopped.err else ". Is Dante Virtual Soundcard running?")
                self.log.warning("rta: %s", self.reason)
                self.stop_ev.wait(5)
        with self.lock:
            self.status = "off"
            self.mic = self.ref = None

    def analyse(self, got, per, roles):
        sp = self.spec

        def chan(via, c):
            a, k = got[via], len(per[via])
            i = per[via].index(c)
            return a[len(a) - sp.n * k + i::k].tolist()

        res = {}
        for key, members in roles.items():
            sigs = [chan(v, c) for v, c in members]
            bins = []
            for j in range(0, len(sigs), 2):
                px, py = sp.powers(sigs[j], sigs[j + 1] if j + 1 < len(sigs) else None)
                bins.append(px)
                if py is not None:
                    bins.append(py)
            if bins:
                avg = [sum(b[k] for b in bins) / len(bins) for k in range(len(bins[0]))]  # left and right: the mean power
                res[key] = sp.to_bands(avg)
                if key == "mic":
                    self.ring.feed(avg)  # feedback is looked for on the bins, not the bands: a ringing note is narrow
        mic = res["mic"]
        ref = res.get("mains")
        if ref and res.get("subs"):
            ref = [m + s for m, s in zip(ref, res["subs"])]
        za = sum(p * w for p, w in zip(mic, sp.aw))
        zz = sum(mic)
        now = time.time()
        with self.lock:
            k = 0.6  # roughly the meters' Fast (125 ms) at five times a second
            self.mic = mic if self.mic is None else [o + (n - o) * k for o, n in zip(self.mic, mic)]
            self.ref = None if ref is None else (ref if self.ref is None or len(self.ref) != len(ref) else [o + (n - o) * k for o, n in zip(self.ref, ref)])
            fast = (db(za), db(zz))
            self.mic_db = fast if self.mic_db is None else tuple(10 * math.log10(max(1e-14, 10 ** (o / 10) + (10 ** (n / 10) - 10 ** (o / 10)) * k)) for o, n in zip(self.mic_db, fast))
            self._acc.append(za)
            if not self.secs or now - self.secs[-1][0] >= 1.0:
                self.secs.append((now, db(sum(self._acc) / len(self._acc))))
                self._acc = []
            self.status = "silent" if fast[1] < SILENT else "live"
            self.reason = "No sound on %s." % self.source if self.status == "silent" else ""
            self.t = now

    def stop(self):
        self.stop_ev.set()
        for c in list(self.caps.values()):
            c.stop()

    # ---- what the screens get -------------------------------------------------------------------

    def leq(self, secs):
        now = time.time()
        v = [x for t, x in self.secs if now - t <= secs]
        return round(10 * math.log10(sum(10 ** (x / 10) for x in v) / len(v)) + self.cal["offset"], 1) if v else None

    def state(self):
        with self.lock:
            off = self.cal["offset"]
            out = {"status": self.status, "reason": self.reason, "source": self.source, "t": round(self.t, 2),
                   "calibrated": bool(self.cal["calibrated"]), "cal": dict(self.cal), "bands": BANDS, "from_hz": 20, "per_octave": 12}
            if self.mic is not None and self.status in ("live", "silent"):
                mic = [round(db(p) + off, 1) for p in self.mic]
                out["mic"] = mic
                out["dba"], out["dbz"] = round(self.mic_db[0] + off, 1), round(self.mic_db[1] + off, 1)
                if self.ref is not None:
                    ref = [db(p) for p in self.ref]
                    lo, hi = 48, 96  # 250 Hz to 4 kHz: move the reference onto the room's curve there
                    m = sum(10 ** (x / 10) for x in mic[lo:hi]) / (hi - lo)
                    r = sum(10 ** (x / 10) for x in ref[lo:hi]) / (hi - lo)
                    shift = (10 * math.log10(m) - 10 * math.log10(r)) if r > 1e-14 and m > 1e-14 else off
                    out["ref"] = [round(x + shift, 1) for x in ref]
            out["leq"] = {k: self.leq(s) for k, s in LEQS}
            ro = self.ring.view(off)
            if self.status != "live":
                ro["ringing"] = []  # nothing heard now, so nothing is ringing now
            out["ringout"] = ro
            out["max"] = round(max(x for _, x in self.secs) + off, 1) if self.secs else None
        return out

    def uncalibrate(self):
        """Back to not calibrated (a new mic, or a different gain): 0 dBFS = 120 dB SPL again."""
        with self.lock:
            self.cal = {"offset": DEFAULT_OFFSET, "calibrated": "", "how": ""}
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.log.info("rta: calibration cleared")

    def calibrate(self, spl, weight="Z"):
        """The level the calibrator (or a sound level meter) says right now -> the mic's offset. -> (ok, message)."""
        with self.lock:
            if self.status != "live" or self.mic_db is None or time.time() - self.t > 2:
                return False, "The RTA isn't hearing the mic yet. Open the RTA, check the sound arrives, then try again."
            now_dbfs = self.mic_db[0 if weight == "A" else 1]
            if now_dbfs < -70:
                return False, "That's very quiet for a calibrator (%.0f dBFS). Is it switched on and on the mic?" % now_dbfs
            off = spl - now_dbfs
            if not 60 <= off <= 180:
                return False, "That doesn't look right (it would need %.0f dB). Check the mic's gain." % off
            how = "%g dB calibrator" % spl if weight == "Z" else "matched a meter at %g dB(A)" % spl
            self.cal = {"offset": round(off, 2), "calibrated": time.strftime("%Y-%m-%d %H:%M"), "how": how}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cal, f)
            os.replace(tmp, self.path)
        self.log.info("rta: mic calibrated, %s (offset %.1f dB)", how, off)
        return True, "Calibrated: %s." % how
