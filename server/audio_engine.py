"""The audio engine: C3 Life's one connection to Dante Virtual Soundcard, in ASIO mode, 64 in and 64 out.

DVS gives Windows only 16 channels in WDM mode; ASIO gives 64, but only one program can use it at a time. So C3
Life opens it once, here, and shares it out:

  - Listen, the RTA and the MD log read it as before through ffmpeg: the engine serves every channel, as raw
    32-bit samples, on a local port (127.0.0.1 only), and `listen_input` points ffmpeg there (input_args()).
    They still pick channels by number (DVS 1-64), so nothing else changes.
  - the recorder is handed every block as it arrives (subscribe()).
  - playback is asked for each block to send out (play()); with nothing playing, DVS sends silence.

config.json: "audio_engine": "asio" (DVS in ASIO mode), "fake" (made-up tones, for tests and the preview) or ""
(off: Listen and the RTA read DVS in WDM mode, 16 channels, as before). "engine_device" (default "Dante"), part of
the ASIO device's name; "engine_rate" 48000; "engine_channels" 64; "engine_port" 47110.

ASIO needs the sounddevice package (PortAudio with ASIO, chosen with SD_ENABLE_ASIO=1); Install-C3Life.ps1
installs it. **Not tried with Dante Virtual Soundcard yet.**
"""

import array
import math
import os
import queue
import socket
import threading
import time

BLOCK = 1024       # frames per read: about 21 ms at 48 kHz
CLIENT_Q = 100     # blocks a listener may fall behind (about 2 s) before it's dropped
FULL = 2 ** 31


