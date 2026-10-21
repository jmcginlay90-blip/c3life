"""Listen: send any Dante audio on the C3 Life server to phones, tablets and room players, around the building
or (through a private network such as Tailscale) from home.

Audio comes in through Dante Virtual Soundcard. Each *source* is one or two DVS channels. When the first
person starts listening, ffmpeg turns those channels into an MP3 stream; every listener gets the same
stream, and ffmpeg stops 30 seconds after the last one leaves. MP3 over plain HTTP plays in every phone
browser, keeps playing with the screen locked, and is about 1 to 3 seconds behind the room.

Who hears what is set by an Admin:
  sources  {id, name, ch: [dvs channel] or [left, right]}
  groups   {id, name, sources: [source ids], members: [names], home: bool}
  rooms    {id, name, source: source id or ""}       a room player plays its room's source
"""

import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time

ID_RE = re.compile(r"^[a-z0-9-]{1,24}$")
IDLE_STOP_S = 30
CHUNK = 4096

DEFAULT_LISTEN = {
    "sources": [
        {"id": "main", "name": "Main mix", "ch": [4, 5]},
        # The stream mix has songs in it, so its subtitles are made from the speech mics instead.
        {"id": "stream", "name": "Stream mix", "ch": [6, 7], "captions_from": "speech"},
        {"id": "foyer", "name": "Foyer", "ch": [8]},
        {"id": "speech", "name": "Pastor and speech mics", "ch": [9], "captions": True},
    ],
    "groups": [
        {"id": "creche", "name": "Crèche team", "sources": ["main"], "members": [], "home": False},
        {"id": "kids", "name": "Kids church", "sources": ["main", "speech"], "members": [], "home": False},
        {"id": "welcome", "name": "Welcome team", "sources": ["foyer", "main"], "members": ["Foyer team"], "home": False},
        {"id": "home", "name": "Listen from home", "sources": ["stream"], "members": [], "home": True},
        # Anyone on the church Wi-Fi can hear this group's sources without signing in (an Admin can untick it).
        {"id": "everyone", "name": "Everyone (no sign-in)", "sources": ["stream"], "members": [], "home": False, "open": True},
    ],
    "rooms": [
        {"id": "creche", "name": "Crèche", "source": "main"},
        {"id": "kids", "name": "Kids room", "source": "speech"},
        {"id": "overflow", "name": "Overflow hall", "source": "stream"},
    ],
    # The Listener account (no PIN) is for helpers who only listen. An Admin changes what it hears on Users & PINs.
    "people": {"Listener": {"sources": ["main"], "fixed": ""}},
}


def valid_config(c):
    """Return an error message, or "" if the listening setup makes sense."""
    if not isinstance(c, dict):
        return "Not understood."
    src, grp, rooms = c.get("sources"), c.get("groups"), c.get("rooms")
    if not all(isinstance(x, list) for x in (src, grp, rooms)):
        return "Sources, groups and rooms must be lists."
    if len(src) > 16 or len(grp) > 30 or len(rooms) > 30:
        return "Too many (16 sources, 30 groups, 30 rooms at most)."
    ids = set()
    for s in src:
        if not isinstance(s, dict) or not ID_RE.match(str(s.get("id", ""))) or not isinstance(s.get("name"), str) or not 0 < len(s["name"]) <= 40:
            return "Each source needs a short id and a name of up to 40 letters."
        ch = s.get("ch")
        if not isinstance(ch, list) or len(ch) not in (1, 2) or not all(isinstance(n, int) and 1 <= n <= 64 for n in ch):
            return "%s: choose one channel (mono) or two (left, right), from 1 to 64." % s["name"]
        if s["id"] in ids:
            return "Two sources share the id %s." % s["id"]
        if not isinstance(s.get("captions", False), bool):
            return "%s: subtitles must be on or off." % s["name"]
        if not isinstance(s.get("dante", False), bool):
            return "%s: name from Dante must be on or off." % s["name"]
        if not isinstance(s.get("captions_from", ""), str) or len(s.get("captions_from", "")) > 24:
            return "%s: subtitles from another source isn't understood." % s["name"]
        ids.add(s["id"])
    for s in src:
        f = s.get("captions_from", "")
        if f and (f not in ids or f == s["id"]):
            return "%s: subtitles come from a source that doesn't exist." % s["name"]
    for g in grp:
        if not isinstance(g, dict) or not ID_RE.match(str(g.get("id", ""))) or not isinstance(g.get("name"), str) or not 0 < len(g["name"]) <= 40:
            return "Each group needs a short id and a name of up to 40 letters."
        if not isinstance(g.get("sources"), list) or any(x not in ids for x in g["sources"]):
            return "%s: a source it lists doesn't exist." % g["name"]
        if not isinstance(g.get("members"), list) or not all(isinstance(m, str) and len(m) <= 40 for m in g["members"]):
            return "%s: members must be names." % g["name"]
        if not isinstance(g.get("home", False), bool):
            return "%s: home must be yes or no." % g["name"]
        if not isinstance(g.get("open", False), bool):
            return "%s: open to everyone must be yes or no." % g["name"]
    for r in rooms:
        if not isinstance(r, dict) or not ID_RE.match(str(r.get("id", ""))) or not isinstance(r.get("name"), str) or not 0 < len(r["name"]) <= 40:
            return "Each room needs a short id and a name of up to 40 letters."
        if r.get("source", "") not in ids and r.get("source", "") != "":
            return "%s: its source doesn't exist." % r["name"]
    return valid_people(c.get("people", {}), ids)


