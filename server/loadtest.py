#!/usr/bin/env python3
"""Load test: how the C3 Life server copes with a busy Sunday, all at once, for a few minutes:
  - the audio engine at 64 channels (Dante Virtual Soundcard in ASIO, or made-up tones with --fake)
  - recording every channel as 24-bit WAVs (to --drive, or a temporary folder)
  - several Listen streams (ffmpeg making MP3, like phones listening)
  - the RTA (ffmpeg plus the same plain-Python FFT, five times a second, two channels)
It measures the CPU and memory used, whether the engine ever fell behind (a block late), and whether the recorder
dropped anything. It changes nothing on any device and doesn't need C3 Life running (stop the C3 Life task first,
because only one program can open DVS in ASIO).

On the HP (an Administrator PowerShell, in C:\\C3Life\\server):
    schtasks /end /tn "C3 Life"
    py loadtest.py --minutes 5 --listen 8 --drive E:\\        (E: = the recording drive; leave it out for a temp folder)
    schtasks /run /tn "C3 Life"
Anywhere else, with made-up tones:  python3 loadtest.py --fake --minutes 1
It prints a summary and writes loadtest-report-<date>.txt next to where it was run. psutil (pip install psutil) gives
per-process CPU; without it only the total is measured on Windows.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio_engine as ae  # noqa: E402
import recorder as rec_mod  # noqa: E402
import rta_live  # noqa: E402

try:
    import psutil
except ImportError:
    psutil = None


class Late:
    """Watches the engine's blocks: each should come every BLOCK/rate seconds."""
    def __init__(self, rate):
        self.every = ae.BLOCK / float(rate)
        self.last, self.late, self.worst, self.blocks = 0.0, 0, 0.0, 0

    def __call__(self, data, first, ch):
        t = time.perf_counter()
        if self.last:
            gap = t - self.last
            self.worst = max(self.worst, gap)
            if gap > self.every * 4:  # a block well over 80 ms late: someone would hear it as a glitch in a stream
                self.late += 1
        self.last = t
        self.blocks += 1


