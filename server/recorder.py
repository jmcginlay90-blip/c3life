"""Recording: the channels C3 Life hears from Dante Virtual Soundcard (the audio engine), one WAV file each, onto an
external drive. Played back to the Wing later for a virtual soundcheck.

The drive. Plug in a USB drive: once an Admin (or anyone with Recording control) has said "Use this drive for
recordings", or if it already has a "C3 Life Recordings" folder at the top, it's the recording drive whenever it's
plugged in. Nothing else is ever written to. A recording drive unplugged while recording stops the recording; what was
written up to then is kept.

What's recorded. Each DVS channel ticked (by default every one with a Dante route), as 24-bit 48 kHz WAV, named from
the Wing: the Dante route into that DVS channel says which Wing card output feeds it, the Wing says which input that
output sends, and the Wing channel using that input gives the name ("03 Lead Vox.wav"). Each recording is a folder:
  <drive>\\C3 Life Recordings\\2026-10-25 1002\\  01 Lead Vox.wav ... session.json, markers.csv, markers.txt (Audacity),
  2026-10-25 1002.RPP (a REAPER project with every track named and every marker)

Markers. While recording, each time ProPresenter puts up a slide from a different presentation (or playlist item),
a marker is added with its name: the songs in order, the Message and so on. Anyone with Recording control can add
one too.

By itself (Settings, Recording control): start on the start slide (a presentation, slide group or slide label, as
slide cues match), or as soon as the recording drive is plugged in; stop on the end slide, or after the most hours set.
This is the one thing in C3 Life that runs by itself: recording only listens and changes nothing on any device.

Playback (virtual soundcheck): each track goes out on the DVS output with the same number it was recorded from, which
Dante routes to a Wing card input. Playing asks first: it lists every Wing channel it will switch to the card, switches
them (Control only), and plays. Back to stage stops and puts every channel's input back as it was. Not while recording.

data/recording.json: {"settings": {...}, "drives": [{"id", "label"}]}
"""

import array
import copy
import json
import os
import queue
import re
import shutil
import string
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import patching

FOLDER = "C3 Life Recordings"
KINDS = ("presentation", "group", "label")
DEFAULTS = {"auto": "off",                 # "off" | "slide" (the start slide) | "plug" (as soon as the drive is plugged in)
            "start": {"kind": "presentation", "text": ""},
            "stop": {"kind": "presentation", "text": ""},
            "max_hours": 4,                # stops by itself after this long, if nothing else has
            "markers": "presentation",     # "presentation" | "item" (the playlist item's name) | "off"
            "sections": True,              # a marker for each part of a song too: its slide group (Verse 1, Chorus, Bridge...)
            "split": [],                   # DVS channels kept as mono tracks, though they're one side of a stereo Wing channel
            "channels": "routed",          # "routed" (every DVS channel Dante feeds) or [1, 2, ...]
            "local": True,                 # no recording drive plugged in: record onto this computer instead
            "copy_auto": True,             # copy what's on this computer to the recording drive when it's plugged in
            "free_local": True,            # once a copy is checked, delete it from this computer
            "make_room": True}             # this computer nearly full: delete its oldest recordings that are on the recording drive
MAKE_ROOM = 40 * (1 << 30)                 # keep this much free on this computer (about 1 h 15 of 64 tracks)
COPIES = "C3 Life Recordings (copies)"     # Copy to a drive that isn't the recording drive (one to take home)
COPIED = "copied.json"                     # in a recording on this computer: the recording drive it was copied to, checked
LOW_SPACE = 1 << 30                        # stops with 1 GB left
HEADER_EVERY = 5.0                         # seconds between WAV header updates (a power cut keeps what was written)
WRITE_Q = 500                              # blocks waiting to be written (about 10 s)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s or "").strip(" .")
    return s[:60] or "Track"


# ---- drives ----
def windows_drives():
    import ctypes
    k = ctypes.windll.kernel32
    mask = k.GetLogicalDrives()
    system = (os.environ.get("SystemDrive") or "C:").upper()
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = letter + ":\\"
        if root[:2] == system or k.GetDriveTypeW(root) not in (2, 3):  # removable, or fixed (USB SSDs say fixed)
            continue
        name, fs, serial = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261), ctypes.c_uint32()
        if not k.GetVolumeInformationW(root, name, 261, ctypes.byref(serial), None, None, fs, 261):
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        out.append({"id": "%08X" % serial.value, "root": root, "label": name.value or "Drive %s" % letter, "fs": fs.value,
                    "free": u.free, "total": u.total})
    return out


def folder_drives(roots):
    """Folders standing in for drives (the tests, or a fixed place set in config.json)."""
    out = []
    for r in roots:
        if os.path.isdir(r):
            u = shutil.disk_usage(r)
            out.append({"id": "dir:" + os.path.abspath(r), "root": r, "label": os.path.basename(r.rstrip("/\\")) or r, "fs": "",
                        "free": u.free, "total": u.total})
    return out


# ---- WAV ----
class Wav:
    """A 24-bit WAV (mono, or stereo for a pair) that's always playable: the header's sizes are brought up to date every few seconds."""

    def __init__(self, path, rate, channels=1):
        self.path, self.rate, self.bytes, self.channels = path, rate, 0, channels
        self.f = open(path, "wb")
        self.f.write(self._header())
        self.last = time.time()

    def _header(self):
        n = self.bytes
        return b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, self.channels, self.rate, self.rate * 3 * self.channels, 3 * self.channels, 24) + \
            b"data" + struct.pack("<I", n)

    def write(self, b):
        self.f.write(b)
        self.bytes += len(b)
        if time.time() - self.last > HEADER_EVERY:
            self.fix()

    def fix(self):
        pos = self.f.tell()
        self.f.seek(0)
        self.f.write(self._header())
        self.f.seek(pos)
        self.f.flush()
        self.last = time.time()

    def close(self):
        self.fix()
        self.f.close()


def split24(data, c, ch):
    """Channel c (0-based) of interleaved 32-bit samples -> 24-bit mono bytes (the top three bytes of each)."""
    step = 4 * ch
    n = len(data) // step
    out = bytearray(3 * n)
    out[0::3] = data[4 * c + 1::step]
    out[1::3] = data[4 * c + 2::step]
    out[2::3] = data[4 * c + 3::step]
    return out


def join24(a, b):
    """Two 24-bit mono byte strings -> one interleaved stereo one (left, right)."""
    out = bytearray(len(a) + len(b))
    for i in range(3):
        out[i::6] = a[i::3]
        out[3 + i::6] = b[i::3]
    return out


def part24(st, side):
    """One side (0 left, 1 right) of 24-bit stereo bytes -> mono bytes."""
    n = len(st) // 6
    out = bytearray(3 * n)
    for i in range(3):
        out[i::3] = st[3 * side + i:6 * n:6]
    return out


def put24(out, mono, c, ch):
    """Put 24-bit mono bytes into channel c of interleaved 32-bit samples (bytearray `out`)."""
    step = 4 * ch
    n = len(mono) // 3
    out[4 * c + 1:4 * c + 1 + step * n:step] = mono[0::3]
    out[4 * c + 2:4 * c + 2 + step * n:step] = mono[1::3]
    out[4 * c + 3:4 * c + 3 + step * n:step] = mono[2::3]


def clock(secs):
    secs = int(secs)
    return "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)


def kind(m):
    """"song", "section" or "" (Start, End, one added by hand). Recordings from before sections: ProPresenter's are songs."""
    return m.get("kind") or ("song" if m.get("by") == "ProPresenter" else "")


def parts(markers, length):
    """The markers with where each song and section ends: a section at the next section or song, a song at the next
    song (or the end). One added by hand is only somewhere to play from."""
    out = []
    for i, m in enumerate(markers):
        k = kind(m)
        m = dict(m, kind=k)
        if k:
            stops = ("song",) if k == "song" else ("song", "section")
            m["end"] = next((x["t"] for x in markers[i + 1:] if kind(x) in stops or x.get("name") == "End"), length)
        out.append(m)
    return out