class FakeBackend:
    """Made-up sound: channel n is a sine at 100 x n Hz, 20 dB below full scale. What's written out is kept
    (the last few blocks) so tests can check playback. realtime=False runs as fast as it can (tests)."""

    def __init__(self, channels, rate, realtime=True):
        self.ch, self.rate, self.realtime = channels, rate, realtime
        n = rate // 10  # a tenth of a second: every channel's tone fits it exactly
        a = array.array("i", [0]) * (n * channels)
        for c in range(channels):
            f = 100.0 * (c + 1)
            a[c::channels] = array.array("i", (int(0.1 * FULL * math.sin(2 * math.pi * f * i / rate)) for i in range(n)))
        self.loop, self.loop_frames, self.pos = a.tobytes(), n, 0
        self.written = []
        self.t0 = None
        self.name = "Made-up sound (test)"

    def read(self, frames):
        if self.realtime:
            if self.t0 is None:
                self.t0, self.done = time.monotonic(), 0
            self.done += frames
            ahead = self.t0 + self.done / self.rate - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
        fb = 4 * self.ch
        out = bytearray()
        while len(out) < frames * fb:
            take = min(frames * fb - len(out), (self.loop_frames - self.pos) * fb)
            out += self.loop[self.pos * fb:self.pos * fb + take]
            self.pos = (self.pos + take // fb) % self.loop_frames
        return bytes(out)

    def write(self, data):
        self.written.append(data)
        del self.written[:-50]

    def close(self):
        pass


class AsioBackend:
    """Dante Virtual Soundcard through PortAudio's ASIO host API (the sounddevice package)."""

    def __init__(self, channels, rate, want="Dante"):
        os.environ.setdefault("SD_ENABLE_ASIO", "1")
        import sounddevice as sd  # only on the Windows C3 Life server
        dev = None
        for i, d in enumerate(sd.query_devices()):
            api = sd.query_hostapis(d["hostapi"])["name"]
            if "ASIO" in api and want.lower() in d["name"].lower():
                dev = i
                break
        if dev is None:
            names = [d["name"] for d in sd.query_devices() if "ASIO" in sd.query_hostapis(d["hostapi"])["name"]]
            raise OSError("No ASIO device called %s. Is Dante Virtual Soundcard set to ASIO and started? ASIO devices seen: %s"
                          % (want, ", ".join(names) or "none"))
        d = sd.query_devices(dev)
        cin, cout = min(channels, d["max_input_channels"]), min(channels, d["max_output_channels"])
        if cin < 1:
            raise OSError("%s has no inputs. Set Dante Virtual Soundcard's channel count (64 x 64)." % d["name"])
        self.ch, self.out_ch, self.rate, self.name = cin, cout, rate, d["name"]
        self.stream = sd.RawStream(device=(dev, dev), samplerate=rate, channels=(cin, max(cout, 1)), dtype="int32",
                                   blocksize=BLOCK, latency="high")
        self.stream.start()

    def read(self, frames):
        data, _over = self.stream.read(frames)
        return bytes(data)

    def write(self, data):
        self.stream.write(data)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class Engine:
    def __init__(self, cfg, log, backend=None):
        self.cfg, self.log = cfg, log
        self.kind = cfg.get("audio_engine") or ""
        self.rate = int(cfg.get("engine_rate", 48000))
        self.want_ch = int(cfg.get("engine_channels", 64))
        self.port = int(cfg.get("engine_port", 47110))
        self.backend = backend
        self.ch = self.want_ch
        self.lock = threading.Lock()
        self.subs = []          # fn(data, first_frame, channels): the recorder
        self.player = None      # fn(frames) -> bytes for the out channels, or None for silence
        self.clients = []       # queues for ffmpeg (Listen, the RTA, the MD log)
        self.frames = 0
        self.levels = [0.0] * self.ch   # each channel's peak in the last quarter-second, 0-1
        self.status, self.reason, self.device = "off", "", ""
        self.running = False
        self.srv = None

    @property
    def on(self):
        return self.kind in ("asio", "fake")

    def input_args(self):
        """What ffmpeg reads to get every channel (Listen, the RTA, the MD log): DVS channel n is c(n-1)."""
        return ["-f", "s32le", "-ar", str(self.rate), "-ac", str(self.ch), "-i", "tcp://127.0.0.1:%d" % self.port]

    def start(self):
        if not self.on or self.running:
            return
        self.running = True
        try:
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("127.0.0.1", self.port))
            self.port = self.srv.getsockname()[1]
            self.srv.listen(16)
            threading.Thread(target=self._accept, daemon=True).start()
        except OSError as e:
            self.status, self.reason = "error", "Port %d is in use, so Listen and the RTA can't hear the engine (%s)." % (self.port, e)
            self.log.error("engine: %s", self.reason)
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False
        if self.srv:
            try:
                self.srv.close()
            except OSError:
                pass

    def subscribe(self, fn):
        with self.lock:
            self.subs.append(fn)

    def unsubscribe(self, fn):
        with self.lock:
            self.subs = [s for s in self.subs if s is not fn]

    def play(self, fn):
        """fn(frames) -> interleaved int32 bytes for self.out_ch channels, or None (silence). None stops playing."""
        self.player = fn

    def view(self):
        return {"kind": self.kind, "status": self.status, "reason": self.reason, "device": self.device,
                "channels": self.ch, "rate": self.rate, "listeners": len(self.clients),
                "playing": self.player is not None, "seconds": round(self.frames / self.rate, 1)}

    def peaks(self):
        return list(self.levels)

    # ---- the loop: read a block, hand it round, send a block out ----
    def _open(self):
        if self.backend is None:
            if self.kind == "fake":
                self.backend = FakeBackend(self.want_ch, self.rate)
            else:
                self.backend = AsioBackend(self.want_ch, self.rate, self.cfg.get("engine_device", "Dante"))
        self.ch = self.backend.ch
        self.out_ch = getattr(self.backend, "out_ch", self.ch)
        self.levels = [0.0] * self.ch
        self.device = self.backend.name

    def _run(self):
        while self.running:
            try:
                self._open()
            except Exception as e:  # sounddevice missing, DVS not in ASIO mode or not started...
                self.backend = None
                msg = str(e) if isinstance(e, OSError) else "%s: %s" % (e.__class__.__name__, e)
                if self.reason != msg:
                    self.log.error("engine: can't open the sound: %s", msg)
                self.status, self.reason = "error", msg
                time.sleep(5)
                continue
            self.status, self.reason = "running", ""
            self.log.info("engine: %s, %d in, %d out at %d Hz", self.device, self.ch, self.out_ch, self.rate)
            n = 0
            silence = bytes(BLOCK * 4 * max(self.out_ch, 1))
            try:
                while self.running:
                    data = self.backend.read(BLOCK)
                    first = self.frames
                    self.frames += BLOCK
                    with self.lock:
                        subs, clients = list(self.subs), list(self.clients)
                    for fn in subs:
                        try:
                            fn(data, first, self.ch)
                        except Exception as e:
                            self.log.error("engine: %s", e)
                    for q in clients:
                        try:
                            q.put_nowait(data)
                        except queue.Full:  # that listener has stopped reading: let it go
                            self._drop(q)
                    n += 1
                    if n % 12 == 0:
                        self._levels(data)
                    out = None
                    p = self.player
                    if p is not None:
                        try:
                            out = p(BLOCK)
                        except Exception as e:
                            self.log.error("engine: playback: %s", e)
                    self.backend.write(out if out else silence)
            except Exception as e:
                self.log.error("engine: the sound stopped: %s", e)
                self.status, self.reason = "error", "The sound stopped: %s" % e
                try:
                    self.backend.close()
                except Exception:
                    pass
                self.backend = None
                time.sleep(2)

    def _levels(self, data):
        a = array.array("i")
        a.frombytes(data)
        ch = self.ch
        self.levels = [max(max(s), -min(s)) / FULL if s else 0.0 for s in (a[c::ch] for c in range(ch))]

    # ---- ffmpeg's way in: every channel, as raw samples, on 127.0.0.1 ----
    def _accept(self):
        while self.running:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            q = queue.Queue(CLIENT_Q)
            q.conn = conn
            with self.lock:
                self.clients.append(q)
            threading.Thread(target=self._send, args=(q,), daemon=True).start()

    def _send(self, q):
        try:
            while self.running:
                data = q.get()
                if data is None:
                    break
                q.conn.sendall(data)
        except OSError:
            pass
        self._drop(q)

    def _drop(self, q):
        with self.lock:
            if q not in self.clients:
                return
            self.clients.remove(q)
        try:
            q.put_nowait(None)
        except queue.Full:
            pass
        try:
            q.conn.close()
        except OSError:
            pass