def cpu_sampler(stop, out, procs):
    """Every second: total CPU %, this process, and the ffmpegs (with psutil)."""
    me = psutil.Process() if psutil else None
    watched = {}
    if psutil:
        psutil.cpu_percent(None)
        me.cpu_percent(None)
    while not stop.wait(1.0):
        row = {"t": time.time()}
        if psutil:
            row["total"] = psutil.cpu_percent(None)
            row["me"] = me.cpu_percent(None) / psutil.cpu_count()
            ff = 0.0
            for p in list(procs):
                try:
                    if p.pid not in watched:
                        watched[p.pid] = psutil.Process(p.pid)
                        watched[p.pid].cpu_percent(None)
                    ff += watched[p.pid].cpu_percent(None) / psutil.cpu_count()
                except psutil.Error:
                    pass
            row["ffmpeg"] = ff
            row["mem_mb"] = me.memory_info().rss / 1e6
            row["free_mb"] = psutil.virtual_memory().available / 1e6
        elif hasattr(os, "getloadavg"):
            row["load"] = os.getloadavg()[0]
        out.append(row)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="made-up tones instead of Dante Virtual Soundcard")
    ap.add_argument("--channels", type=int, default=64)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--listen", type=int, default=6, help="Listen streams (MP3) at once")
    ap.add_argument("--no-rta", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--drive", default="", help="a folder or drive to record onto (the recording is deleted after)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log = logging.getLogger("loadtest")
    lines = []

    def say(t=""):
        print(t, flush=True)
        lines.append(t)

    say("C3 Life load test · %s · %s" % (time.strftime("%Y-%m-%d %H:%M"), "made-up tones" if a.fake else "Dante Virtual Soundcard (ASIO)"))
    say("%d channels · %d Listen streams · RTA %s · recording %s · %.1f minutes" % (a.channels, a.listen, "off" if a.no_rta else "on", "off" if a.no_record else "on", a.minutes))
    if psutil:
        say("CPU: %d threads · memory %.1f GB" % (psutil.cpu_count(), psutil.virtual_memory().total / 1e9))

    eng = ae.Engine({"audio_engine": "fake" if a.fake else "asio", "engine_port": 0, "engine_channels": a.channels}, log)
    late = Late(eng.rate)
    eng.subscribe(late)
    eng.start()
    t0 = time.time()
    while eng.status != "running" and time.time() - t0 < 15:
        time.sleep(0.1)
    if eng.status != "running":
        say("The engine didn't start: %s" % (eng.reason or eng.status))
        say("Is the C3 Life task stopped, and Dante Virtual Soundcard running in ASIO, 64 x 64?")
        return 2
    say("Engine running: %d channels at %d Hz (%s)" % (eng.ch, eng.rate, eng.device or eng.kind))

    tmp = None
    r = None
    if not a.no_record:
        root = a.drive or tempfile.mkdtemp(prefix="c3life-loadtest-")
        tmp = None if a.drive else root
        os.makedirs(os.path.join(root, rec_mod.FOLDER), exist_ok=True)
        data = tempfile.mkdtemp(prefix="c3life-loadtest-data-")
        r = rec_mod.Recorder(data, {"record_drive_roots": [root]}, log, eng)
        r.check_drives()
        err = r.start("load test")
        if err:
            say("Recording didn't start: %s" % err)
            r = None
        else:
            say("Recording %d tracks to %s" % (len(r.rec["tracks"]), root))

    procs, caps = [], []
    for i in range(a.listen):
        c = i * 2 % eng.ch
        cmd = [a.ffmpeg, "-hide_banner", "-loglevel", "error"] + eng.input_args() + \
            ["-af", "pan=stereo|c0=c%d|c1=c%d" % (c, c + 1), "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-"]
        try:
            procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        except OSError as e:
            say("ffmpeg didn't start (%s): is it installed? Carrying on without Listen streams." % e)
            break

    rta_stop = threading.Event()
    rta_n = [0]
    if not a.no_rta:
        cap = rta_live.Capture([a.ffmpeg, "-hide_banner", "-loglevel", "error"] + eng.input_args() + ["-af", "pan=2c|c0=c0|c1=c1", "-ar", str(rta_live.RATE), "-f", "f32le", "-"], 2, log)
        try:
            cap.start()
            procs.append(cap.proc)
            caps.append(cap)
            spec = rta_live.Spectrum()

            def rta_loop():
                while not rta_stop.is_set():
                    x = cap.read(rta_live.RATE // 5)
                    if x is None:
                        return
                    a2 = list(x[-2 * spec.n::2]) if len(x) >= 2 * spec.n else list(x[0::2]) + [0.0] * (spec.n - len(x) // 2)
                    b2 = list(x[-2 * spec.n + 1::2]) if len(x) >= 2 * spec.n else list(x[1::2]) + [0.0] * (spec.n - len(x) // 2)
                    px, py = spec.powers(a2[:spec.n], b2[:spec.n])
                    spec.to_bands(px), spec.to_bands(py)
                    rta_n[0] += 1
            threading.Thread(target=rta_loop, daemon=True).start()
        except OSError as e:
            say("The RTA's ffmpeg didn't start (%s)." % e)

    stop = threading.Event()
    rows = []
    threading.Thread(target=cpu_sampler, args=(stop, rows, procs), daemon=True).start()
    end = time.time() + a.minutes * 60
    say("Running…")
    while time.time() < end:
        time.sleep(min(15, max(0.1, end - time.time())))
        alive = sum(1 for p in procs if p.poll() is None)
        last = rows[-1] if rows else {}
        say("  %3.0f s · CPU %s · engine late %d · ffmpegs running %d/%d%s" % (
            time.time() - (end - a.minutes * 60), ("%.0f%%" % last["total"]) if "total" in last else "?", late.late, alive, len(procs),
            (" · recorder dropped %d" % r.rec["dropped"]) if r and r.rec else ""))
    stop.set()
    rta_stop.set()

    alive_end = sum(1 for p in procs[:a.listen] if p.poll() is None)
    dropped = r.rec["dropped"] if r and r.rec else 0
    size = 0
    if r and r.rec:
        folder = r.rec.get("folder") or ""
        r.stop("load test")
        for root_, _, files in os.walk(folder):
            size += sum(os.path.getsize(os.path.join(root_, f)) for f in files)
    for p in procs:
        if p.poll() is None:
            p.kill()
    for c in caps:
        c.stop()
    eng.stop()

    say("")
    say("Results")
    secs = a.minutes * 60
    say("  Engine blocks: %d (expected about %d) · late (over 80 ms): %d · worst gap %.0f ms" % (late.blocks, secs / late.every, late.late, late.worst * 1000))
    if r:
        say("  Recording: %s written (%.1f MB a minute) · dropped blocks: %d" % ("%.0f MB" % (size / 1e6), size / 1e6 / max(a.minutes, 0.01), dropped))
    say("  Listen streams still running at the end: %d of %d" % (alive_end, a.listen))
    if not a.no_rta:
        say("  RTA spectra worked out: %d (%.1f a second; it needs 5)" % (rta_n[0], rta_n[0] / secs))
    if rows and "total" in rows[0]:
        tot = [x["total"] for x in rows]
        say("  CPU total: average %.0f%%, highest %.0f%% · C3 Life's part %.0f%% · ffmpeg %.0f%%" % (
            sum(tot) / len(tot), max(tot), sum(x["me"] for x in rows) / len(rows), sum(x["ffmpeg"] for x in rows) / len(rows)))
        say("  Memory: this test %.0f MB · free on the computer %.0f MB at the lowest" % (max(x["mem_mb"] for x in rows), min(x["free_mb"] for x in rows)))
    elif rows and "load" in rows[0]:
        say("  Load average: %.2f at the highest (psutil isn't installed, so no CPU %%)" % max(x["load"] for x in rows))
    ok = late.late == 0 and dropped == 0 and (a.no_rta or rta_n[0] / secs >= 4)
    say("  Verdict: %s" % ("OK: it kept up." if ok else "It fell behind somewhere (see above)."))
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    elif r and r.last:
        shutil.rmtree(os.path.join(a.drive, rec_mod.FOLDER, r.last["name"]), ignore_errors=True)
    name = "loadtest-report-%s.txt" % time.strftime("%Y-%m-%d-%H%M")
    with open(name, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("Written to %s" % os.path.abspath(name))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