def valid_people(people, ids):
    """What each person may listen to, set by an Admin on Users & PINs: {name: {"sources": [...], "fixed": id or ""}}."""
    if not isinstance(people, dict) or len(people) > 500:
        return "People not understood."
    for name, p in people.items():
        if not isinstance(name, str) or not 0 < len(name) <= 40 or not isinstance(p, dict):
            return "People not understood."
        if not isinstance(p.get("sources", []), list) or any(x not in ids for x in p.get("sources", [])):
            return "%s: a source they're given doesn't exist." % name
        if p.get("fixed", "") not in ids and p.get("fixed", "") != "":
            return "%s: the one source they're kept to doesn't exist." % name
    return ""


class Source:
    """One running stream: a single ffmpeg, shared by everyone listening to it."""

    def __init__(self, sid, cmd, log):
        self.sid, self.cmd, self.log = sid, cmd, log
        self.listeners = set()
        self.lock = threading.Lock()
        self.proc = None
        self.idle_since = None

    def add(self):
        q = queue.Queue(maxsize=64)  # about 16 s of audio; a listener that falls further behind is dropped
        with self.lock:
            self.listeners.add(q)
            self.idle_since = None
            if not self.proc or self.proc.poll() is not None:
                self.start()
        return q

    def remove(self, q):
        with self.lock:
            self.listeners.discard(q)
            if not self.listeners:
                self.idle_since = time.time()

    def start(self):
        self.log.info("listen: starting %s", self.sid)
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                         creationflags=(0x08000000 if os.name == "nt" else 0))
        except OSError as e:
            self.log.error("listen: can't start ffmpeg for %s: %s", self.sid, e)
            self.proc = None
            return
        threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def pump(self, proc):
        while True:
            data = proc.stdout.read(CHUNK)
            if not data:
                break
            with self.lock:
                for q in list(self.listeners):
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        self.listeners.discard(q)
                        q.put(None)
        with self.lock:
            for q in list(self.listeners):
                q.put(None)
            self.listeners.clear()

    def stop(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            self.log.info("listen: stopping %s (nobody listening)", self.sid)
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    @property
    def live(self):
        return bool(self.proc and self.proc.poll() is None)


class Listen:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.path = os.path.join(data_dir, "listen.json")
        self.rooms_path = os.path.join(data_dir, "listen-rooms.json")
        self.lock = threading.Lock()
        self.sources = {}
        self.captioners = {}
        self.public = PublicFeed(data_dir, cfg, log)
        self.level = lambda role: 1  # set by the web server from Access levels › Listen
        self.translator = Translator(cfg, os.path.join(os.path.dirname(self.path), "..", "translate"), log)
        self.transcripts = Transcripts(os.path.join(data_dir, "transcripts"), int(cfg.get("transcript_days", 30)), log)
        if not os.path.exists(self.path):
            self.save(DEFAULT_LISTEN)
        threading.Thread(target=self.reaper, daemon=True).start()

    # ---- the Admin's setup ----
    def config(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, c):
        from c3life_server import write_json
        old = {x["id"]: x["ch"] for x in self.config()["sources"]} if os.path.exists(self.path) else {}
        new = {x["id"]: x["ch"] for x in c["sources"]}
        with self.lock:
            if "people" not in c and os.path.exists(self.path):  # the Listen settings page doesn't send people; keep them
                c = dict(c, people=self.config().get("people", {}))
            write_json(self.path, c)
        # Only restart a stream whose channels changed or that was removed; everyone else keeps listening.
        for sid in list(self.sources):
            if new.get(sid) != old.get(sid):
                self.sources.pop(sid).stop()
        for sid in list(self.captioners):
            if new.get(sid) != old.get(sid):
                self.captioners.pop(sid).stop()

    def set_person(self, name, sources, fixed):
        c = self.config()
        ids = {x["id"] for x in c["sources"]}
        people = {}
        for n, p in c.get("people", {}).items():  # forget sources that have since been removed
            p = {"sources": [x for x in p.get("sources", []) if x in ids], "fixed": p.get("fixed", "") if p.get("fixed", "") in ids else ""}
            if p["sources"] or p["fixed"]:
                people[n] = p
        if not sources and not fixed:
            people.pop(name, None)
        else:
            people[name] = {"sources": sources, "fixed": fixed}
        err = valid_people(people, {x["id"] for x in c["sources"]})
        if err:
            return err
        self.save(dict(c, people=people))
        return ""

    # ---- room players: a device paired by an Admin gets a long-lived room token ----
    def room_tokens(self):
        if not os.path.exists(self.rooms_path):
            return {}
        with open(self.rooms_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def pair_room(self, room_id):
        from c3life_server import write_json
        tok = "room-" + secrets.token_urlsafe(24)
        with self.lock:
            t = self.room_tokens()
            t[hashlib.sha256(tok.encode()).hexdigest()] = {"room": room_id, "paired": time.strftime("%Y-%m-%d %H:%M")}
            write_json(self.rooms_path, t)
        return tok

    def room_for(self, tok):
        if not tok or not tok.startswith("room-"):
            return None
        r = self.room_tokens().get(hashlib.sha256(tok.encode()).hexdigest())
        if not r:
            return None
        return next((x for x in self.config()["rooms"] if x["id"] == r["room"]), None)

    def unpair_room(self, room_id):
        from c3life_server import write_json
        with self.lock:
            t = {k: v for k, v in self.room_tokens().items() if v["room"] != room_id}
            write_json(self.rooms_path, t)

    # ---- who may hear what ----
    def allowed(self, who, remote=False):
        """who = {"name","role"} for a person, or {"room": {...}} for a room player."""
        c = self.config()
        by_id = {s["id"]: s for s in c["sources"]}
        if who.get("room") is not None:
            sid = who["room"].get("source", "")
            return [by_id[sid]] if sid in by_id and not remote else []
        if who.get("role") == "Admin" and not remote:
            return list(c["sources"])
        if not who.get("guest") and self.level(who.get("role", "")) < 1:  # Access levels › Listen: None
            return []
        # Groups ticked "Anyone can listen without signing in": in the building only, for everyone, signed in or not.
        open_ids = [] if remote else [x for g in c["groups"] if g.get("open") for x in g["sources"]]
        if who.get("guest"):
            return [by_id[i] for i in dict.fromkeys(open_ids) if i in by_id]
        me = c.get("people", {}).get(who["name"], {})
        if me.get("fixed") and not remote:  # kept to one source by an Admin: nothing else, whatever their groups say
            return [by_id[me["fixed"]]] if me["fixed"] in by_id else []
        ids = [] if remote else [x for x in me.get("sources", []) if x in by_id]
        for g in c["groups"]:
            if who["name"] in g["members"] and (g.get("home") or not remote):
                ids += [x for x in g["sources"] if x not in ids]
        ids += [x for x in open_ids if x not in ids]
        return [by_id[i] for i in ids if i in by_id]

    # ---- the audio ----
    def command(self, src):
        ch = src["ch"]
        if len(ch) == 1:
            pan = "pan=stereo|c0=c%d|c1=c%d" % (ch[0] - 1, ch[0] - 1)
        else:
            pan = "pan=stereo|c0=c%d|c1=c%d" % (ch[0] - 1, ch[1] - 1)
        inp = self.cfg.get("listen_input") or ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + self.cfg.get("listen_device", "Dante Virtual Soundcard (1-16)")]
        return ([self.cfg.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error"] + inp +
                ["-af", pan, "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", str(self.cfg.get("listen_kbps", 128)) + "k",
                 "-f", "mp3", "-flush_packets", "1", "-"])

    def open(self, src):
        with self.lock:
            s = self.sources.get(src["id"])
            if not s:
                s = self.sources[src["id"]] = Source(src["id"], self.command(src), self.log)
        return s, s.add()

    def live(self, sid):
        s = self.sources.get(sid)
        return bool(s and s.live)

    def listeners(self, sid):
        s = self.sources.get(sid)
        return len(s.listeners) if s else 0

    # ---- subtitles ----
    def engine_factory(self):
        model = self.cfg.get("captions_model") or os.path.join(os.path.dirname(self.path), "..", "speech-model")
        kind = self.cfg.get("captions_engine", "vosk")
        if kind == "vosk":
            if not os.path.isdir(model):
                raise FileNotFoundError(model)
            return lambda: VoskEngine(model)
        if callable(kind):  # tests
            return kind
        raise ValueError(kind)

    def has_open(self):
        return any(g.get("open") and g["sources"] for g in self.config()["groups"])

    def caption_source(self, src):
        """The source whose speech makes the subtitles: itself, or the one it takes them from."""
        if src.get("captions_from"):
            return next((x for x in self.config()["sources"] if x["id"] == src["captions_from"]), None)
        return src if src.get("captions") else None

    def captions(self, src, since, lang=""):
        c = self.captioners.get(src["id"])
        if not c or c.status in ("stopped",):
            try:
                factory = self.engine_factory()
            except Exception as e:
                return {"status": "unavailable", "reason": "Subtitles aren't set up on the C3 Life server yet (%s)." % e.__class__.__name__, "lines": [], "partial": ""}
            old = c
            c = self.captioners[src["id"]] = Captioner(src["id"], caption_command(self.cfg, src), factory, self.log, self.translator)
            c.transcript = self.transcripts
            if old:  # carry on numbering, so phones already showing subtitles keep getting new lines
                c.lines, c.next_id = old.lines, old.next_id
            else:  # after a restart, carry on from today's saved lines
                c.lines = self.transcripts.today(src["id"])[-Captioner.KEEP_LINES:]
                c.next_id = (c.lines[-1]["id"] + 1) if c.lines else 1
            c.start()
        lines, partial = c.since(since, lang)
        return {"status": c.status, "reason": c.reason, "lines": lines, "partial": partial, "lang": lang}

    def reaper(self):
        while True:
            time.sleep(5)
            self.transcripts.tidy()
            for c in list(self.captioners.values()):
                if c.status == "live" and time.time() - c.last_poll > Captioner.IDLE_S:
                    c.stop()
            for s in list(self.sources.values()):
                if not s.listeners and s.idle_since and time.time() - s.idle_since > IDLE_STOP_S:
                    s.stop()
                    s.idle_since = None


# ---------------------------------------------------------------------------------------------------------
# Subtitles: speech to text on the C3 Life server, with no internet (Vosk). Best on a speech source, not music.
# ---------------------------------------------------------------------------------------------------------
_MODELS = {}


class VoskEngine:
    rate = 16000

    def __init__(self, model_path):
        from vosk import Model, KaldiRecognizer, SetLogLevel  # optional: only needed for subtitles
        SetLogLevel(-1)
        if model_path not in _MODELS:
            _MODELS[model_path] = Model(model_path)
        self.rec = KaldiRecognizer(_MODELS[model_path], self.rate)

    def feed(self, data):
        """Returns (finished line or "", the line so far)."""
        if self.rec.AcceptWaveform(data):
            return json.loads(self.rec.Result()).get("text", ""), ""
        return "", json.loads(self.rec.PartialResult()).get("partial", "")

    def flush(self):
        """The audio stopped: whatever was still being heard becomes a finished line."""
        return json.loads(self.rec.FinalResult()).get("text", "")


def tidy(text):
    """Speech-to-text gives lower case with no full stops: make it easier to read."""
    t = " ".join(text.split())
    if not t:
        return ""
    t = re.sub(r"\bi\b", "I", t)
    return t[0].upper() + t[1:] + ("" if t[-1] in ".?!" else ".")


class Captioner:
    IDLE_S = 60
    MAX_WORDS = 18
    KEEP_LINES = 3000  # about 4 hours of speech: the whole service, held in memory

    def __init__(self, sid, cmd, make_engine, log, translator=None):
        self.sid, self.cmd, self.make_engine, self.log = sid, cmd, make_engine, log
        self.translator = translator
        self.transcript = None  # set by Listen: saves each finished line to today's file
        self.lines = []
        self.next_id = 1
        self.partial = ""
        self.status, self.reason = "starting", ""
        self.proc = None
        self.lock = threading.Lock()
        self.last_poll = time.time()

    def start(self):
        try:
            engine = self.make_engine()
        except Exception as e:  # vosk not installed, or no model
            self.status, self.reason = "unavailable", "Subtitles aren't set up on the C3 Life server (%s)." % e.__class__.__name__
            self.log.warning("captions: %s unavailable: %s", self.sid, e)
            return
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                         creationflags=(0x08000000 if os.name == "nt" else 0))
        except OSError as e:
            self.status, self.reason = "unavailable", "Can't start ffmpeg."
            self.log.error("captions: %s: %s", self.sid, e)
            return
        self.status = "live"
        self.log.info("captions: starting %s", self.sid)
        threading.Thread(target=self.run, args=(engine, self.proc), daemon=True).start()

    def run(self, engine, proc):
        while True:
            data = proc.stdout.read(8000)  # a quarter of a second at 16 kHz
            if not data:
                break
            final, partial = engine.feed(data)
            if not final and len(partial.split()) >= self.MAX_WORDS and hasattr(engine, "flush"):
                final, partial = engine.flush(), ""  # someone who never pauses: break the line so it can be read and translated
            self.add(final, partial)
        if hasattr(engine, "flush"):
            self.add(engine.flush(), "")
        with self.lock:
            if self.status == "live":
                self.status = "stopped"

    def add(self, final, partial):
        with self.lock:
            if final:
                line = {"id": self.next_id, "t": time.strftime("%H:%M:%S"), "text": tidy(final)}
                self.lines.append(line)
                self.next_id += 1
                if self.translator:
                    self.translator.line(self, line)
                del self.lines[:-self.KEEP_LINES]
                if self.transcript:
                    self.transcript.add(self.sid, line)
            self.partial = partial

    def since(self, n, lang=""):
        self.last_poll = time.time()
        with self.lock:
            lines = [x for x in self.lines if x["id"] > n]
        if not lang or not self.translator:
            return lines, self.partial
        return self.translator.since(self, lines, lang), ""

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            self.log.info("captions: stopping %s", self.sid)
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.status = "stopped"


def caption_command(cfg, src):
    ch = src["ch"]
    pan = "pan=mono|c0=c%d" % (ch[0] - 1) if len(ch) == 1 else "pan=mono|c0=0.5*c%d+0.5*c%d" % (ch[0] - 1, ch[1] - 1)
    inp = cfg.get("listen_input") or ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + cfg.get("listen_device", "Dante Virtual Soundcard (1-16)")]
    return [cfg.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error"] + inp + ["-af", pan, "-ar", "16000", "-ac", "1", "-f", "s16le", "-"]


# ---------------------------------------------------------------------------------------------------------
# Public: send ONE stream to an internet radio (Icecast) service, which serves everyone with a public link.
# The C3 Life server is never opened to the internet. A person starts and stops it; it never starts by itself.
# ---------------------------------------------------------------------------------------------------------
class PublicFeed:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.path = os.path.join(data_dir, "listen-public.json")
        self.proc = None
        self.on = False
        self.since = ""
        self.error = ""
        self.lock = threading.Lock()

    def settings(self):
        if not os.path.exists(self.path):
            return {"source": "stream", "host": "", "port": 8000, "mount": "/church", "user": "source", "password": "", "name": "Church live", "link": ""}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def public_view(self):
        s = dict(self.settings())
        s["has_password"] = bool(s.pop("password", ""))  # the password never leaves the C3 Life server
        s.update(on=self.on, running=bool(self.proc and self.proc.poll() is None), since=self.since, error=self.error)
        return s

    def save(self, body):
        from c3life_server import write_json
        cur = self.settings()
        for k in ("source", "host", "mount", "user", "name", "link"):
            if k in body:
                v = body[k]
                if not isinstance(v, str) or len(v) > 200 or any(c in v for c in "\r\n"):
                    return "%s isn't valid." % k
                cur[k] = v.strip()
        if "port" in body:
            if not isinstance(body["port"], int) or not 1 <= body["port"] <= 65535:
                return "Port must be a number from 1 to 65535."
            cur["port"] = body["port"]
        if body.get("password"):
            if not isinstance(body["password"], str) or len(body["password"]) > 200:
                return "Password isn't valid."
            cur["password"] = body["password"]
        if cur["mount"] and not cur["mount"].startswith("/"):
            cur["mount"] = "/" + cur["mount"]
        if cur["host"] and not re.match(r"^[A-Za-z0-9.-]{1,253}$", cur["host"]):
            return "The service address should look like stream.example.com."
        if cur["link"] and not re.match(r"^https?://[^\s<>\"']+$", cur["link"]):
            return "The public link should start with https://"
        with self.lock:
            write_json(self.path, cur)
        return ""

    def command(self, src):
        s = self.settings()
        ch = src["ch"]
        pan = "pan=stereo|c0=c%d|c1=c%d" % ((ch[0] - 1, ch[0] - 1) if len(ch) == 1 else (ch[0] - 1, ch[1] - 1))
        inp = self.cfg.get("listen_input") or ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + self.cfg.get("listen_device", "Dante Virtual Soundcard (1-16)")]
        from urllib.parse import quote
        url = "icecast://%s:%s@%s:%d%s" % (quote(s["user"], safe=""), quote(s["password"], safe=""), s["host"], int(s["port"]), s["mount"])
        return ([self.cfg.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error"] + inp +
                ["-af", pan, "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-content_type", "audio/mpeg",
                 "-ice_name", s.get("name") or "Church live", "-f", "mp3", url])

    def start(self, src):
        s = self.settings()
        if not s["host"] or not s["password"]:
            return "Fill in the service address and password first."
        with self.lock:
            if self.on:
                return "It's already on."
            self.on, self.error = True, ""
            self.since = time.strftime("%H:%M")
        threading.Thread(target=self.keep_running, args=(src,), daemon=True).start()
        self.log.info("public: started sending %s to %s", src["id"], s["host"])
        return ""

    def keep_running(self, src):
        """While it's on, keep the stream going; if the internet drops, try again every 10 seconds."""
        while self.on:
            try:
                self.proc = subprocess.Popen(self.command(src), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                             creationflags=(0x08000000 if os.name == "nt" else 0))
                err = self.proc.stderr.read().decode("utf-8", "replace")
                self.proc.wait()
            except OSError as e:
                err = str(e)
            if self.on:
                last = (err.strip().splitlines() or ["The stream stopped"])[-1]
                pw = self.settings().get("password")
                if pw:  # ffmpeg can print the address it was given: never show the password
                    from urllib.parse import quote
                    last = last.replace(quote(pw, safe=""), "***").replace(pw, "***")
                self.error = last[:200]
                self.log.warning("public: stopped (%s); trying again in 10 s", self.error)
                for _ in range(20):
                    if not self.on:
                        break
                    time.sleep(0.5)

    def stop(self):
        with self.lock:
            self.on = False
        p = self.proc
        self.error = ""
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.log.info("public: stopped by a person")


# ---------------------------------------------------------------------------------------------------------
# Translated subtitles: Argos Translate language packs, run on this PC with no internet. Each finished
# subtitle line is translated once per language, only while someone has that language chosen.
# ---------------------------------------------------------------------------------------------------------
NATIVE = {"ar": "العربية", "bg": "Български", "bn": "বাংলা", "ca": "Català", "cs": "Čeština", "da": "Dansk", "de": "Deutsch",
          "el": "Ελληνικά", "es": "Español", "et": "Eesti", "fa": "فارسی", "fi": "Suomi", "fr": "Français", "ga": "Gaeilge",
          "he": "עברית", "hi": "हिन्दी", "hu": "Magyar", "id": "Bahasa Indonesia", "it": "Italiano", "ja": "日本語", "ko": "한국어",
          "lt": "Lietuvių", "lv": "Latviešu", "ms": "Bahasa Melayu", "nb": "Norsk", "nl": "Nederlands", "pl": "Polski",
          "pt": "Português", "pb": "Português (Brasil)", "ro": "Română", "ru": "Русский", "sk": "Slovenčina", "sl": "Slovenščina",
          "sq": "Shqip", "sv": "Svenska", "sw": "Kiswahili", "th": "ไทย", "tl": "Tagalog", "tr": "Türkçe", "uk": "Українська",
          "ur": "اردو", "vi": "Tiếng Việt", "zh": "中文 (简体)", "zt": "中文 (繁體)"}


class ArgosEngine:
    """One Argos Translate language pack (English to one language), used straight through CTranslate2."""

    def __init__(self, path):
        import ctranslate2
        import sentencepiece
        self.sp = sentencepiece.SentencePieceProcessor(model_file=os.path.join(path, "sentencepiece.model"))
        self.model = ctranslate2.Translator(os.path.join(path, "model"), device="cpu", inter_threads=1, intra_threads=2)

    def translate(self, text):
        r = self.model.translate_batch([self.sp.encode(text, out_type=str)], beam_size=2, max_decoding_length=256)
        return self.sp.decode(r[0].hypotheses[0]).strip()


class Translator:
    ACTIVE_S = 60  # stop translating a language a minute after the last person stops reading it

    def __init__(self, cfg, default_dir, log):
        self.cfg, self.log = cfg, log
        self.dir = cfg.get("translate_dir") or default_dir
        self.engines, self.done, self.active = {}, {}, {}
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self._langs, self._seen = [], None
        threading.Thread(target=self.work, daemon=True).start()

    def languages(self):
        """What's installed: [{"code","name","native"}], read from each pack's metadata.json."""
        if callable(self.cfg.get("translate_engine")):
            return sorted(({"code": c, "name": n, "native": NATIVE.get(c, n)} for c, n in self.cfg.get("translate_langs", [])), key=lambda x: x["name"])
        try:
            names = sorted(os.listdir(self.dir))
            stamp = (names, [os.path.getmtime(os.path.join(self.dir, n)) for n in names])
        except OSError:
            return []
        if stamp != self._seen:
            out = []
            for n in names:
                try:
                    with open(os.path.join(self.dir, n, "metadata.json"), encoding="utf-8") as f:
                        m = json.load(f)
                except (OSError, ValueError):
                    continue
                if m.get("from_code") == "en" and re.match(r"^[a-z]{2,3}$", m.get("to_code", "")):
                    out.append({"code": m["to_code"], "name": m.get("to_name", m["to_code"]), "native": NATIVE.get(m["to_code"], m.get("to_name", "")), "_path": os.path.join(self.dir, n)})
            self._langs, self._seen = sorted(out, key=lambda x: x["name"]), stamp
        return [{k: v for k, v in x.items() if k != "_path"} for x in self._langs]

    def has(self, lang):
        return any(x["code"] == lang for x in self.languages())

    def engine(self, lang):
        if lang not in self.engines:
            kind = self.cfg.get("translate_engine")
            if callable(kind):  # tests
                self.engines[lang] = kind(lang)
            else:
                path = next(x["_path"] for x in self._langs if x["code"] == lang)
                self.log.info("translate: loading %s", lang)
                self.engines[lang] = ArgosEngine(path)
        return self.engines[lang]

    def line(self, cap, line):
        """A new finished subtitle line: translate it into every language someone is reading."""
        now = time.time()
        for (sid, lang), t in list(self.active.items()):
            if t[0] is cap and now - t[1] < self.ACTIVE_S:
                self.q.put((cap, lang, line))

    def since(self, cap, lines, lang):
        """The translated lines, in order, as far as they're done."""
        key = (cap.sid, lang)
        old = self.active.get(key)
        if not old or old[0] is not cap or time.time() - old[1] >= self.ACTIVE_S:
            # someone just picked this language: catch up on the last few lines, skip anything older
            recent = cap.since(0)[0][-4:]
            start = recent[0]["id"] if recent else cap.next_id
            for x in recent:
                self.q.put((cap, lang, x))
        else:
            start = old[2]
        self.active[key] = (cap, time.time(), start)
        out = []
        with self.lock:
            for x in lines:
                if x["id"] < start:
                    continue
                t = self.done.get((cap.sid, lang, x["id"]))
                if t is None:
                    break
                out.append(dict(x, text=t, original=x["text"]))
        return out

    def work(self):
        while True:
            cap, lang, line = self.q.get()
            k = (cap.sid, lang, line["id"])
            if k in self.done:
                continue
            try:
                t = self.engine(lang).translate(line["text"])
            except Exception as e:  # a broken pack must not stop the subtitles
                self.log.warning("translate: %s failed: %s", lang, e)
                t = line["text"]
            with self.lock:
                self.done[k] = t
                if len(self.done) > 4000:
                    for old in list(self.done)[:1000]:
                        del self.done[old]



# ---------------------------------------------------------------------------------------------------------
# Transcripts: each finished subtitle line is saved to a file for that day and source, so subtitles carry on
# after a restart and an Admin can download the day's words. Deleted after "transcript_days" (0 = never saved).
# ---------------------------------------------------------------------------------------------------------
class Transcripts:
    NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9-]{1,40})\.jsonl$")

    DAYS = (0, 7, 30, 90, 365)

    def __init__(self, folder, days, log):
        self.folder, self.days, self.log = folder, days, log
        self.lock = threading.Lock()
        self.last_tidy = 0
        # An Admin's choice on Listen settings beats "transcript_days" in config.json.
        self.settings_path = os.path.join(os.path.dirname(folder), "listen-transcripts.json")
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                self.days = int(json.load(f)["days"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        os.makedirs(folder, exist_ok=True)

    def set_days(self, days):
        from c3life_server import write_json
        if days not in self.DAYS:
            return "Choose how long to keep them: %s days." % ", ".join(str(d) for d in self.DAYS)
        with self.lock:
            write_json(self.settings_path, {"days": days})
        self.days, self.last_tidy = days, 0
        self.tidy()  # fewer days (or none) deletes the older files now
        return ""

    def delete(self, day, sid):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or not re.match(r"^[a-z0-9-]{1,40}$", sid):
            return False
        try:
            with self.lock:
                os.remove(self.path(sid, day))
            return True
        except OSError:
            return False

    def path(self, sid, day=None):
        return os.path.join(self.folder, "%s-%s.jsonl" % (day or time.strftime("%Y-%m-%d"), sid))

    def add(self, sid, line):
        if self.days <= 0:
            return
        try:
            with self.lock, open(self.path(sid), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            self.log.warning("transcript: can't save: %s", e)

    def read(self, sid, day=None):
        try:
            with open(self.path(sid, day), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError):
            return []

    def today(self, sid):
        return self.read(sid) if self.days > 0 else []

    def list(self):
        out = []
        try:
            names = os.listdir(self.folder)
        except OSError:
            return out
        for n in sorted(names, reverse=True):
            m = self.NAME.match(n)
            if m:
                with open(os.path.join(self.folder, n), encoding="utf-8") as f:
                    count = sum(1 for x in f if x.strip())
                out.append({"day": m.group(1), "source": m.group(2), "lines": count})
        return out

    def text(self, day, sid):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or not re.match(r"^[a-z0-9-]{1,40}$", sid):
            return None
        lines = self.read(sid, day)
        if not lines:
            return None
        return "\n".join("%s  %s" % (x.get("t", ""), x.get("text", "")) for x in lines) + "\n"

    def tidy(self):
        """Once an hour, delete transcripts older than the number of days kept."""
        if time.time() - self.last_tidy < 3600:
            return
        self.last_tidy = time.time()
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - max(self.days, 0) * 86400))
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for n in names:
            m = self.NAME.match(n)
            if m and (self.days <= 0 or m.group(1) < cutoff):
                try:
                    os.remove(os.path.join(self.folder, n))
                    self.log.info("transcript: deleted %s (older than %d days)", n, self.days)
                except OSError:
                    pass