def rpp(name, rate, tracks, markers, length):
    """A REAPER project: each track with its file, each marker; each section of a song is a region."""
    q = lambda s: '"%s"' % s.replace('"', "'")
    lines = ["<REAPER_PROJECT 0.1 \"6.0\" 0", "  SAMPLERATE %d 0 0" % rate]
    for i, m in enumerate(parts(markers, length), 1):
        if m["kind"] == "section":
            lines += ["  MARKER %d %.3f %s 1" % (i, m["t"], q(m["name"])), "  MARKER %d %.3f \"\" 1" % (i, m["end"])]
        else:
            lines.append("  MARKER %d %.3f %s 0" % (i, m["t"], q(m["name"])))
    for t in tracks:
        lines += ["  <TRACK", "    NAME %s" % q(t["name"]), "    <ITEM", "      POSITION 0", "      LENGTH %.3f" % length,
                  "      NAME %s" % q(t["name"]), "      <SOURCE WAVE", "        FILE %s" % q(t["file"]), "      >", "    >", "  >"]
    lines.append(">")
    return "\n".join(lines) + "\n"


# ---- ProPresenter: which slide is up ----
class SlideWatch:
    """Asks ProPresenter once a second which slide is up. -> on_slide({"presentation", "group", "label", "item", "index"})
    each time it changes."""

    def __init__(self, cfg, on_slide):
        self.cfg, self.on_slide = cfg, on_slide
        self.last, self.pres = None, {}
        self.error = ""

    def _get(self, path):
        url = (self.cfg.get("propresenter") or "").rstrip("/")
        if not url:
            raise OSError("ProPresenter hasn't been found on the network yet.")
        with urllib.request.urlopen(url + path, timeout=3) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else {}

    def poll(self):
        try:
            si = (self._get("/v1/presentation/slide_index") or {}).get("presentation_index") or {}
            pid = si.get("presentation_id") or {}
            uuid, idx = pid.get("uuid") or "", si.get("index")
            if not uuid or not isinstance(idx, int):
                self.error = ""
                return
            key = (uuid, idx)
            if key == self.last:
                return
            self.last = key
            if uuid not in self.pres:
                p = (self._get("/v1/presentation/" + urllib.parse.quote(uuid)) or {}).get("presentation") or {}
                flat = []
                for g in p.get("groups") or []:
                    for s in g.get("slides") or []:
                        flat.append((g.get("name") or "", s.get("label") or ""))
                self.pres = {uuid: {"name": (p.get("id") or {}).get("name") or pid.get("name") or "", "slides": flat}}
            p = self.pres[uuid]
            g, lab = p["slides"][idx] if 0 <= idx < len(p["slides"]) else ("", "")
            item = ""
            try:
                a = self._get("/v1/playlist/active") or {}
                item = (((a.get("presentation") or {}).get("item")) or {}).get("name") or ""
            except (OSError, ValueError):
                pass
            self.error = ""
            self.on_slide({"presentation": p["name"] or pid.get("name") or "", "group": g, "label": lab, "item": item, "index": idx})
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.error = "ProPresenter didn't answer (%s)." % (getattr(e, "reason", None) or e)


def matches(rule, slide):
    if not rule or not (rule.get("text") or "").strip():
        return False
    return (slide.get(rule.get("kind")) or "").strip().lower() == rule["text"].strip().lower()


def valid_settings(s):
    if not isinstance(s, dict):
        return "Settings not understood."
    if s.get("auto") not in ("off", "slide", "plug"):
        return "Start by itself: off, on the start slide, or when the drive is plugged in."
    for k in ("start", "stop"):
        r = s.get(k)
        if not isinstance(r, dict) or r.get("kind") not in KINDS or not isinstance(r.get("text"), str) or len(r["text"]) > 80:
            return "The %s slide isn't understood." % k
    if s["auto"] == "slide" and not s["start"]["text"].strip():
        return "Say which slide starts the recording."
    if not isinstance(s.get("max_hours"), (int, float)) or isinstance(s.get("max_hours"), bool) or not 0.5 <= s["max_hours"] <= 12:
        return "Stop after 0.5 to 12 hours."
    if not all(isinstance(s.get(k), bool) for k in ("local", "copy_auto", "free_local", "make_room", "sections")):
        return "Settings not understood."
    if s.get("markers") not in ("presentation", "item", "off"):
        return "Markers: presentation, playlist item or off."
    c = s.get("channels")
    if c != "routed" and not (isinstance(c, list) and 0 < len(c) <= 64 and len(set(c)) == len(c) and
                              all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 64 for n in c)):
        return "Channels: the routed ones, or a list of DVS channels 1-64."
    sp = s.get("split", [])
    if not (isinstance(sp, list) and len(sp) <= 64 and all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 64 for n in sp)):
        return "Mono tracks: a list of DVS channels 1-64."
    return None


