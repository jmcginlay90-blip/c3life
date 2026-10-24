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

FOLDER = "C3 Life Recordings"
KINDS = ("presentation", "group", "label")
DEFAULTS = {"auto": "off",                 # "off" | "slide" (the start slide) | "plug" (as soon as the drive is plugged in)
            "start": {"kind": "presentation", "text": ""},
            "stop": {"kind": "presentation", "text": ""},
            "max_hours": 4,                # stops by itself after this long, if nothing else has
            "markers": "presentation",     # "presentation" | "item" (the playlist item's name) | "off"
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
    """A 24-bit mono WAV that's always playable: the header's sizes are brought up to date every few seconds."""

    def __init__(self, path, rate):
        self.path, self.rate, self.bytes = path, rate, 0
        self.f = open(path, "wb")
        self.f.write(self._header())
        self.last = time.time()

    def _header(self):
        n = self.bytes
        return b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, self.rate, self.rate * 3, 3, 24) + \
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


def rpp(name, rate, tracks, markers, length):
    """A REAPER project: each track with its file, each marker."""
    q = lambda s: '"%s"' % s.replace('"', "'")
    lines = ["<REAPER_PROJECT 0.1 \"6.0\" 0", "  SAMPLERATE %d 0 0" % rate]
    for i, m in enumerate(markers, 1):
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
    if not all(isinstance(s.get(k), bool) for k in ("local", "copy_auto", "free_local", "make_room")):
        return "Settings not understood."
    if s.get("markers") not in ("presentation", "item", "off"):
        return "Markers: presentation, playlist item or off."
    c = s.get("channels")
    if c != "routed" and not (isinstance(c, list) and 0 < len(c) <= 64 and len(set(c)) == len(c) and
                              all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 64 for n in c)):
        return "Channels: the routed ones, or a list of DVS channels 1-64."
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
        """{dvs channel: {"name", "wing_ch", "src", "from"}} for every DVS channel with a Dante route."""
        t, cached = self.names_cache
        if not fresh and time.time() - t < 30:
            return cached
        out = {}
        routes = {}
        if self.dante:
            try:
                routes = (self.dante.get() or {}).get("channels") or {}
            except Exception:
                routes = {}
        tx = {}
        if routes and self.dante and any("wing" in (r.get("tx_device") or "").lower() for r in routes.values()):
            try:
                tx = self.dante.tx_channels()
            except Exception:
                tx = {}
        strips, outs = {}, {}
        if self.desk:
            try:
                strips = (self.desk.state().get("strips") or {}).get("ch") or {}
                outs = self.desk.outputs() if hasattr(self.desk, "outputs") else {}
            except Exception:
                pass
        wing_name = (self.cfg.get("dante_wing_name") or "").lower()
        waiting = False
        for k, r in routes.items():
            if not str(k).isdigit():
                continue
            dev, chn = r.get("tx_device") or "", str(r.get("tx_channel") or "")
            item = {"name": "%s %s" % (dev, chn), "wing_ch": None, "src": "", "from": "%s · %s" % (dev, chn)}
            if (wing_name and dev.lower() == wing_name) or (not wing_name and "wing" in dev.lower()):
                num = next((n for n, nm in (tx.get(dev) or {}).items() if str(nm) == chn), None)
                if num is None and chn.isdigit():
                    num = int(chn)
                o = outs.get("CRD/%d" % num) if num else None
                if o and o.get("grp") == "OFF":
                    item["name"] = "Wing card out %d (off)" % num
                elif o and o.get("grp"):
                    grp, idx = o["grp"], o.get("in")
                    item["src"] = "%s/%s" % (grp, idx)
                    for n, s in strips.items():
                        if s.get("src_grp") == grp and s.get("src_in") == idx:
                            item.update(name=s.get("name") or "Ch %s" % n, wing_ch=int(n))
                            break
                    else:
                        from rta_patch import _src_text
                        item["name"] = _src_text(grp, int(idx or 0))
                elif num:
                    item["name"] = "Wing card out %d" % num
                    waiting = True  # the Wing hasn't said what this output sends yet
            out[int(k)] = item
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
        out = []
        for c in chans:
            nm = (names.get(c) or {}).get("name") or "DVS %d" % c
            out.append({"dvs": c, "name": nm, "wing_ch": (names.get(c) or {}).get("wing_ch"), "src": (names.get(c) or {}).get("src", ""),
                        "file": "%02d %s.wav" % (c, safe_name(nm))})
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
                files = [Wav(os.path.join(folder, t["file"]), self.engine.rate) for t in tracks]
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
                    w.write(split24(data, t["dvs"] - 1, ch))
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

    def marker(self, name, who, quiet=False):
        name = (name or "").strip()[:80]
        if not name:
            return "A marker needs a name."
        with self.lock:
            r = self.rec
            if not r:
                return "It isn't recording."
            r["markers"].append({"t": round(self.now(), 2), "name": name, "by": who})
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
        tracks = [{k: t[k] for k in ("dvs", "name", "wing_ch", "src", "file")} for t in r["tracks"]]
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
        f = r["folder"]
        with open(os.path.join(f, "session.json"), "w", encoding="utf-8") as h:
            json.dump({"name": r["name"], "rate": r["rate"], "length": round(length, 3), "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"])),
                       "by": r["by"], "tracks": tracks, "markers": r["markers"]}, h, indent=1)
        with open(os.path.join(f, "markers.csv"), "w", encoding="utf-8") as h:
            h.write("time,seconds,name\n")
            for m in r["markers"]:
                h.write('%s,%.2f,"%s"\n' % (clock(m["t"]), m["t"], m["name"].replace('"', "'")))
        with open(os.path.join(f, "markers.txt"), "w", encoding="utf-8") as h:  # Audacity labels
            for m in r["markers"]:
                h.write("%.3f\t%.3f\t%s\n" % (m["t"], m["t"], m["name"]))
        with open(os.path.join(f, r["name"] + ".RPP"), "w", encoding="utf-8") as h:
            h.write(rpp(r["name"], r["rate"], tracks, r["markers"], length))

    # ---- ProPresenter: start, stop and markers ----
    def _on_slide(self, slide):
        self.slide = slide
        st = self.settings
        if self.rec:
            if matches(st["stop"], slide):
                self.stop("auto: the end slide (%s)" % (slide.get(st["stop"]["kind"]) or ""))
                return
            if st["markers"] != "off":  # a new song (or the Message): a marker; more slides of the same one: none
                name = slide.get("item" if st["markers"] == "item" else "presentation") or slide.get("presentation") or ""
                if name and not self._last_marker_is(name):
                    self.marker(name, "ProPresenter")
        elif st["auto"] == "slide" and matches(st["start"], slide) and not self.player:
            err = self.start("auto: the start slide (%s)" % (slide.get(st["start"]["kind"]) or ""))
            if err:
                self.log.warning("recording: the start slide came up but it couldn't start: %s", err)
            elif st["markers"] != "off":
                name = slide.get("item" if st["markers"] == "item" else "presentation") or slide.get("presentation") or ""
                if name:
                    self.marker(name, "ProPresenter")

    def _last_marker_is(self, name):
        r = self.rec
        ms = [m for m in (r["markers"] if r else []) if m["name"] not in ("Start", "End")]
        return bool(ms) and ms[-1]["name"] == name

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
                            "markers": [m for m in s.get("markers") or [] if m.get("name") not in ("Start", "End")]})
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

    def play_plan(self, name, where="drive"):
        """What Play would do, to ask first: [{"track", "wing_ch", "now", "to"}], and anything that stops it."""
        folder, s = self._session(name, where)
        if not s:
            return None, "That recording isn't on the drive."
        ins = self.card_ins()
        strips = {}
        if self.desk:
            try:
                strips = (self.desk.state().get("strips") or {}).get("ch") or {}
            except Exception:
                pass
        steps, notes = [], []
        for t in s.get("tracks") or []:
            n = t.get("wing_ch")
            if not n:
                notes.append("%s isn't from a Wing channel, so it isn't sent to the desk." % t["name"])
                continue
            m = ins.get(t["dvs"])
            if not m:
                notes.append("%s: DVS out %d isn't routed to the Wing card in Dante Controller, so it's skipped." % (t["name"], t["dvs"]))
                continue
            cur = strips.get(str(n)) or {}
            steps.append({"track": t["name"], "dvs": t["dvs"], "wing_ch": n, "card_in": m,
                          "now": "%s/%s" % (cur.get("src_grp", "?"), cur.get("src_in", "?")), "to": "CRD/%d" % m})
        return {"name": name, "steps": steps, "notes": notes, "length": s.get("length", 0),
                "markers": [m for m in s.get("markers") or [] if m.get("name") not in ("Start", "End")]}, None

    def play(self, name, start_at, who, where="drive"):
        if self.rec:
            return "Not while recording."
        if self.player:
            return "It's already playing. Press Back to stage first."
        plan, err = self.play_plan(name, where)
        if err:
            return err
        if not plan["steps"]:
            return "Nothing in that recording can go to the desk. " + " ".join(plan["notes"])
        if not self.engine or self.engine.status != "running":
            return "C3 Life isn't connected to Dante Virtual Soundcard."
        strips = (self.desk.state().get("strips") or {}).get("ch") or {}
        saved = []
        for st in plan["steps"]:  # remember every input first, then switch: Back to stage puts these back
            cur = strips.get(str(st["wing_ch"])) or {}
            if not cur.get("src_grp"):
                return "The Wing hasn't said what channel %d's input is yet. Try again in a few seconds." % st["wing_ch"]
            saved.append((st["wing_ch"], cur["src_grp"], int(cur.get("src_in") or 0)))
        for i, st in enumerate(plan["steps"]):
            e = self.desk.set_src(st["wing_ch"], "CRD", st["card_in"])
            if e:
                for n, g, x in saved[:i]:
                    self.desk.set_src(n, g, x)
                return e
        self.stage = saved
        folder, s = self._session(name, where)
        try:
            self.player = Player(folder, s, plan["steps"], self.engine, start_at)
        except OSError as e:
            self.back_to_stage(who)
            return "The recording couldn't be opened (%s)." % e
        self.player.who = who
        self.engine.play(self.player.block)
        self.log.info("recording: %s is playing %s to the desk from %s (%d channels)", who, name, clock(start_at), len(plan["steps"]))
        return None

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
        if self.stage:
            self.log.info("recording: %s put %d channels back to the stage", who, len(self.stage))
        if not errs:
            self.stage = []
        return ("Some channels couldn't be put back: " + "; ".join(errs)) if errs else None

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
               "playing": None, "on_stage": not self.stage, "engine": self.engine.view() if self.engine else {"status": "off"},
               "slide": self.slide, "propresenter": self.watch.error, "copy": self.job, "short": self.short}
        if r:
            out["recording"] = {"name": r["name"], "by": r["by"], "where": r["drive_label"], "secs": round(self.now(), 1), "tracks": len(r["tracks"]),
                                "markers": [m for m in r["markers"] if m["name"] != "Start"], "dropped": r["dropped"], "error": r["error"]}
        if p:
            out["playing"] = {"name": p.name, "secs": round(p.pos / p.rate, 1), "length": p.length, "by": getattr(p, "who", ""),
                              "channels": len(p.steps), "ended": p.ended}
        if full:
            out["drives"] = [self._drive_view(d) for d in self.drives]
            ch = self.engine.ch if self.engine else 64
            tracks = (r["tracks"] if r else self.tracks_for(ch))
            lv = self.engine.peaks() if self.engine else []
            out["tracks"] = [{"dvs": t["dvs"], "name": t["name"], "wing_ch": t["wing_ch"],
                              "level": round(lv[t["dvs"] - 1], 3) if t["dvs"] - 1 < len(lv) else 0} for t in tracks]
            out["sessions"] = self.sessions()
            rate = (self.engine.rate if self.engine else 48000) * 3 * max(len(tracks), 1)
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