class Recorder:
    def __init__(self, data_dir, cfg, log, engine, desk=None, dante=None, list_drives=None):
        self.path = os.path.join(data_dir, "recording.json")
        self.cfg, self.log, self.engine, self.desk, self.dante = cfg, log, engine, desk, dante
        self.list_drives = list_drives or (lambda: folder_drives(cfg["record_drive_roots"]) if cfg.get("record_drive_roots")
                                           else (windows_drives() if os.name == "nt" else []))
        self.lock = threading.Lock()
        self.settings, self.approved = copy.deepcopy(DEFAULTS), []
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            st = dict(self.settings, **(d.get("settings") or {}))
            if not valid_settings(st):
                self.settings = st
            self.approved = [x for x in d.get("drives", []) if isinstance(x, dict) and x.get("id")]
        except (OSError, ValueError, AttributeError):
            pass
        self.drives, self.drive = [], None   # every drive seen; the recording drive (or None)
        self.rec, self.last = None, None     # the recording going on; the last one, finished
        self.q = queue.Queue(WRITE_Q)
        self.player = None                   # playback going on
        self.stage = []                      # Wing channel inputs to put back: [(n, grp, idx)]
        self.stage_routes = []               # Dante routes to put back: the play plan's "dante" steps
        self.layouts = patching.Layouts(data_dir)
        self.names_cache = (0.0, {})
        self.slide = None
        self.watch = SlideWatch(cfg, self._on_slide)
        self.running = False
        self.local = {"id": "local", "label": "This computer", "ok": True,
                      "root": cfg.get("record_local_dir") or os.path.join(os.path.dirname(os.path.abspath(data_dir)), "recordings")}
        self.job = None                      # a copy going on: {"name", "to", "done", "total", "error"}
        self.short = False                   # this computer is nearly full and nothing could be deleted (Home warns)
        self.job_lock = threading.Lock()

    # ---- settings and drives ----
    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"settings": self.settings, "drives": self.approved}, f, indent=1)
        os.replace(tmp, self.path)

    def save_settings(self, body, who):
        st = dict(self.settings, **{k: v for k, v in (body or {}).items() if k in DEFAULTS})
        err = valid_settings(st)
        if err:
            return err
        with self.lock:
            self.settings = st
            self._save()
        self.log.info("recording: %s changed the settings (start by itself: %s)", who, st["auto"])
        return None

    def use_drive(self, did, use, who):
        d = next((x for x in self.drives if x["id"] == did), None)
        with self.lock:
            if use:
                if not d:
                    return "That drive isn't plugged in."
                if self.rec:
                    return "Not while recording."
                try:
                    os.makedirs(os.path.join(d["root"], FOLDER), exist_ok=True)
                except OSError as e:
                    return "C3 Life can't write to %s (%s)." % (d["label"], e)
                if not any(x["id"] == did for x in self.approved):
                    self.approved.append({"id": did, "label": d["label"]})
            else:
                self.approved = [x for x in self.approved if x["id"] != did]
            self._save()
        self.log.info("recording: %s %s %s for recordings", who, "chose" if use else "stopped using", d["label"] if d else did)
        self.check_drives()
        return None

    def check_drives(self):
        try:
            drives = self.list_drives()
        except Exception as e:
            self.log.error("recording: can't list the drives: %s", e)
            drives = []
        for d in drives:
            d["ok"] = any(x["id"] == d["id"] for x in self.approved) or os.path.isdir(os.path.join(d["root"], FOLDER))
        before = self.drive
        self.drives = drives
        ready = next((d for d in drives if d["ok"]), None)
        self.drive = ready
        if self.rec and self.rec["drive"] == "local":
            loc = self._local_drive()
            if loc and loc["free"] < LOW_SPACE:
                self.stop("this computer is full", error="This computer's drive is nearly full, so the recording stopped.")
        elif self.rec and not any(d["id"] == self.rec["drive"] for d in drives):
            self.stop("the recording drive was unplugged", error="The recording drive was unplugged, so the recording stopped. What was recorded up to then is on it.")
        elif self.rec and ready and ready["id"] == self.rec["drive"] and ready["free"] < LOW_SPACE:
            self.stop("the drive is full", error="The recording drive is nearly full, so the recording stopped.")
        if ready and (not before or before["id"] != ready["id"]):
            if not any(x["id"] == ready["id"] for x in self.approved):  # found by its folder: remember it
                with self.lock:
                    self.approved.append({"id": ready["id"], "label": ready["label"]})
                    self._save()
            self.log.info("recording: %s is plugged in (%s free)", ready["label"], gb(ready["free"]))
            if self.settings["auto"] == "plug" and not self.rec and not self.player:
                self.start("auto: the drive was plugged in")
        if ready and self.settings["copy_auto"] and not self.rec and not self.job:
            waiting = [x["name"] for x in self._list(self.local)]
            if waiting:
                self.copy([(n, "local") for n in reversed(waiting)], ready["id"], "auto: %s was plugged in" % ready["label"])

    # ---- names, from Dante and the Wing ----
    def names(self, fresh=False):
        """{dvs channel: {"name", "wing_ch", "wing_chs", "side", "src", "from", "from_dev", "from_ch"}} for every DVS channel with a
        Dante route, named as on the Wing (see patching.trace)."""
        t, cached = self.names_cache
        if not fresh and time.time() - t < 30:
            return cached
        dvs = {}
        if self.dante:
            try:
                dvs = (self.dante.get() or {}).get("channels") or {}
            except Exception:
                dvs = {}
        routes, chans, wing = [], {}, ""
        if dvs and self.dante:
            routes, chans, _, wing = self._dante_now()
            if not wing:
                wing = next((r["tx_device"] for r in dvs.values() if "wing" in (r.get("tx_device") or "").lower()), "")
        strips, inputs, outs = {}, {}, {}
        if self.desk:
            try:
                st = self.desk.state()
                strips, inputs = (st.get("strips") or {}).get("ch") or {}, st.get("inputs") or {}
                if wing and any((r.get("tx_device") or "").lower() == wing.lower() for r in dvs.values()) and hasattr(self.desk, "outputs"):
                    outs = self.desk.outputs()
            except Exception:
                pass
        out = patching.trace(dvs, routes, chans, strips, inputs, outs, wing)
        waiting = any(v.pop("waiting", False) for v in out.values())
        # kept 30 s, but only 2 s while any name is still waiting on the Wing (just after start-up), so a recording
        # started then isn't named "Wing card out 33" for half a minute
        self.names_cache = (time.time() - (28 if waiting else 0), out)
        return out

    def tracks_for(self, ch_count):
        names = self.names()
        want = self.settings["channels"]
        chans = sorted(names) if want == "routed" else list(want)
        if want == "routed" and not chans:
            chans = list(range(1, min(ch_count, 64) + 1))  # Dante can't say: every channel
        chans = [c for c in chans if c <= ch_count]
        pr = patching.pairs({c: names[c] for c in chans if c in names}, set(self.settings.get("split") or []))
        right = set(pr.values())
        out = []
        for c in chans:
            if c in right:
                continue
            n = names.get(c) or {}
            nm = n.get("name") or "DVS %d" % c
            t = {"dvs": c, "name": nm, "wing_ch": n.get("wing_ch"), "wing_chs": n.get("wing_chs") or [], "src": n.get("src", ""),
                 "from": n.get("from", ""), "from_dev": n.get("from_dev", ""), "from_ch": n.get("from_ch", ""), "pair": None,
                 "file": "%02d %s.wav" % (c, safe_name(nm))}
            if c in pr:
                r = names.get(pr[c]) or {}
                t.update(pair=pr[c], from_dev_r=r.get("from_dev", ""), from_ch_r=r.get("from_ch", ""),
                         **{"from": "%s + %s" % (n.get("from", ""), (r.get("from") or "").split(" · ")[-1])})
            out.append(t)
        return out

    # ---- recording ----
    def start(self, who):
        with self.lock:
            if self.rec:
                return "It's already recording."
            if self.player:
                return "Not while playing back. Press Back to stage first."
            if not self.engine or self.engine.status != "running":
                return "C3 Life isn't hearing Dante Virtual Soundcard (%s)." % ((self.engine and self.engine.reason) or "the audio engine is off")
            d = self.drive or (self._local_drive() if self.settings["local"] else None)
            if not d:
                return "No recording drive is plugged in."
            if d["free"] < LOW_SPACE:
                return "The recording drive is nearly full."
            tracks = self.tracks_for(self.engine.ch)
            name = time.strftime("%Y-%m-%d %H%M")
            folder = os.path.join(d["root"], FOLDER, name)
            n = 2
            while os.path.exists(folder):
                folder = os.path.join(d["root"], FOLDER, "%s (%d)" % (name, n))
                n += 1
            try:
                os.makedirs(folder)
                files = [Wav(os.path.join(folder, t["file"]), self.engine.rate, 2 if t.get("pair") else 1) for t in tracks]
            except OSError as e:
                return "C3 Life couldn't write to %s (%s)." % (d["label"], e)
            self.rec = {"name": os.path.basename(folder), "folder": folder, "drive": d["id"], "drive_label": d["label"], "by": who,
                        "started": time.time(), "first": None, "frames": 0, "tracks": tracks, "files": files, "markers": [],
                        "rate": self.engine.rate, "dropped": 0, "error": ""}
            self.q = queue.Queue(WRITE_Q)
        threading.Thread(target=self._writer, args=(self.rec, self.q), daemon=True).start()
        self.engine.subscribe(self._feed)
        self.log.info("recording: %s started recording %d tracks to %s", who, len(tracks), folder)
        self.marker("Start", who, quiet=True)
        return None

    def _feed(self, data, first, ch):
        r = self.rec
        if not r:
            return
        try:
            self.q.put_nowait((data, first, ch))
        except queue.Full:
            r["dropped"] += 1

    def _writer(self, r, q):
        while True:
            item = q.get()
            if item is None:
                break
            data, first, ch = item
            if r["first"] is None:
                r["first"] = first
            try:
                for t, w in zip(r["tracks"], r["files"]):
                    m = split24(data, t["dvs"] - 1, ch)
                    w.write(join24(m, split24(data, t["pair"] - 1, ch)) if t.get("pair") else m)
            except OSError as e:
                r["error"] = "Writing stopped: %s" % e
                self.log.error("recording: %s", r["error"])
                threading.Thread(target=self.stop, args=("a write failed",), kwargs={"error": r["error"]}, daemon=True).start()
                break
            r["frames"] = first + len(data) // (4 * ch) - r["first"]
        for w in r["files"]:
            try:
                w.close()
            except OSError:
                pass

    def now(self):
        """Seconds into the recording going on."""
        r = self.rec
        if not r:
            return 0.0
        if r["first"] is None:
            return 0.0
        return max(0.0, (self.engine.frames - r["first"]) / r["rate"])

    def marker(self, name, who, quiet=False, kind="", song=""):
        name = (name or "").strip()[:80]
        if not name:
            return "A marker needs a name."
        with self.lock:
            r = self.rec
            if not r:
                return "It isn't recording."
            m = {"t": round(self.now(), 2), "name": name, "by": who}
            if kind:
                m["kind"] = kind
            if song:
                m["song"] = song
            r["markers"].append(m)
        if not quiet:
            self.log.info("recording: marker %s (%s)", name, who)
        return None

    def stop(self, who, error=""):
        self.engine.unsubscribe(self._feed)
        with self.lock:
            r, self.rec = self.rec, None
        if not r:
            return "It isn't recording."
        r["error"] = r["error"] or error
        r["markers"].append({"t": round((self.engine.frames - (r["first"] or self.engine.frames)) / r["rate"], 2), "name": "End", "by": who})
        self.q.put(None)
        t0 = time.time()
        while any(not w.f.closed for w in r["files"]) and time.time() - t0 < 10:
            time.sleep(0.05)
        length = r["frames"] / r["rate"]
        tracks = [{k: t.get(k) for k in ("dvs", "pair", "name", "wing_ch", "wing_chs", "src", "file", "from", "from_dev", "from_ch", "from_dev_r", "from_ch_r")
                   if t.get(k) not in (None, "", [])} for t in r["tracks"]]
        try:
            self._write_info(r, tracks, length)
        except OSError as e:
            r["error"] = r["error"] or "The marker files couldn't be written (%s)." % e
        self.last = {"name": r["name"], "folder": r["folder"], "drive": r["drive_label"], "length": round(length, 1), "tracks": len(tracks),
                     "markers": [m for m in r["markers"] if m["name"] not in ("Start", "End")], "by": r["by"], "stopped_by": who,
                     "error": r["error"], "dropped": r["dropped"]}
        if error:
            self.log.warning("recording: stopped: %s", error)
        self.log.info("recording: %s stopped %s after %s (%d tracks)", who, r["name"], clock(length), len(tracks))
        return None

    def _write_info(self, r, tracks, length):
        write_session(r["folder"], {"name": r["name"], "rate": r["rate"], "length": round(length, 3),
                                    "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"])),
                                    "by": r["by"], "tracks": tracks, "markers": r["markers"]})

    # ---- moving a marker afterwards (the lyrics were a little early or late) ----
    def nudge(self, name, where, i, by, who):
        """Move marker i (as listed, without Start and End) by `by` seconds, and write the marker files again."""
        if not isinstance(by, (int, float)) or isinstance(by, bool) or not -60 <= by <= 60 or by == 0:
            return "Move it by up to a minute.", None
        folder, s = self._session(name, where)
        if not s:
            return "That recording isn't on the drive.", None
        if self.player and self.player.name == s.get("name"):
            return "Not while it's playing. Press Back to stage first.", None
        ms = s.get("markers") or []
        real = [k for k, m in enumerate(ms) if m.get("name") not in ("Start", "End")]
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(real):
            return "That marker isn't there.", None
        m = ms[real[i]]
        t0 = m["t"]
        m["t"] = round(min(max(0.0, t0 + by), float(s.get("length") or t0 + by)), 2)
        if kind(m) == "song":  # a song's first part starts with it
            for x in ms:
                if kind(x) == "section" and x.get("song") == m["name"] and abs(x["t"] - t0) < 0.05:
                    x["t"] = m["t"]
        start = [x for x in ms if x.get("name") == "Start"]
        end = [x for x in ms if x.get("name") == "End"]
        rest = sorted([x for x in ms if x.get("name") not in ("Start", "End")], key=lambda x: x["t"])
        s["markers"] = start + rest + end
        s["nudged"] = time.strftime("%Y-%m-%d %H:%M")
        try:
            write_session(folder, s)
        except OSError as e:
            return "The marker files couldn't be written (%s)." % e, None
        self.log.info("recording: %s moved the marker %s in %s from %s to %s", who, m["name"], name, clock(t0), clock(m["t"]))
        return None, [x for x in parts(s["markers"], s.get("length", 0)) if x.get("name") not in ("Start", "End")]

    def jump(self, step, who):
        p = self.player
        if not p:
            return "Nothing is playing.", ""
        err, label = p.jump(step)
        if not err:
            self.log.info("recording: %s jumped %s to %s", who, "on" if step > 0 else "back", label)
        return err, label


    # ---- ProPresenter: start, stop and markers ----
    def _on_slide(self, slide):
        self.slide = slide
        st = self.settings
        if self.rec:
            if matches(st["stop"], slide):
                self.stop("auto: the end slide (%s)" % (slide.get(st["stop"]["kind"]) or ""))
                return
            self._mark(slide)
        elif st["auto"] == "slide" and matches(st["start"], slide) and not self.player:
            err = self.start("auto: the start slide (%s)" % (slide.get(st["start"]["kind"]) or ""))
            if err:
                self.log.warning("recording: the start slide came up but it couldn't start: %s", err)
            else:
                self._mark(slide)

    def _mark(self, slide):
        """A new song (or the Message): a marker. A new slide group in it (Verse 1, Chorus...): a section marker.
        More slides of the same group: none."""
        st = self.settings
        if st["markers"] == "off":
            return
        song = slide.get("item" if st["markers"] == "item" else "presentation") or slide.get("presentation") or ""
        if not song:
            return
        r = self.rec
        ms = [m for m in (r["markers"] if r else []) if kind(m)]
        last_song = next((m for m in reversed(ms) if m["kind"] == "song"), None)
        if not last_song or last_song["name"] != song:
            self.marker(song, "ProPresenter", kind="song")
            ms = []
        group = (slide.get("group") or "").strip()
        if st.get("sections", True) and group:
            last = ms[-1] if ms else None
            if not (last and last["kind"] == "section" and last["name"] == group):
                self.marker(group, "ProPresenter", quiet=True, kind="section", song=song)

    # ---- the loop: drives every 3 s, ProPresenter every second, the time limit ----
    def start_loop(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        n = 0
        while self.running:
            if n % 3 == 0:
                self.check_drives()
            if n % 30 == 0 and self.settings["local"]:
                self.make_room(delete=self.settings["make_room"])
            if self.rec or self.settings["auto"] == "slide":
                self.watch.poll()
            r = self.rec
            if r and time.time() - r["started"] > self.settings["max_hours"] * 3600:
                self.stop("auto: %s hours" % self.settings["max_hours"], error="It stopped by itself after %s hours." % self.settings["max_hours"])
            n += 1
            time.sleep(1)

    def close(self):
        self.running = False
        if self.rec:
            self.stop("the server stopping")
        self.back_to_stage("the server stopping")

    # ---- making room on this computer ----
    def make_room(self, need=MAKE_ROOM, delete=True):
        """This computer is nearly full: delete its oldest recordings that were copied to the recording drive and
        checked (copied.json). A recording only here is never deleted. delete=False only notes whether it's short
        (the setting is off). -> (deleted names, still short of room)"""
        loc = self._local_drive()
        if not loc or loc["free"] >= need:
            self.short = False
            return [], False
        if not delete:
            self.short = True
            return [], True
        base = os.path.join(self.local["root"], FOLDER)
        busy = {self.rec["name"]} if self.rec and self.rec["drive"] == "local" else set()
        if self.player:
            busy.add(self.player.name)
        gone = []
        try:
            names = sorted(os.listdir(base))  # oldest first: the names start with the date and time
        except OSError:
            names = []
        for nm in names:
            if loc["free"] >= need:
                break
            folder = os.path.join(base, nm)
            if nm in busy or not os.path.isfile(os.path.join(folder, COPIED)):
                continue
            try:
                with open(os.path.join(folder, COPIED), encoding="utf-8") as f:
                    c = json.load(f)
                ok = c.get("files") and all(os.path.isfile(os.path.join(folder, x)) and os.path.getsize(os.path.join(folder, x)) == n
                                            for x, n in c["files"].items())
            except (OSError, ValueError, AttributeError):
                ok = False
            if not ok:
                continue  # changed since it was copied: keep it
            shutil.rmtree(folder, ignore_errors=True)
            gone.append(nm)
            self.log.warning("recording: this computer was nearly full, so it deleted %s (it's on %s, copied %s)", nm, c.get("label", "the recording drive"), c.get("at", "?"))
            loc = self._local_drive() or loc
        self.short = loc["free"] < need
        return gone, self.short

    # ---- recordings on the drive ----
    def _local_drive(self):
        try:
            os.makedirs(os.path.join(self.local["root"], FOLDER), exist_ok=True)
            u = shutil.disk_usage(self.local["root"])
        except OSError:
            return None
        return dict(self.local, free=u.free, total=u.total, fs="")

    def _where(self, where):
        """"drive" (the recording drive), "local" (this computer) or a plugged-in drive's id -> that drive, or None."""
        if where == "local":
            return self._local_drive()
        if where in (None, "", "drive"):
            return self.drive
        return next((d for d in self.drives if d["id"] == where), None)

    def sessions(self):
        """What's recorded: on the recording drive, then on this computer (waiting to be copied)."""
        out = [dict(x, where="drive") for x in self._list(self.drive)] if self.drive else []
        return out + [dict(x, where="local") for x in self._list(self.local)]

    def _list(self, d):
        if not d:
            return []
        base = os.path.join(d["root"], FOLDER)
        out = []
        try:
            names = sorted(os.listdir(base), reverse=True)
        except OSError:
            return []
        for nm in names[:50]:
            try:
                with open(os.path.join(base, nm, "session.json"), encoding="utf-8") as f:
                    s = json.load(f)
                out.append({"name": nm, "length": s.get("length", 0), "tracks": len(s.get("tracks") or []),
                            "track_list": [{"dvs": t.get("dvs"), "pair": t.get("pair"), "name": t.get("name", ""), "from": t.get("from", "")}
                                           for t in s.get("tracks") or []],
                            "markers": [m for m in parts(s.get("markers") or [], s.get("length", 0)) if m.get("name") not in ("Start", "End")]})
            except (OSError, ValueError):
                continue
        return out

    def _session(self, name, where="drive"):
        d = self._where(where)
        if not d or not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
            return None, None
        folder = os.path.join(d["root"], FOLDER, name)
        try:
            with open(os.path.join(folder, "session.json"), encoding="utf-8") as f:
                return folder, json.load(f)
        except (OSError, ValueError):
            return None, None

    # ---- playback to the desk (virtual soundcheck) ----
    def card_ins(self):
        """{dvs output number: Wing card input} from Dante's routes (DVS out k -> Wing card in m)."""
        if not self.dante:
            return {}
        try:
            routes = self.dante.all_routes()
        except Exception:
            return {}
        dvs = self.dante.dvs_name()
        wing = (self.cfg.get("dante_wing_name") or "").lower()
        out = {}
        for r in routes:
            if r["tx_device"].lower() != dvs:
                continue
            rx = r["rx_device"].lower()
            if (wing and rx == wing) or (not wing and "wing" in rx):
                try:
                    out[int(re.sub(r"\D", "", r["tx_channel"]) or 0)] = int(re.sub(r"\D", "", r["rx_channel"]) or 0)
                except ValueError:
                    pass
        return out

    def _dante_now(self):
        """(every route, every device's channels, this server's Dante name, the Wing's Dante name), read now."""
        routes, chans = [], {}
        if self.dante:
            try:
                routes = self.dante.all_routes()
            except Exception:
                routes = []
            try:
                chans = self.dante.channels() if hasattr(self.dante, "channels") else \
                    {d: {"tx": t, "rx": {}} for d, t in (self.dante.tx_channels() or {}).items()}
            except Exception:
                chans = {}
        me = ""
        if self.dante:
            try:
                me = (self.dante.get() or {}).get("device_name") or ""
            except Exception:
                me = ""
            me = me or next((d for d in chans if d.lower() == self.dante.dvs_name()), "") or self.dante.dvs_name().upper()
        return routes, chans, me, patching.wing_device(routes, chans, self.cfg)

    def play_plan(self, name, where="drive", send=None):
        """What Play would do, to ask first, and anything that stops it.
        Each track goes back where it was recorded from: every Wing card input that hears that source is pointed at
        this server's output with the track's number instead (steps of kind "dante"). A track recorded from a Wing
        output can't go back that way, so its Wing channel is switched to the card input this server's output is
        routed to (kind "wing", as before). `send` {dvs: "off" | [{"dev", "ch"}, ...]} sends a track somewhere else."""
        folder, s = self._session(name, where)
        if not s:
            return None, "That recording isn't on the drive."
        to = send or {}
        routes, chans, me, wing = self._dante_now()
        wl = wing.lower()
        my_tx = (chans.get(me) or {}).get("tx") or {}
        wing_rx = (chans.get(wing) or {}).get("rx") or {}
        strips, inputs = {}, {}
        if self.desk:
            try:
                st = self.desk.state()
                strips, inputs = (st.get("strips") or {}).get("ch") or {}, st.get("inputs") or {}
            except Exception:
                pass
        into = {(r["rx_device"].lower(), r["rx_channel"]): r for r in routes}
        ins = None
        steps, notes, play = [], [], []
        for t in s.get("tracks") or []:
            dest = to.get(str(t["dvs"]), "source")
            if dest == "off":
                notes.append("%s isn't played (you chose so)." % t["name"])
                continue
            sides = [(t["dvs"], t.get("from_dev") or "", t.get("from_ch") or "")]
            if t.get("pair"):
                sides.append((t["pair"], t.get("from_dev_r") or "", t.get("from_ch_r") or ""))
            mine = []
            for i, (k, fdev, fch) in enumerate(sides):
                if isinstance(dest, list):
                    d = dest[i] if i < len(dest) and isinstance(dest[i], dict) else None
                    targets = [(str(d.get("dev") or ""), str(d.get("ch") or ""))] if d and d.get("dev") and d.get("ch") else []
                elif fdev and wl and fdev.lower() != wl:
                    targets = [(r["rx_device"], r["rx_channel"]) for r in routes
                               if r["tx_device"].lower() == fdev.lower() and r["tx_channel"] == fch and r["rx_device"].lower() == wl]
                else:
                    targets = []
                for rx_dev, rx_ch in targets:
                    cur = into.get((rx_dev.lower(), rx_ch))
                    hears = []
                    if rx_dev.lower() == wl:
                        n = patching._num(rx_ch, wing_rx)
                        hears = ["%d %s" % (c, (strips.get(str(c)) or {}).get("name") or "") for c, _ in patching.channels_on(strips, inputs, "CRD", n or 0)]
                    tx = my_tx.get(k) or "%02d" % k
                    mine.append({"kind": "dante", "track": t["name"], "dvs": k, "rx_dev": rx_dev, "rx_ch": rx_ch, "tx_ch": tx,
                                 "prev_dev": cur["tx_device"] if cur else "", "prev_ch": cur["tx_channel"] if cur else "",
                                 "now": "%s · %s" % (cur["tx_device"], cur["tx_channel"]) if cur else "nothing", "to": "%s · %s" % (me, tx),
                                 "wing": hears, "wing_ch": None,
                                 "text": "%s: %s %s, now %s → C3 Life track %d%s" % (
                                     t["name"], "Wing card in" if rx_dev.lower() == wl else rx_dev, rx_ch,
                                     "%s · %s" % (cur["tx_device"], cur["tx_channel"]) if cur else "nothing", k,
                                     " (Wing ch %s)" % ", ".join(hears) if hears else "")})
            if not mine and not isinstance(dest, list) and t.get("wing_ch"):  # recorded from a Wing output
                if ins is None:
                    ins = self.card_ins()
                n, m = t["wing_ch"], ins.get(t["dvs"])
                if m:
                    cur = strips.get(str(n)) or {}
                    now = "%s/%s" % (cur.get("src_grp", "?"), cur.get("src_in", "?"))
                    mine.append({"kind": "wing", "track": t["name"], "dvs": t["dvs"], "wing_ch": n, "card_in": m, "now": now, "to": "CRD/%d" % m,
                                 "text": "Ch %d · %s: %s → CRD/%d (the recording)" % (n, t["name"], now, m)})
                else:
                    notes.append("%s: DVS out %d isn't routed to the Wing card in Dante Controller, so it's skipped." % (t["name"], t["dvs"]))
                    continue
            if not mine:
                notes.append("%s: nothing on the Wing hears where it was recorded from (%s), so it isn't sent anywhere. Choose where it goes." %
                             (t["name"], t.get("from") or "unknown"))
                continue
            steps += mine
            play.append(t)
        return {"name": name, "steps": steps, "notes": notes, "length": s.get("length", 0), "play": play, "device": me,
                "tracks": [{"dvs": t["dvs"], "pair": t.get("pair"), "name": t["name"], "from": t.get("from", "")} for t in s.get("tracks") or []],
                "markers": [m for m in parts(s.get("markers") or [], s.get("length", 0)) if m.get("name") not in ("Start", "End")]}, None

    def play(self, name, start_at, who, where="drive", end_at=None, loop=False, part="", send=None, segs=None):
        """Play to the desk from start_at; with end_at, only up to there (a song or one section), over and over with loop.
        segs [(from, to, label)]: several parts one after the other instead (all of them looped with loop)."""
        if segs:
            if len(segs) > 50 or any(b is None or b <= a for a, b, _ in segs):
                return "Each part needs a start and an end."
            segs = sorted(segs, key=lambda x: x[0])
            start_at, end_at = segs[0][0], segs[-1][1]
        if end_at is not None and end_at <= start_at:
            return "That part has no length."
        if loop and end_at is None:
            return "Choose a song or a section to loop."
        if self.rec:
            return "Not while recording."
        if self.player:
            return "It's already playing. Press Back to stage first."
        plan, err = self.play_plan(name, where, send)
        if err:
            return err
        if not plan["steps"]:
            return "Nothing in that recording can go to the desk. " + " ".join(plan["notes"])
        if not self.engine or self.engine.status != "running":
            return "C3 Life isn't connected to Dante Virtual Soundcard."
        wsteps = [x for x in plan["steps"] if x["kind"] == "wing"]
        dsteps = [x for x in plan["steps"] if x["kind"] == "dante"]
        if dsteps and self.desk and isinstance(getattr(self.desk, "settings", None), dict) and self.desk.settings.get("mode") != "control":
            return "The Wing is in Watch only. Switch it to Control on the Mixer first."
        strips = (self.desk.state().get("strips") or {}).get("ch") or {} if wsteps else {}
        saved = []
        for st in wsteps:  # remember every input first, then switch: Back to stage puts these back
            cur = strips.get(str(st["wing_ch"])) or {}
            if not cur.get("src_grp"):
                return "The Wing hasn't said what channel %d's input is yet. Try again in a few seconds." % st["wing_ch"]
            saved.append((st["wing_ch"], cur["src_grp"], int(cur.get("src_in") or 0)))
        done = []

        def undo():
            for n, g, x in saved[:len(switched)]:
                self.desk.set_src(n, g, x)
            self._put_routes_back(done)
        switched = []
        for st in wsteps:
            e = self.desk.set_src(st["wing_ch"], "CRD", st["card_in"])
            if e:
                undo()
                return e
            switched.append(st)
        for st in dsteps:
            e = self.dante.route_any(st["rx_dev"], st["rx_ch"], plan["device"], st["tx_ch"])
            if e:
                undo()
                return "%s (%s). Nothing was left changed." % (e, st["text"])
            done.append(st)
        self.stage, self.stage_routes = saved, done
        folder, s = self._session(name, where)
        try:
            marks = [m for m in parts(s.get("markers") or [], s.get("length", 0)) if m.get("end") is not None]
            self.player = Player(folder, s, plan["play"], self.engine, start_at, end_at, loop, segs=segs, marks=marks)
        except OSError as e:
            self.back_to_stage(who)
            return "The recording couldn't be opened (%s)." % e
        self.player.who = who
        if not segs:
            self.player.part = (part or "")[:120]
        self.engine.play(self.player.block)
        self.log.info("recording: %s is playing %s to the desk from %s%s%s (%d tracks, %d Dante routes, %d Wing inputs switched)", who, name,
                      clock(start_at), " to %s" % clock(end_at) if end_at is not None else "", ", looped" if loop else "",
                      len(plan["play"]), len(done), len(saved))
        return None

    def _put_routes_back(self, steps):
        errs = []
        for st in reversed(steps):
            if st["prev_dev"]:
                e = self.dante.route_any(st["rx_dev"], st["rx_ch"], st["prev_dev"], st["prev_ch"])
            else:
                e = self.dante.unroute_any(st["rx_dev"], st["rx_ch"])
            if e:
                errs.append((st, "%s %s back to %s: %s" % (st["rx_dev"], st["rx_ch"], st["now"], e)))
        return errs

    def back_to_stage(self, who):
        p, self.player = self.player, None
        if self.engine:
            self.engine.play(None)
        if p:
            p.close()
        errs = []
        for n, g, x in self.stage:
            e = self.desk.set_src(n, g, x) if self.desk else None
            if e:
                errs.append("channel %d: %s" % (n, e))
        bad = self._put_routes_back(self.stage_routes) if self.stage_routes else []
        errs += [m for _, m in bad]
        if self.stage or self.stage_routes:
            self.log.info("recording: %s put %d channels and %d Dante routes back to the stage", who, len(self.stage), len(self.stage_routes))
        if not errs:
            self.stage = []
        self.stage_routes = [st for st, _ in bad]  # only the ones that didn't go back: pressing it again tries those
        return ("Some channels couldn't be put back: " + "; ".join(errs)) if errs else None

    # ---- the patch: where each of this server's 64 Dante channels comes from ----
    def patch_view(self):
        """Each DVS channel (1-64): where it comes from and what it's called; every Dante source to choose from; the layouts."""
        routes, chans, me, wing = self._dante_now()
        dvs = {}
        if self.dante:
            try:
                dvs = (self.dante.get() or {}).get("channels") or {}
            except Exception:
                dvs = {}
        names = self.names(fresh=True)
        pr = patching.pairs(names, set(self.settings.get("split") or []))
        right = {v: k for k, v in pr.items()}
        ch_count = self.engine.ch if self.engine else 64
        rows = []
        for k in range(1, min(ch_count, 64) + 1):
            r, n = dvs.get(str(k)), names.get(k) or {}
            rows.append({"dvs": k, "dev": r["tx_device"] if r else "", "ch": r["tx_channel"] if r else "", "name": n.get("name", ""),
                         "wing_chs": n.get("wing_chs") or [], "side": n.get("side", ""), "pair": pr.get(k) or right.get(k),
                         "mono": k in (self.settings.get("split") or []), "status": (r or {}).get("status", "")})
        sources = []
        for dev in sorted(chans, key=str.lower):
            if dev == me:
                continue
            for num, nm in sorted(((chans[dev].get("tx") or {}).items())):
                sources.append({"dev": dev, "ch": nm, "num": num})
        receivers = [{"dev": dev, "ch": nm} for dev in sorted(chans, key=str.lower) if dev != me
                     for _, nm in sorted(((chans[dev].get("rx") or {}).items()))]
        return {"device": me, "wing": wing, "rows": rows, "sources": sources, "receivers": receivers, "used": len(dvs), "of": min(ch_count, 64),
                "layouts": self.layouts.list(), "recording": bool(self.rec), "playing": bool(self.player)}

    def patch_route(self, k, dev, ch, who, pair=None):
        """Route a Dante source into DVS channel k (and the right side of a pair into k + 1). -> error or None"""
        if self.rec:
            return "Not while recording: the tracks would change halfway through."
        if self.player:
            return "Not while playing back. Press Back to stage first."
        if not self.dante:
            return "Dante isn't available on this computer."
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= (64 if pair is None else 63):
            return "Choose a channel from 1 to 64."
        for d, c in [(dev, ch)] + ([(pair.get("dev"), pair.get("ch"))] if pair else []):
            if not isinstance(d, str) or not isinstance(c, str) or not d or not c:
                return "Choose a source."
        e = self.dante.route(dev, ch, k)
        if not e and pair:
            e = self.dante.route(pair["dev"], pair["ch"], k + 1)
        if not e:  # a new source pairs again by itself, if it's stereo on the Wing
            gone = {k, k + 1} if pair else {k}
            sp = [x for x in (self.settings.get("split") or []) if x not in gone]
            if sp != (self.settings.get("split") or []):
                with self.lock:
                    self.settings["split"] = sp
                    self._save()
        self.names_cache = (0.0, {})
        if not e:
            self.log.info("recording: %s patched %s · %s%s into C3 Life channel %d%s", who, dev, ch,
                          " and %s · %s" % (pair["dev"], pair["ch"]) if pair else "", k, " and %d" % (k + 1) if pair else "")
        return e

    def patch_clear(self, k, who):
        if self.rec or self.player:
            return "Not while recording or playing back."
        if not self.dante:
            return "Dante isn't available on this computer."
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 64:
            return "Choose a channel from 1 to 64."
        e = self.dante.unroute(k)
        self.names_cache = (0.0, {})
        if not e:
            self.log.info("recording: %s cleared C3 Life channel %d", who, k)
        return e

    def patch_mono(self, k, mono, who):
        """Keep DVS channel k (one side of a stereo Wing channel) as a mono track, or let it pair again."""
        sp = [x for x in (self.settings.get("split") or []) if x != k]
        if mono:
            sp.append(k)
        return self.save_settings({"split": sorted(sp)}, who)

    def layout_plan(self, name):
        lay = self.layouts.get(name)
        if lay is None:
            return None, "That layout isn't there."
        now = (self.dante.get() or {}).get("channels") or {} if self.dante else {}
        return patching.layout_changes(lay, now), None

    def layout_apply(self, name, who):
        """Put this server's channels back as the layout had them. Stops at the first route Dante refuses. -> error or None"""
        if self.rec or self.player:
            return "Not while recording or playing back."
        ch, err = self.layout_plan(name)
        if err:
            return err
        for c in ch:
            e = self.dante.route(c["dev"], c["ch"], c["dvs"]) if c["dev"] else self.dante.unroute(c["dvs"])
            if e:
                self.names_cache = (0.0, {})
                return "Channel %d: %s. The ones before it were changed." % (c["dvs"], e)
        self.names_cache = (0.0, {})
        self.log.info("recording: %s put the %s layout back (%d changes)", who, name, len(ch))
        return None

    def layout_save(self, name, who):
        now = (self.dante.get() or {}).get("channels") or {} if self.dante else {}
        e = self.layouts.save(name, now, who)
        if not e:
            self.log.info("recording: %s saved the patch as %s", who, name)
        return e

    # ---- copying recordings to a drive ----
    def copy(self, items, to, who):
        """Copy recordings [(name, where)] onto drive `to` (its id), one after another, in the background. Each copy is
        checked (every file the same size); one from this computer is then deleted from it if free_local is on."""
        dest = self._where(to)
        if not dest or dest["id"] == "local":
            return "That drive isn't plugged in."
        with self.job_lock:
            if self.job and not self.job.get("finished"):
                return "It's already copying. Wait for it to finish."
            if self.rec and self.rec["drive"] == dest["id"]:
                return "Not while recording onto that drive."
            todo = []
            for name, where in items:
                folder, s = self._session(name, where)
                if not s:
                    return "%s isn't there any more." % name
                todo.append((name, where, folder))
            total = sum(os.path.getsize(os.path.join(f, x)) for _, _, f in todo for x in os.listdir(f))
            if total > dest["free"] - LOW_SPACE:
                return "There isn't room on %s (%s needed, %s free)." % (dest["label"], gb(total), gb(dest["free"]))
            self.job = {"names": [n for n, _, _ in todo], "to": dest["label"], "done": 0, "total": total, "error": "", "finished": False, "by": who}
        threading.Thread(target=self._copy, args=(self.job, todo, dest), daemon=True).start()
        self.log.info("recording: %s is copying %d recording(s) to %s", who, len(todo), dest["label"])
        return None

    def _copy(self, job, todo, dest):
        # onto the recording drive: its C3 Life Recordings folder. Onto any other drive (to take home): a folder with
        # another name, so that drive never looks like a recording drive the next time it's plugged in
        sub = FOLDER if dest.get("ok") else COPIES
        for name, where, folder in todo:
            target = os.path.join(dest["root"], sub, name)
            n = 2
            while os.path.exists(target):  # never over another recording
                target = os.path.join(dest["root"], sub, "%s (%d)" % (name, n))
                n += 1
            try:
                os.makedirs(target)
                files = {}
                for x in sorted(os.listdir(folder)):
                    if x == COPIED:
                        continue
                    src, dst = os.path.join(folder, x), os.path.join(target, x)
                    with open(src, "rb") as a, open(dst, "wb") as b:
                        while True:
                            buf = a.read(1 << 20)
                            if not buf:
                                break
                            b.write(buf)
                            job["done"] += len(buf)
                        b.flush()
                        os.fsync(b.fileno())
                    if os.path.getsize(dst) != os.path.getsize(src):
                        raise OSError("%s didn't copy fully" % x)
                    files[x] = os.path.getsize(dst)
            except OSError as e:
                job["error"] = "%s didn't copy to %s (%s). It's still on %s." % (name, dest["label"], e, "this computer" if where == "local" else "the drive")
                self.log.error("recording: %s", job["error"])
                break
            if where == "local" and self.settings["free_local"]:
                shutil.rmtree(folder, ignore_errors=True)
            elif where == "local" and dest.get("ok"):  # kept here: remember it's safely on the recording drive (make_room)
                try:
                    with open(os.path.join(folder, COPIED), "w", encoding="utf-8") as f:
                        json.dump({"drive": dest["id"], "label": dest["label"], "folder": os.path.basename(target),
                                   "at": time.strftime("%Y-%m-%d %H:%M"), "files": files}, f, indent=1)
                except OSError:
                    pass
            self.log.info("recording: copied %s to %s", name, dest["label"])
        job["finished"] = True

    # ---- what the screen shows ----
    def view(self, full=False):
        r, p = self.rec, self.player
        out = {"settings": self.settings, "drive": self._drive_view(self.drive), "recording": None, "last": self.last,
               "playing": None, "on_stage": not self.stage and not self.stage_routes, "engine": self.engine.view() if self.engine else {"status": "off"},
               "slide": self.slide, "propresenter": self.watch.error, "copy": self.job, "short": self.short}
        if r:
            out["recording"] = {"name": r["name"], "by": r["by"], "where": r["drive_label"], "secs": round(self.now(), 1), "tracks": len(r["tracks"]),
                                "markers": [m for m in r["markers"] if m["name"] != "Start"], "dropped": r["dropped"], "error": r["error"]}
        if p:
            out["playing"] = {"name": p.name, "secs": round(p.pos / p.rate, 1), "length": p.length, "by": getattr(p, "who", ""),
                              "channels": len(p.steps), "ended": p.ended, "from": round(p.start / p.rate, 2),
                              "to": round(p.end / p.rate, 2) if p.end is not None else None, "loop": p.loop, "part": getattr(p, "part", ""),
                              "parts": len(p.segs), "can_jump": bool(p.marks) or len(p.segs) > 1}
        if full:
            out["drives"] = [self._drive_view(d) for d in self.drives]
            ch = self.engine.ch if self.engine else 64
            tracks = (r["tracks"] if r else self.tracks_for(ch))
            lv = self.engine.peaks() if self.engine else []
            lvl = lambda k: round(lv[k - 1], 3) if k and k - 1 < len(lv) else 0
            out["tracks"] = [{"dvs": t["dvs"], "pair": t.get("pair"), "name": t["name"], "wing_ch": t["wing_ch"], "from": t.get("from", ""),
                              "level": max(lvl(t["dvs"]), lvl(t.get("pair")))} for t in tracks]
            out["sessions"] = self.sessions()
            rate = (self.engine.rate if self.engine else 48000) * 3 * max(sum(2 if t.get("pair") else 1 for t in tracks), 1)
            if self.drive:
                out["drive"]["hours_left"] = round(self.drive["free"] / rate / 3600, 1)
            loc = self._local_drive() if self.settings["local"] else None
            out["local"] = {"free_text": gb(loc["free"]), "hours_left": round(loc["free"] / rate / 3600, 1), "short": self.short} if loc else None
        return out

    @staticmethod
    def _drive_view(d):
        if not d:
            return None
        return {"id": d["id"], "label": d["label"], "free": d["free"], "total": d["total"], "ok": d.get("ok", False), "fs": d.get("fs", ""),
                "free_text": gb(d["free"]), "fat32": d.get("fs", "").upper() == "FAT32"}


def gb(n):
    return "%.0f GB" % (n / 1e9) if n >= 10e9 else "%.1f GB" % (n / 1e9)


def write_session(f, s):
    """A recording's session.json, markers.csv, Audacity labels (markers.txt) and REAPER project."""
    tracks, length, rate = s.get("tracks") or [], s.get("length", 0), s.get("rate", 48000)
    with open(os.path.join(f, "markers.csv"), "w", encoding="utf-8") as h:
        h.write("time,seconds,name,type,song\n")
        for m in s["markers"]:
            h.write('%s,%.2f,"%s",%s,"%s"\n' % (clock(m["t"]), m["t"], m["name"].replace('"', "'"), kind(m) or "marker",
                                                 (m.get("song") or "").replace('"', "'")))
    with open(os.path.join(f, "markers.txt"), "w", encoding="utf-8") as h:  # Audacity labels: a section is a region
        for m in parts(s["markers"], length):
            end = m["end"] if m["kind"] == "section" else m["t"]
            h.write("%.3f\t%.3f\t%s\n" % (m["t"], end, ("%s: %s" % (m["song"], m["name"])) if m.get("song") else m["name"]))
    with open(os.path.join(f, s["name"] + ".RPP"), "w", encoding="utf-8") as h:
        h.write(rpp(s["name"], rate, tracks, s["markers"], length))
    tmp = os.path.join(f, "session.json.tmp")  # last, and all at once: never half a file, even if it stops halfway
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(s, h, indent=1)
    os.replace(tmp, os.path.join(f, "session.json"))


class Player:
    """Reads a recording's tracks ahead, a block at a time, for the engine to send out (each on its DVS out number).
    It plays a list of parts one after the other (`segs`: [(from s, to s or None, label)]), over and over with loop,
    and can jump to the next or previous part while it plays."""

    def __init__(self, folder, s, steps, engine, start_at, end_at=None, loop=False, segs=None, marks=None):
        self.name, self.rate, self.steps = s.get("name", ""), int(s.get("rate", 48000)), steps
        self.length = s.get("length", 0)
        self.out_ch = getattr(engine, "out_ch", engine.ch)
        segs = segs or [(start_at, end_at, "")]
        fr = lambda x: max(0, int(float(x or 0) * self.rate))
        self.segs = [(fr(a), max(fr(a) + 1, fr(b)) if b is not None else None, lab or "") for a, b, lab in segs]
        self.loop = bool(loop) and all(e is not None for _, e, _ in self.segs)
        self.marks = marks or []  # the recording's songs and sections: [{"t", "end", "name", "kind", "song"}], to jump between
        self.files = []
        by_dvs = {t["dvs"]: t for t in s.get("tracks") or []}
        for st in {x["dvs"]: x for x in steps}.values():  # each track once (a step per Wing input it goes to)
            t = by_dvs[st["dvs"]]
            f = open(os.path.join(folder, t["file"]), "rb")
            outs = [t["dvs"] - 1] + ([t["pair"] - 1] if t.get("pair") else [])
            self.files.append((outs, f))
        self.si = 0
        self.lock = threading.Lock()
        self._seek(self.segs[0][0])
        self.pos, self.ended = self.rpos, False
        self.q = queue.Queue(50)
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    # what the screen shows: the part being heard now (the reader is a little ahead of it)
    @property
    def heard(self):
        return next((i for i, (a, e, _) in enumerate(self.segs) if a <= self.pos <= (e if e is not None else float("inf"))), self.si)

    @property
    def start(self):
        return self.segs[self.heard][0]

    @property
    def end(self):
        return self.segs[self.heard][1]

    @property
    def part(self):
        i = self.heard
        lab = self.segs[i][2]
        return "%s (%d of %d)" % (lab, i + 1, len(self.segs)) if len(self.segs) > 1 and lab else lab

    @part.setter
    def part(self, v):  # play() names a single part
        if len(self.segs) == 1:
            a, b, _ = self.segs[0]
            self.segs[0] = (a, b, v or "")

    def _seek(self, frame):
        self.rpos = frame
        for cs, f in self.files:
            f.seek(44 + 3 * len(cs) * frame)

    def _read(self):
        from audio_engine import BLOCK
        while self.alive:
            with self.lock:
                if not self.alive:
                    return
                bufs = [bytearray() for _ in self.files]
                need, done, turns = BLOCK, False, 0
                while need > 0 and not done:
                    a, e, _ = self.segs[self.si]
                    n = need if e is None else max(0, min(need, e - self.rpos))
                    got = n
                    for i, (cs, f) in enumerate(self.files):
                        d = f.read(n * 3 * len(cs)) if n else b""
                        bufs[i] += d
                        got = min(got, len(d) // (3 * len(cs)))
                    if not self.files:
                        got = n
                    self.rpos += got
                    need -= got
                    if need > 0:  # this part is over (or the file is): the next part, round again, or the end
                        turns += 1
                        if self.si + 1 < len(self.segs):
                            self.si += 1
                        elif self.loop and turns <= len(self.segs) + 1:
                            self.si = 0
                        else:
                            done = True
                            continue
                        self._seek(self.segs[self.si][0])
                rpos = self.rpos
            if need == BLOCK:  # nothing at all
                self.ended = True
                self.q.put(None)
                return
            out = bytearray(BLOCK * 4 * self.out_ch)
            for (cs, _), data in zip(self.files, bufs):
                for side, c in enumerate(cs):
                    if c < self.out_ch and data:
                        put24(out, part24(data, side) if len(cs) == 2 else data, c, self.out_ch)
            self.q.put((bytes(out), rpos))

    def jump(self, step):
        """Next (+1) or previous (-1) part while it plays -> (error or None, the part's name)."""
        now, name = self.pos / self.rate, ""
        with self.lock:
            if len(self.segs) > 1:  # several parts ticked: move between them
                h = self.heard
                i = h + step if step > 0 or now - self.segs[h][0] / self.rate < 2 else h
                if not 0 <= i < len(self.segs):
                    if not self.loop:
                        return ("That's the last part." if step > 0 else "That's the first part."), ""
                    i %= len(self.segs)
                self.si = i
                self._seek(self.segs[i][0])
            else:
                a, e, lab = self.segs[0]
                one = e is not None
                cur = next((m for m in self.marks if m.get("end") is not None and m["t"] <= now < m["end"] and m.get("kind") == "section"), None) or \
                    next((m for m in self.marks if m.get("end") is not None and m["t"] <= now < m["end"]), None)
                kind = (cur or {}).get("kind") or "section"
                pool = sorted([m for m in self.marks if m.get("end") is not None and m.get("kind") == kind] or
                              [m for m in self.marks if m.get("end") is not None], key=lambda m: m["t"])
                if not pool:
                    return "This recording has no songs or parts to jump between.", ""
                if step > 0:
                    nxt = next((m for m in pool if m["t"] > now + 0.05), None)
                else:
                    here = next((m for m in reversed(pool) if m["t"] <= now + 0.01), None)
                    nxt = here if here and now - here["t"] >= 2 else next((m for m in reversed(pool) if m["t"] < (here or {"t": now})["t"] - 0.01), here)
                if not nxt:
                    return ("That's the last part." if step > 0 else "That's the first part."), ""
                name = ("%s: %s" % (nxt["song"], nxt["name"])) if nxt.get("song") else nxt["name"]
                if one:  # playing just one part (or looping it): play this one instead, the same way
                    self.segs[0] = (int(nxt["t"] * self.rate), int(nxt["end"] * self.rate), name)
                self._seek(int(nxt["t"] * self.rate))
            self.pos = self.rpos
            label = self.part
        try:  # throw away what was read ahead, so the jump is heard straight away
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.pos = self.rpos
        return None, label or name

    def block(self, frames):
        try:
            b = self.q.get_nowait()
        except queue.Empty:
            return None  # the drive is behind: a moment of silence
        if b is None:
            return None
        self.pos = b[1]
        return b[0]

    def close(self):
        self.alive = False
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        with self.lock:  # not while the reader is in the middle of a read
            for _, f in self.files:
                f.close()