class Player:
    """Reads a recording's tracks ahead, a block at a time, for the engine to send out (each on its DVS out number)."""

    def __init__(self, folder, s, steps, engine, start_at):
        self.name, self.rate, self.steps = s.get("name", ""), int(s.get("rate", 48000)), steps
        self.length = s.get("length", 0)
        self.out_ch = getattr(engine, "out_ch", engine.ch)
        self.files = []
        by_dvs = {t["dvs"]: t for t in s.get("tracks") or []}
        start = max(0, int(float(start_at or 0) * self.rate))
        for st in steps:
            t = by_dvs[st["dvs"]]
            f = open(os.path.join(folder, t["file"]), "rb")
            f.seek(44 + 3 * start)
            self.files.append((st["dvs"] - 1, f))
        self.pos, self.ended = start, False
        self.q = queue.Queue(50)
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        from audio_engine import BLOCK
        while self.alive:
            out = bytearray(BLOCK * 4 * self.out_ch)
            got = 0
            for c, f in self.files:
                if c >= self.out_ch:
                    continue
                mono = f.read(BLOCK * 3)
                got = max(got, len(mono))
                if mono:
                    put24(out, mono, c, self.out_ch)
            if not got:
                self.ended = True
                self.q.put(None)
                return
            self.q.put(bytes(out))

    def block(self, frames):
        try:
            b = self.q.get_nowait()
        except queue.Empty:
            return None  # the drive is behind: a moment of silence
        if b is None:
            return None
        self.pos += frames
        return b

    def close(self):
        self.alive = False
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        for _, f in self.files:
            f.close()
