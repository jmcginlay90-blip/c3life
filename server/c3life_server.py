#!/usr/bin/env python3
"""C3 Life web server.

Serves the C3 Life app to every phone, iPad and laptop on the booth network
(http://c3life.local, port 80), answers a health check, and keeps each
person's monitor mixes on the C3 Life server so they stay in sync on all their
devices. For now the app it serves is the clickable mock-up; the real app
drops into the same app folder later.

Only computers on the booth network (and this PC) are answered. No directory
listings, nothing outside the app folder. Mixes need a sign-in with the
person's PIN, checked here. Python 3.9+ standard library only.

API (JSON):
  GET  /api/health                         no sign-in
  POST /api/login   {"name","pin"} or {"name","password"} (Admins) -> {"token","name","role","expires"}
  POST /api/logout                         (token)
  GET  /api/mixes[?since=<version>]        (token) -> {"version","saves","mymix","updated"} or {"changed":false}
  PUT  /api/mixes   {"saves"?,"mymix"?}    (token) -> {"version","updated"}
  GET  /api/listen/mine                    (token or room token) -> what this person or room may hear
  GET  /api/listen/config                  (Admin) sources, groups, rooms, and what's live
  PUT  /api/listen/config                  (Admin)
  POST /api/listen/pair    {"room"}        (Admin) -> {"token"} for a room player
  POST /api/listen/unpair  {"room"}        (Admin)
  GET  /listen/<source>.mp3?t=<token>      the audio
  GET  /api/listen/captions/<source>?since=<n>&t=<token>[&lang=es]   subtitles (sources with "captions" on),
                                           translated on this PC when lang is an installed language
  GET  /api/dante/dvs                     (Admin) what Dante routes into each DVS channel, read with netaudio
  GET  /api/dante/devices                 (Dante view) every Dante device with its transmit and receive channel names
  POST /api/dante/rename/check {"device","new"} or {"device","kind":"tx"|"rx","number","new"}   (Dante view)
                                           what renaming it would break: every route that uses the old name
  POST /api/dante/rename {..., "id"}       (Dante control) rename it, then route those again with the new name
  GET  /api/access                        the roles and what each may do (Access levels)
  PUT  /api/access  {"roles","password"}  (Admin, password again)
  PUT  /api/listen/person  {"name","sources","fixed","password"}   (Admin, password again) what one person may hear
  GET  /api/listen/transcripts             (Admin) saved subtitle transcripts, one per day and source
  GET  /api/listen/transcript/<day>-<source>.txt?t=<token>   (Admin) download one
  DELETE /api/listen/transcript/<day>-<source>.txt          (Admin) delete one
  PUT  /api/listen/transcripts {"days"}     (Admin) keep saved subtitles 0, 7, 30, 90 or 365 days
  GET  /api/listen/public                  (Admin) the internet feed settings (never the password)
  PUT  /api/listen/public                  (Admin) change them, including the public link
  POST /api/listen/public/start {"source"?} (Admin) start sending to the internet radio service
  POST /api/listen/public/stop             (Admin)
  GET  /api/wing[?since=<version>]          (Mixer view) the live Wing: status, names, colours, faders, mutes,
                                           each channel's mic input with its gain and phantom power
  GET  /api/wing/meters                    (Mixer view) channel levels in dB
  GET  /api/wing/channel?n=<1-40>          (Mixer view) one channel's trim, filters, gate, EQ and compressor
                                           (read while it's open; change them with /api/wing/set, field "eq/1g"...)
  GET  /api/wing/tapeq?n=<1-40>            (Tap EQ view) a channel's tap EQ: its own 3-band EQ on the pre-fader send tap,
                                           heard in every monitor mix that takes the channel pre-fader (not front of house)
  POST /api/wing/tapeq {"n","field","value"}   (Tap EQ control) "peq/on", "peq/1g", "peq/1f", "peq/1q", ... 3
  GET  /api/music                          (Music view) Spotify on the Mac mini (through the C3 Life helper), the playlists,
                                           whether ProPresenter is open there, and "can" (Music control)
  PUT  /api/music {"playlists","fade_seconds","walk_in_volume"}   (Music control) the playlists: {"name", "uri"}
  POST /api/music/do {"do": "play|pause|next|previous|volume|playlist|fade", ...}   (Music control) passed to the helper
  POST /api/music/propresenter             (Music control) open ProPresenter (on its computer's helper)
  GET  /api/macros                         (Macros view) the run sheet's macros, the run going on now, and (Macros
                                           control) "choices": the steps C3 Life can run, named from what's set up
  PUT  /api/macros {"macros","playback"}   (Macros control) the macros, and the notes Playback steps send
  POST /api/macros/run {"id"} | /stop | /undo   (Macros control) nothing runs unless someone presses Run
  POST /api/power {"do": "app|restart|off|abort|cycle"}   (Settings or Mixer control; cycle: Admin) restart C3 Life,
                                           restart or shut down this computer (15 s, abort stops it), smart-plug reset
  GET  /api/midi                          (Show, Lights or Mixer view) C3 Life's network MIDI session: who's connected, what came in
  GET  /api/chat?since=<id> | POST /api/chat {"text", "ch", "stage"}   (Chat view / control, and chat on for that
                                           person) team chat; the stage display channel also goes to ProPresenter
  GET  /api/users | PUT /api/users {"password", "users", "removed"}   (Admin; saving needs the password again)
                                           Users & PINs: names, roles, PINs, can sign in, team chat
  GET  /api/home                          (signed in) Home: the slide, Playback, the band DCA, the room level, devices,
                                           recording; each part only for a role that can see that area
  POST /api/home/do {"do": "play|stop|band|mark", "on", "name"}   Play/Stop to Playback (Show control), mute the band
                                           DCA ("band_dca", Mixer control), a marker in the log (and the recording)
  GET  /api/plan/order?from=pco|pp        (Running order view) the running order from Planning Center or ProPresenter's
                                           playlist, each item marked if it's in the other one too
  GET  /api/log?since=<id>                 (Running order, Show or Mixer view) the service log: what C3 Life did today
  GET  /api/pp/status | /api/pp/timers     (Show or Running order view) ProPresenter's slide now, next and after it, and
                                           its timers; GET /api/pp/thumb?u=&i=&w=&t=<token> a slide's picture
  POST /api/pp/trigger {"do": "next|previous|go|clear", "index", "layer"}   (Show control)
  POST /api/pp/timer {"id", "do": "start|stop|reset|add|set", "secs"}       (Show control)
  GET  /api/recording                      (Recording view) the recording drive, what's recording, its tracks (named from
                                           the Wing) and levels, the recordings on the drive, playback, the settings
  PUT  /api/recording/settings {...}       (Recording control) start by itself (off | slide | plug), the start and end
                                           slides, the most hours, markers from presentations or playlist items, channels
  POST /api/recording/start | /stop | /marker {"name"} | /drive {"id","use"} | /plan {"name"}   (Recording control)
  POST /api/recording/copy {"name","where","to"}   (Recording control) copy a recording onto a plugged-in drive
  POST /api/recording/play {"name","from","to","loop","part","where"} | /stage   (Recording + Mixer control) play back to the desk; put it back
  PUT  /api/rta/ringout {"on","sens"}      (RTA settings control) the feedback watch; GET /api/rta/live has "ringout"
  POST /api/rta/ringout/cut {"f"}          (RTA settings + Mixer control, the Wing in Control) a narrow cut on a flat band
                                           of Main 1's EQ; again on the same note goes deeper
  POST /api/rta/ringout/undo {"id"}        (the same) that band back as it was
  POST /api/foyer/do {"do": "source", "to": "a"|"b"} | {"do": "volume", "db"} | {"do": "mute", "on"}   (Foyer control),
                     {"do": "balance", "mix": "a"|"b", "db"}  (Foyer setup control: each mix's send level to the matrix)
                                           which mix the foyer hears, its volume (never above the maximum) and mute,
                                           on the Wing (Control); GET /api/foyer has "live", what the desk is doing now
  GET  /api/daily | PUT /api/daily {...}    (Settings or RTA view / Admin) the daily RTA and activity reports: logging
                                           times, the service times, which days were logged and sent
  POST /api/daily/send {"day"}              (Admin) email that day's RTA and activity reports now (shutting down does it too)
  GET  /api/daily/file?t=&day=&kind=        (Admin) download a day's RTA readings, spectrum or activity log
  GET  /api/network | /api/network/scan     (Settings view) the server's own connections and the Wi-Fi it can hear (Windows)
  POST /api/network/join|forget|order       (Admin; the screen asks first) join a Wi-Fi with its password, forget one, or
                                           set the order Windows tries them (passwords are never logged or kept)
  GET  /api/midi/buttons | PUT {"on","maps"} (Show/Mixer/Settings view; Settings control) MIDI buttons: a pad or
                                           footswitch on network MIDI runs a macro, next/previous slide, Lights Go, Playback
  POST /api/midi/buttons/learn              (Settings control) the next note or CC heard is taken for a new button
  GET  /api/mics | PUT /api/mics {"receivers","warn"}  (Mixer view / control) wireless mics from networked Shure
                                           receivers: battery, signal, names from the Wing (watch only: it only asks)
  GET  /api/md/live                        (signed in) is the MD talking now? Every screen asks, for the green box
  GET  /api/md | PUT /api/md {...}          (Mixer or Show view / Mixer control) MD talk: the channel, when it goes green,
                                           the Listen source for the words, the stage display, and the log
  POST /api/md/test | /api/md/clear         (Mixer control) every screen green for 5 seconds / clear the log
  GET  /api/setup                          (signed in) the app's name and version; for an Admin, the backups too
  POST /api/setup/name {"name"}             (Admin) rename the app (Home, the laptop dashboard, reports)
  POST /api/setup/backup {"drive"}          (Admin) back up data/ on the server (and to the plugged-in drive)
  GET  /api/setup/download?t=&id=           (Admin) download a backup (no id: one made now)
  POST /api/setup/restore {"id","password"} (Admin, with the password again) put a backup back and restart C3 Life
  GET  /api/check                          (the sound, Show, Lights or Settings teams) Help's system check, from what the server
                                           knows now: the server, DVS, each device, Dante, the scan, the drive, storage, internet
  POST /api/support {"note"}                (the same) a support report: the check, the last 200 log lines and the settings
                                           (no passwords, PINs or keys), saved and emailed like Report an issue
  GET  /api/devices                        (signed in) what's set up and whether it's answering, for the warning on
                                           every screen: only what this role can see (Wing and Tio: Mixer or Own monitor mix)
  PUT  /api/devices {"left","events","events_mode"}  (Settings control) leave devices out (no warnings, macros skip
                                           their steps), mark some events only, and turn events mode on or off
  POST /api/helpers/find                   (Music or Settings control) scan the network now for C3 Life helpers
  POST /api/helpers/key {"id","key"}       (Admin) a helper's key, checked with the helper; kept here, never sent back
  POST /api/helpers/forget {"id"}          (Admin) take a helper off the list
  GET  /api/wing/history?n=&since=          (Mixer view) the open channel's last 8 s of level and gate/comp gain reduction
  GET  /api/wing/rta                       (Mixer view) the Wing's RTA: 120 bands from 20 Hz, and what it's on
  POST /api/wing/rta {"n","tap","kind"?}   (Mixer control; a musician for their own mix, "kind": "bus") point the Wing's
                                           RTA at a channel or a bus (IN, PREEQ, POSTEQ, POST). GET: a musician sees it
                                           only while it's on one of their own mixes
  POST /api/wing/set  {"kind","n","field","value"}   (Mixer control) fdr, mute, name, col, gain or vph
  POST /api/wing/fade {"moves":[...]}       (Mixer or Macros control) slide cues' fades, run here
  PUT  /api/wing/settings {"ip"?,"mode"?}   (Admin) the Wing's address; off, watch or control
  POST /api/wing/find                      (Admin) look for Wings on the network
  GET  /api/monitors                       (Mixer view: everything; a musician: their own mixes) monitor mixes and the lock
  PUT  /api/monitors {"buses","people","locked"}   (Mixer control) which buses are monitor mixes, whose is whose
  GET  /api/wing/sends?bus=<1-16>           (Mixer view, or a musician for their own mix) every channel's send to a bus
  POST /api/wing/send {"bus","n","field","value","kind"?}   (Mixer control, or a musician's own mix while unlocked)
                                           field lvl, on or pan for channel n ("kind": "aux" for an aux input, like the
                                           click); n 0 is the bus itself (fdr, mute, pan, or its EQ: "eq/on", "eq/1g"...)
  GET  /api/monitors/check                 (Mixer view) monitor buses with sends that aren't Pre, or pan linked
  POST /api/monitors/fix                   (Mixer control) make them Pre with pan link off
  GET  /api/tio                            (Mixer view) the Tio1608-D's 16 inputs: gain, phantom power, low cut
  POST /api/tio/set {"n","field","value"}  (Mixer control) gain, vph, hpf or hpff
  PUT  /api/tio/settings {"ip"?,"mode"?,"card_from"?}  (Admin)
  GET  /api/wing/patch                     (Wing patch view) the Wing's card and USB outputs and what feeds each one
  POST /api/wing/patch {"out","grp","idx"}  (Wing patch control, Wing in Control) patch one output; the screen asks first
  GET  /api/rta/inputs                     (RTA view) where the room mic and the desk reference come from, how they reach
                                           this server (DVS, AES67 or USB channel), and the route Dante really has
  PUT  /api/rta/inputs {"mic","ref"}        (RTA settings control) choose them; nothing changes on the desk or on Dante
  GET  /api/rta/plan?which=mic|ref         (RTA view) what C3 Life would patch on the Wing and route on Dante for it
  POST /api/rta/patch {"which","id"}       (RTA settings control, plus Wing patch control for the Wing and Dante control for Dante; the Wing in Control)
                                           make that plan, if it's still the same; each step is read back
  POST /api/rta/unpatch {"which"}          (the same) put the last one back
  GET  /api/rta/live                       (RTA view) what the server hears: the room mic and the desk reference in 120 bands
                                           (1/12 octave from 20 Hz), dB(A), dB(Z) and the Leq averages. Asking keeps it
                                           listening; it stops 30 s after the last ask
  POST /api/rta/calibrate {"spl", "weight": "Z"|"A"}   (RTA settings control) the calibrator (Z) or a meter (A) reads spl now
                                           {"clear": true}: not calibrated any more (a new mic, or the gain changed)
  GET  /api/foyer                          (Foyer, Show or Mixer view) the foyer's name, its two mixes and matrix,
                                           with the names the desk gives those buses (when the Wing is connected)
  PUT  /api/foyer {"name","mix_a","mix_b","mtx","max_db"}   (Foyer setup control: Admin) rename the foyer, or choose its buses
  GET  /api/scan                           (Mixer, Show or Settings view) the last network scan, and whether one is running
  POST /api/scan                           (Mixer or Settings control) scan the network now (read only; about 10 s)
  POST /api/scan/use {"kind","ip","port"}  (Mixer or Settings control) use this Wing, Tio or ProPresenter (Watch only)
  POST /api/scan/hide {"ip","hwid","hidden"}  (Admin) "Not booth gear": stop listing a device without a driver
  PUT  /api/scan/settings {"auto"}         (Admin) scan by itself at start-up and every 5 minutes
  GET  /api/lights                         (Lights view) the Lightkey link, its cues, palette, fixtures and looks,
                                           and what C3 Life last sent (Lightkey doesn't answer over OSC)
  PUT  /api/lights/show {"cues","presets","fixtures","looks"}   (Lights control) keep the show's lists
  PUT  /api/lights/settings {"ip","port","mode","page","go","replace","mac"}   (Admin) the lighting Mac; Off or Control
  POST /api/lights/do {"do": "cue"|"go"|"back"|"release"|"preset"|"look"|"master"|"blackout"|"tap"|"tempo"|"fixture"|"clear", ...}
                                           (Lights control) send it to Lightkey as OSC; "fixture" and "look" take "fade"
  POST /api/lights/find                    (Admin) look for Lightkey on the network (Bonjour)
  GET  /api/lights/midi                    (Lights view) the network MIDI session (MultiTracks Playback), who's connected,
                                           what each message does, and the last ones heard with what they did
  PUT  /api/lights/midi {"follow","channel","pc","pc_from","notes","cc_master","tempo"}   (Lights control)

Listening from home: add the private network (for example Tailscale, "100.64.0.0/10") to
"remote_networks". Devices there can only open the Listen page and the audio their group allows
for home, never the booth controls.

Remote support: with "remote_support_roles" set (for example ["Admin"]), someone with that role who signs in
from one of those networks can use the whole app from anywhere, to fix things. Everyone else still gets Listen
only. Off unless it's set. Every remote support sign-in and page is logged.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import queue as _queue
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import access as access_mod
import dante as dante_mod
import listen as listen_mod
import nametags as nametags_mod
import scripture as scripture_mod
import cues as cues_mod
import reports as reports_mod
import pco as pco_mod
import desk as desk_mod
import wing as wing_mod
import tio as tio_mod
import monitors as monitors_mod
import scan as scan_mod
import foyer as foyer_mod
import rta_inputs as rta_inputs_mod
import rta_patch as rta_patch_mod
import dante_names as dante_names_mod
import lightkey as lightkey_mod
import music as music_mod
import helpers as helpers_mod
import health as health_mod
import macros as macros_mod
import rta_live as rta_live_mod
import midi_in as midi_mod
import audio_engine as audio_engine_mod
import recorder as recorder_mod
import propresenter as propresenter_mod
import power as power_mod
import syscheck as syscheck_mod
import setup as setup_mod
import mdtalk as mdtalk_mod
import shure as shure_mod
import midi_buttons as midi_buttons_mod
import netwin as netwin_mod
import daily as daily_mod
import chat as chat_mod

VERSION = "0.3.0"
START = time.time()
log = logging.getLogger("c3life")


class ServiceLog(logging.Handler):
    """The service log on Plan › Log: the last few hundred things C3 Life did, as it says them in its own log
    (macros, cues, recording, the desk, markers...). Every device reads the same list (GET /api/log)."""
    KEEP = 500

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines, self.n = [], 0
        self.lk = threading.Lock()

    def emit(self, rec):
        try:
            msg = rec.getMessage()
        except Exception:
            return
        with self.lk:
            self.n += 1
            self.lines.append({"id": self.n, "t": rec.created, "at": time.strftime("%H:%M:%S", time.localtime(rec.created)),
                               "level": "error" if rec.levelno >= logging.ERROR else ("warn" if rec.levelno >= logging.WARNING else "info"),
                               "text": msg[:300]})
            del self.lines[:-self.KEEP]

    def since(self, n=0):
        with self.lk:
            return [x for x in self.lines if x["id"] > n]


SERVICE_LOG = ServiceLog()
log.addHandler(SERVICE_LOG)
log.setLevel(logging.INFO)  # the service log keeps INFO and up, whatever the file log does

DEFAULTS = {
    "port": 80,
    "bind": "0.0.0.0",
    "app_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"),
    "data_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    # The booth network, plus this PC itself.
    "allowed_networks": ["192.168.10.0/24", "127.0.0.0/8"],
    # Also the network this PC is on now (each of its addresses' /24), added at every start, so phones still get in
    # after the church's router or network changes. false = only allowed_networks.
    "own_network": True,
    # Where "/" goes: Listen, with Sign in at the top. Someone already signed in on that device goes to their own
    # first screen instead (Home, My mix or Foyer), which player.html works out from "start=1".
    "start_page": "/player.html?p=Listen.dc.html&start=1",
    "session_hours": 12,
    # Private networks allowed to LISTEN only (from home). Empty = off. Tailscale: ["100.64.0.0/10"]
    "remote_networks": [],
    # Roles that may use the whole app from those networks (remote support). [] = Listen only from outside.
    "remote_support_roles": [],
    # Where the Listen audio comes from (Dante Virtual Soundcard as a Windows sound device).
    "ffmpeg": "ffmpeg",
    "listen_device": "Dante Virtual Soundcard (1-16)",
    # The audio engine: "asio" = C3 Life opens Dante Virtual Soundcard in ASIO mode (64 x 64) and shares it out to
    # Listen, the RTA, the MD log, recording and playback; "" = Listen and the RTA read DVS in WDM mode (16 channels).
    "audio_engine": "",
    # The smart plug the C3 Life server's power goes through (a Shelly), for the hard reset on the server power screen.
    "smart_plug": "",
    # The Wing DCA Home's "Mute band" button mutes.
    "band_dca": 1,
    "listen_kbps": 128,
    # Subtitles are saved to data\transcripts, one file per day and source, and deleted after this many days.
    # 0 = never saved to disk (kept in memory only, and lost on a restart).
    "transcript_days": 30,
    # Dante Virtual Soundcard's name in Dante Controller ("" = this computer's name, DVS's default).
    "dante_dvs_name": "",
    # Name tags: Planning Center Personal Access Token (api.planningcenteronline.com > Personal Access Tokens).
    "pco_app_id": "",
    "pco_secret": "",
    "pco_service_type": "Sunday Morning",
    "nametag_team": "9 - Ministry",
    "nametag_note_categories": ["Hosting"],
    # ProPresenter's network API (ProPresenter > Settings > Network), and the message used for anyone without a prop.
    "propresenter": "",  # "" = the network scan finds it (and follows it if its address changes)
    "nametag_message": "Name tag",
    "nametag_token": "Name",
    # Bible Scripture. KJV and WEBBE are built in; the others need the church's own keys.
    "esv_key": "",
    "nlt_key": "",
    "apibible_key": "",
    "apibible_ids": {},
    "scripture_default": "NIV",
    "scripture_message_prefix": "Scripture",
    # Issue reports: each is emailed on its own when there's internet. The address can be changed in the app.
    "report_to": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    # optional: also add each report to a monday.com board (the board is chosen in the app)
    "monday_token": "",
    # the Behringer Wing: its address (or find it from the Mixer screen); starts as watch only
    "wing_ip": "",
    "wing_meters": True,
    # the Yamaha Tio1608-D stagebox (gain and phantom power, like R Remote); starts as watch only
    "tio_ip": "",
    # Network scan (Drivers screen): by itself at start-up and every 5 minutes. It only asks; it connects what it
    # recognises in Watch only, follows a device that moved, and never turns Control on. Which networks: allowed_networks.
    "scan_auto": True,
    # C3 Life helpers (helper/c3life_helper.py) on the Macs and Windows PCs: the network scan finds them by itself, and an
    # Admin types each one's key on Music (kept in data/helpers.json, never sent to phones). Only needed to set one by hand:
    "helper": "",
    "helper_key": "",
}

# What a device listening from home may open: the Listen page and its audio, nothing else.
REMOTE_OK = ("/player.html", "/project/Listen.dc.html", "/project/User.dc.html", "/api/health", "/api/login", "/api/logout", "/api/listen/mine", "/api/listen/captions/", "/listen/")

# The mock-up's users, each with PIN 1234 (Viewer has none), except Admins, who use a password (demo: c3life-admin).
# Written once, the first time the server starts. Change them before real use:
#   python c3life_server.py --set-pin "Keys (musician)"      (4 digits)
#   python c3life_server.py --set-pin "Admin"                (Admins: a password of 8 or more characters)
DEMO_ADMIN_PASSWORD = "c3life-admin"
DEMO_USERS = [("Admin", "Admin"), ("FOH sound", "FOH sound"), ("ProPresenter", "ProPresenter"), ("Lighting", "Lighting"),
              ("Foyer team", "Foyer team"), ("Viewer", "Viewer"), ("Keys (musician)", "Musician"), ("Listener", "Listener")]

MAX_BODY = 256 * 1024
MAX_SAVES = 50
FAIL_LIMIT, FAIL_WINDOW = 5, 300  # 5 wrong PINs in 5 minutes locks that name for the rest of the 5 minutes


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())


def check_pin(pin, stored):
    try:
        _, salt, want = stored.split("$")
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(hash_pin(pin, salt).split("$")[2], want)


def valid_password(pw):
    """Admins: 8 or more characters, with a letter and a number or symbol."""
    return isinstance(pw, str) and 8 <= len(pw) <= 200 and any(c.isalpha() for c in pw) and any(not c.isalpha() for c in pw)


def write_json(path, data):
    """Write atomically, so a power cut never leaves half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    """Users and each person's mixes, kept in data_dir on the C3 Life server."""

    def __init__(self, data_dir):
        self.dir = os.path.abspath(data_dir)
        self.mix_dir = os.path.join(self.dir, "mixes")
        os.makedirs(self.mix_dir, exist_ok=True)
        self.users_path = os.path.join(self.dir, "users.json")
        self.lock = threading.Lock()
        if not os.path.exists(self.users_path):
            # Viewer and Listener have no PIN: anyone on the booth network can look, or listen to what Listener may hear.
            users = [{"name": n, "role": r, "pin": hash_pin(DEMO_ADMIN_PASSWORD if r == "Admin" else "1234") if r not in ("Viewer", "Listener") else ""} for n, r in DEMO_USERS]
            write_json(self.users_path, {"users": users})
            log.warning("Created %s with the demo users (PIN 1234; Admin password %s). Change them before real use.", self.users_path, DEMO_ADMIN_PASSWORD)

    def users(self):
        with open(self.users_path, "r", encoding="utf-8") as f:
            return json.load(f)["users"]

    def find_user(self, name):
        return next((u for u in self.users() if u["name"] == name), None)

    def set_pin(self, name, pin):
        with self.lock:
            data = {"users": self.users()}
            u = next((x for x in data["users"] if x["name"] == name), None)
            if not u:
                raise KeyError(name)
            u["pin"] = hash_pin(pin) if pin else ""
            write_json(self.users_path, data)

    def public(self):
        """Everyone, without their PIN hashes: for Users & PINs."""
        return [{"name": u["name"], "role": u["role"], "has_pin": bool(u.get("pin")), "active": u.get("active", True),
                 "chat": u.get("chat", u["role"] != "Listener"), "must_change": bool(u.get("must_change")), "last": u.get("last", "")} for u in self.users()]

    def save_all(self, people, removed, roles):
        """Users & PINs, saved together: people = [{"old", "name", "role", "pin"?, "active", "chat", "must_change"}] ("old" is
        "" for someone new), removed = names. -> error or None. A new PIN is 4-6 digits; an Admin's password 8+ characters."""
        if not isinstance(people, list) or not isinstance(removed, list) or len(people) > 100:
            return "Bad request"
        with self.lock:
            cur = {u["name"]: u for u in self.users()}
            out, names = [], set()
            for p in people:
                if not isinstance(p, dict):
                    return "Bad request"
                name, old, role = (p.get("name") or "").strip(), p.get("old") or "", p.get("role")
                if not name or len(name) > 40:
                    return "Each person needs a name, up to 40 characters."
                if name in names:
                    return "Two people are called %s." % name
                names.add(name)
                if role not in roles:
                    return "%s: there's no role called %s." % (name, role)
                if old and old not in cur:
                    return "%s isn't on the list any more. Reload and try again." % old
                u = dict(cur.get(old) or {"pin": ""})
                u.update(name=name, role=role, active=bool(p.get("active", True)), chat=bool(p.get("chat", role != "Listener")),
                         must_change=bool(p.get("must_change")))
                pin = p.get("pin")
                if isinstance(pin, str) and pin:
                    if role == "Admin" and not valid_password(pin):
                        return "%s: an Admin's password needs 8 or more characters, with a letter and a number or symbol." % name
                    if role != "Admin" and not (pin.isdigit() and 4 <= len(pin) <= 6):
                        return "%s: a PIN is 4 to 6 digits." % name
                    u["pin"] = hash_pin(pin)
                if role == "Admin" and not u.get("pin"):
                    return "%s: an Admin needs a password." % name
                out.append(u)
            gone = [n for n in cur if n not in {p.get("old") for p in people if p.get("old")}]
            if set(gone) - set(removed):
                return "Reload Users & PINs: someone else changed it."
            if not any(u["role"] == "Admin" and u["active"] for u in out):
                return "Keep at least one Admin who can sign in."
            write_json(self.users_path, {"users": out})
        return None

    def mix_path(self, name):
        # The file name is a hash of the person's name: no path tricks, any characters allowed.
        return os.path.join(self.mix_dir, hashlib.sha256(name.encode("utf-8")).hexdigest()[:32] + ".json")

    def get_mixes(self, name):
        p = self.mix_path(name)
        if not os.path.exists(p):
            return {"name": name, "version": 0, "saves": None, "mymix": None, "updated": ""}
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def put_mixes(self, name, saves=None, mymix=None):
        with self.lock:
            cur = self.get_mixes(name)
            if saves is not None:
                cur["saves"] = saves
            if mymix is not None:
                cur["mymix"] = mymix
            cur["version"] = int(cur.get("version", 0)) + 1
            cur["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cur["name"] = name
            write_json(self.mix_path(name), cur)
            return cur


class Sessions:
    def __init__(self, hours):
        self.ttl = hours * 3600
        self.items = {}
        self.fails = {}
        self.lock = threading.Lock()

    def new(self, user):
        tok = secrets.token_urlsafe(24)
        with self.lock:
            self.items[tok] = {"name": user["name"], "role": user["role"], "exp": time.time() + self.ttl}
        return tok, self.items[tok]

    def get(self, tok):
        with self.lock:
            s = self.items.get(tok or "")
            if s and s["exp"] < time.time():
                del self.items[tok]
                s = None
            return s

    def drop(self, tok):
        with self.lock:
            self.items.pop(tok or "", None)

    def locked_out(self, name):
        now = time.time()
        with self.lock:
            recent = [t for t in self.fails.get(name, []) if now - t < FAIL_WINDOW]
            self.fails[name] = recent
            return len(recent) >= FAIL_LIMIT

    def failed(self, name):
        with self.lock:
            self.fails.setdefault(name, []).append(time.time())

    def cleared(self, name):
        with self.lock:
            self.fails.pop(name, None)


def valid_saves(v):
    if not isinstance(v, list) or len(v) > MAX_SAVES:
        return False
    for x in v:
        if not isinstance(x, dict) or not isinstance(x.get("id"), str) or not isinstance(x.get("name"), str):
            return False
        if len(x["name"]) > 40 or len(x["id"]) > 40 or not isinstance(x.get("mix"), dict):
            return False
    return True


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    cfg["app_dir"] = os.path.abspath(cfg["app_dir"])
    cfg["data_dir"] = os.path.abspath(cfg["data_dir"])
    # a list, even if config.json says null or gives one as a plain string
    for k in ("allowed_networks", "remote_networks", "remote_support_roles"):
        v = cfg.get(k)
        cfg[k] = [] if v is None else [v] if isinstance(v, str) else list(v)
    if cfg.get("own_network", True):
        for n in _own_networks():
            if n not in cfg["allowed_networks"]:
                cfg["allowed_networks"].append(n)
    cfg["_nets"] = [ipaddress.ip_network(n, strict=False) for n in cfg["allowed_networks"]]
    cfg["_remote"] = [ipaddress.ip_network(n, strict=False) for n in cfg["remote_networks"]]
    return cfg


def _ip_ok(x):
    try:
        return ipaddress.ip_address(x).version == 4
    except ValueError:
        return False


def make_handler(cfg, store, sessions, listen, access, dante=None, tags=None, bible=None, cues=None, reports=None, plan=None, desk=None, tio=None, monitors=None, scanner=None, foyer=None, rta_inputs=None, dante_names=None, lights=None, rta_live=None, midi=None, music=None, helpers=None, health=None, macros=None, recorder=None, pp=None, midi_peers=None, power=None, chat=None, setup=None, md=None, mics=None, buttons=None, network=None, daily=None):
    nets = cfg["_nets"]
    remote_nets = cfg.get("_remote", [])

    class Handler(SimpleHTTPRequestHandler):
        server_version = "C3Life/" + VERSION
        sys_version = ""

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=cfg["app_dir"], **kw)

        def log_message(self, fmt, *args):
            log.info("%s %s", self.client_address[0], fmt % args)

        def client_ok(self):
            ip = self.client_address[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return False
            self.remote = False
            if any(addr in n for n in nets):
                return True
            if any(addr in n for n in remote_nets):
                self.remote = True
                path = self.path.split("?", 1)[0]
                if path == "/project/User.dc.html" and not cfg.get("remote_support_roles"):
                    pass  # the sign-in page is only for remote support; Listen has its own sign-in
                elif path == "/" or any(path == p or (p.endswith("/") and path.startswith(p)) for p in REMOTE_OK):
                    return True
                s = self.support_session()
                if s:
                    if path.startswith("/project/") or path == "/api/health":
                        log.info("remote support: %s opened %s from %s", s["name"], path, ip)
                    return True
                log.warning("refused %s %s: listening from home only", ip, path)
                self.send_error(403, "From outside the booth you can only use Listen.")
                return False
            log.warning("refused %s: not on the booth network", ip)
            self.send_error(403, "Only devices on the booth network can open C3 Life.")
            return False

        def support_session(self):
            """From outside: the signed-in person, if their role may use the whole app (remote support)."""
            roles = cfg.get("remote_support_roles") or []
            if not roles:
                return None
            auth = self.headers.get("Authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            if not tok:  # pages and images: the sign-in cookie
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "c3s":
                        tok = v
            s = sessions.get(tok) if tok else None
            return s if s and s["role"] in roles else None

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def send_json(self, code, body, cookie=None):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if cookie is not None:  # lets remote support open pages, which can't send the sign-in token
                self.send_header("Set-Cookie", "c3s=%s; Path=/; HttpOnly; SameSite=Strict%s" % (cookie, "" if cookie else "; Max-Age=0"))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self, limit=MAX_BODY):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > limit:
                return None
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return None
            return body if isinstance(body, dict) else None

        def session(self, rooms=False, tok=None, guest=False):
            if tok is None:
                auth = self.headers.get("Authorization", "")
                tok = auth[7:] if auth.startswith("Bearer ") else ""
            s = sessions.get(tok)
            if not s and rooms:
                room = listen.room_for(tok)
                if room:
                    s = {"name": "Room: " + room["name"], "role": "Room", "room": room}
            if not s and guest and not self.remote and listen.has_open():
                # Nobody signed in: they can still hear the groups an Admin opened to everyone (in the building).
                s = {"name": "", "role": "Guest", "guest": True}
            if not s:
                self.send_json(401, {"ok": False, "error": "Sign in again."})
            return s, tok

        def level(self, *areas, need=1):
            """Signed in, with at least `need` in one of these Access areas (1 view, 2 control)."""
            s, _ = self.session()
            if not s:
                return None
            if max(access.level(s["role"], a) for a in areas) < need:
                self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                return None
            return s

        def check_parts(self):
            return {"version": VERSION, "start": START, "cfg": cfg, "health": health, "engine": recorder.engine if recorder else None,
                    "recorder": recorder, "scanner": scanner, "dante": dante, "listen": listen}

        def admin(self):
            s, _ = self.session()
            if s and s["role"] != "Admin":
                self.send_json(403, {"ok": False, "error": "Only an Admin can change this."})
                return None
            return s

        def stream(self, sid, tok):
            s, _ = self.session(rooms=True, tok=tok, guest=True)
            if not s:
                return
            src = next((x for x in listen.allowed(s, remote=self.remote) if x["id"] == sid), None)
            if not src:
                self.send_json(403, {"ok": False, "error": "You're not in a group that can hear this."})
                return
            source, q = listen.open(src)
            log.info("listen: %s started %s%s", s["name"] or "someone not signed in", sid, " (from home)" if self.remote else "")
            # Wait for the first sound before saying it's playing: with none, the phone is told why instead
            try:
                first = q.get(timeout=float(cfg.get("listen_first_wait", 6)))
            except _queue.Empty:
                first = None
            if first is None:
                source.remove(q)
                why = source.why()
                log.warning("listen: %s: %s", sid, why)
                self.send_json(503, {"ok": False, "error": why})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(first)
                while True:
                    try:
                        data = q.get(timeout=20)
                    except _queue.Empty:
                        break
                    if data is None:
                        break
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                source.remove(q)
                log.info("listen: %s stopped %s", s["name"], sid)

        def list_directory(self, path):  # never list folders
            self.send_error(404, "Not found")
            return None

        def route(self):
            return self.path.split("?", 1)[0]

        def do_GET(self):
            if not self.client_ok():
                return
            path = self.route()
            if self.path == "/":
                self.send_response(302)
                self.send_header("Location", "/player.html?p=Listen.dc.html" if self.remote and not self.support_session() else cfg["start_page"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/health":
                self.send_json(200, {"ok": True, "service": "c3life", "version": VERSION, "sync": True, "listen": True, "desk": desk.NAME, "remote_support": bool(cfg.get("remote_support_roles")) and bool(remote_nets), "remote": self.remote,
                                     "uptime_s": int(time.time() - START), "app": os.path.basename(cfg["app_dir"]),
                                     "sound": {k: v for k, v in recorder.engine.view().items() if k in ("kind", "status", "reason", "channels")} if recorder and recorder.engine else {}})
                return
            if path.startswith("/listen/") and path.endswith(".mp3"):
                tok = self.path.split("t=", 1)[1].split("&")[0] if "t=" in self.path else ""
                self.stream(path[len("/listen/"):-4], tok)
                return
            if path == "/api/listen/mine":
                s, _ = self.session(rooms=True, guest=True)
                if not s:
                    return
                srcs = listen.allowed(s, remote=self.remote)
                pub = listen.public.public_view()
                self.send_json(200, {"ok": True, "name": s["name"], "remote": self.remote, "room": s.get("room"), "guest": bool(s.get("guest")),
                                     "sources": [dict(x, live=listen.live(x["id"])) for x in srcs],
                                     "public_link": pub["link"] if pub["on"] and pub["link"] else "",
                                     "languages": listen.translator.languages()})
                return
            if path.startswith("/api/listen/captions/"):
                tok = self.path.split("t=", 1)[1].split("&")[0] if "t=" in self.path else None
                s, _ = self.session(rooms=True, tok=tok, guest=True)
                if not s:
                    return
                sid = path[len("/api/listen/captions/"):]
                src = next((x for x in listen.allowed(s, remote=self.remote) if x["id"] == sid), None)
                if not src:
                    self.send_json(403, {"ok": False, "error": "You're not in a group that can hear this."})
                    return
                cap = listen.caption_source(src)
                if not cap:
                    self.send_json(404, {"ok": False, "error": "Subtitles are off for this source."})
                    return
                src = cap  # one captioner per speech source, shared by everything that uses it
                since = self.path.split("since=", 1)[1].split("&")[0] if "since=" in self.path else "0"
                lang = self.path.split("lang=", 1)[1].split("&")[0] if "lang=" in self.path else ""
                if lang and not listen.translator.has(lang):
                    self.send_json(404, {"ok": False, "error": "That language isn't installed on the C3 Life server."})
                    return
                self.send_json(200, dict(listen.captions(src, int(since) if since.isdigit() else 0, lang), ok=True))
                return
            if path == "/api/listen/transcripts":
                if not self.admin():
                    return
                self.send_json(200, {"ok": True, "days": listen.transcripts.days, "transcripts": listen.transcripts.list()})
                return
            if path.startswith("/api/listen/transcript/") and path.endswith(".txt"):
                tok = self.path.split("t=", 1)[1].split("&")[0] if "t=" in self.path else None
                s, _ = self.session(tok=tok)
                if not s:
                    return
                if s["role"] != "Admin":
                    self.send_json(403, {"ok": False, "error": "Only an Admin can download transcripts."})
                    return
                name = path[len("/api/listen/transcript/"):-4]
                text = listen.transcripts.text(name[:10], name[11:]) if len(name) > 11 else None
                if text is None:
                    self.send_json(404, {"ok": False, "error": "No transcript for that day."})
                    return
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="subtitles-%s.txt"' % name)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/listen/public":
                if not self.admin():
                    return
                self.send_json(200, dict(listen.public.public_view(), ok=True))
                return
            if path == "/api/listen/config":
                if not self.admin():
                    return
                c = listen.config()
                paired = {}
                for v in listen.room_tokens().values():
                    paired[v["room"]] = paired.get(v["room"], 0) + 1
                self.send_json(200, dict(c, ok=True, remote_on=bool(remote_nets),
                                         live={x["id"]: {"live": listen.live(x["id"]), "listeners": listen.listeners(x["id"])} for x in c["sources"]},
                                         paired=paired))
                return
            if path == "/api/dante/devices":
                if self.level("Dante"):
                    self.send_json(200, {"ok": True, "devices": dante_names_mod.devices(dante, cfg)})
                return
            if path == "/api/dante/dvs":
                # What Dante routes into each Dante Virtual Soundcard channel, read from the network (read only).
                if not self.admin():
                    return
                self.send_json(200, dict(dante.get(), ok=True))
                return
            if path in ("/api/scripture", "/api/scripture/list", "/api/scripture/passage", "/api/scripture/playlist"):
                if not self.level("Show", "Running order"):
                    return
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                if path == "/api/scripture/passage":
                    try:
                        self.send_json(200, dict(bible.passage(q.get("ref", ""), q.get("v", "")), ok=True))
                    except scripture_mod.NotFound as e:
                        self.send_json(404, {"ok": False, "error": str(e)})
                    return
                if path == "/api/scripture/playlist":
                    try:
                        items = bible.playlist_items(q.get("id", ""))
                    except (OSError, ValueError):
                        self.send_json(502, {"ok": False, "error": "ProPresenter didn’t answer."})
                        return
                    self.send_json(200, {"ok": True, "items": [{"index": i, "name": (x.get("id") or {}).get("name", ""), "type": x.get("type", "")} for i, x in enumerate(items)]})
                    return
                out = {"ok": True, "passages": bible.list(), "showing": bible.showing}
                if path == "/api/scripture":
                    out.update(bible.pp_state(), versions=bible.versions(), default=bible.default_version())
                self.send_json(200, out)
                return
            if path == "/api/pco":
                s = self.level("Running order", "Show")
                if not s:
                    return
                d = plan.status()
                if d["signed_in"] and s["role"] == "Admin":
                    try:
                        d["service_types"] = plan.service_types()
                    except (OSError, ValueError):
                        d["service_types"] = []
                self.send_json(200, dict(d, ok=True))
                return
            if path == "/api/pco/timers":
                if not self.level("Running order", "Show"):
                    return
                if not plan.signed_in():
                    self.send_json(200, {"ok": True, "signed_in": False, "timers": [], "local_tz": pco_mod.local_tz()})
                    return
                try:
                    self.send_json(200, dict(plan.timer_list(), ok=True, signed_in=True))
                except LookupError as e:
                    self.send_json(200, {"ok": True, "signed_in": True, "timers": [], "error": str(e), "local_tz": pco_mod.local_tz()})
                except (OSError, ValueError) as e:
                    self.send_json(502, {"ok": False, "error": "Couldn’t reach Planning Center (%s)." % e.__class__.__name__})
                return
            if path == "/api/reports/mine":
                # the phone that sent them asks how its reports are getting on
                ids = (self.path.split("ids=", 1)[1].split("&")[0] if "ids=" in self.path else "").split(",")
                out = [reports.view(r) for r in (reports.get(i) for i in ids[:20] if re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$", i)) if r]
                self.send_json(200, {"ok": True, "reports": out})
                return
            if path in ("/api/reports", "/api/reports/settings"):
                if not self.admin():
                    return
                if path.endswith("/settings"):
                    self.send_json(200, dict(reports.public_settings(), ok=True))
                else:
                    self.send_json(200, {"ok": True, "reports": [reports.view(r) for r in reports.all()[:100]], "settings": reports.public_settings()})
                return
            if path in ("/api/monitors", "/api/monitors/check", "/api/wing/sends"):
                s, _ = self.session()
                if not s:
                    return
                mixer, own = access.level(s["role"], "Mixer"), access.level(s["role"], "Own monitor mix")
                d = monitors.get()
                if path == "/api/monitors":
                    if mixer >= 1:
                        self.send_json(200, dict(d, ok=True, mine=monitors.mine(s["name"])))
                    elif own >= 1:
                        self.send_json(200, {"ok": True, "buses": monitors.mine(s["name"]), "mine": monitors.mine(s["name"]), "locked": d["locked"]})
                    else:
                        self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                    return
                if path.endswith("/check"):
                    if mixer < 1:
                        self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                        return
                    sends = {b: {int(n): v for n, v in desk.bus_sends(b).items() if n.isdigit()} for b in d["buses"]}  # channels (aux sends are "a<N>")
                    self.send_json(200, {"ok": True, "status": desk.status()["status"], "buses": monitors.check(sends)})
                    return
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                b = (q.get("bus") or [""])[0]
                if not b.isdigit() or not 1 <= int(b) <= desk.KINDS.get("bus", 0):
                    self.send_json(400, {"ok": False, "error": "Which bus?"})
                    return
                b = int(b)
                if mixer < 1 and not (own >= 1 and b in monitors.mine(s["name"])):
                    self.send_json(403, {"ok": False, "error": "That isn't one of your mixes."})
                    return
                st = desk.state()
                self.send_json(200, {"ok": True, "bus": b, "status": st["status"], "mode": st["mode"], "sends": desk.bus_sends(b), "strip": st["strips"]["bus"].get(str(b), {}),
                                     "names": {n: v.get("name", "") for n, v in st["strips"]["ch"].items()}, "locked": d["locked"],
                                     "aux_names": {n: v.get("name", "") for n, v in st["strips"]["aux"].items()}, "eq": desk.bus_eq(b),
                                     "can_change": desk.settings["mode"] == "control" and not monitors.may_change(s["name"], mixer, b) if b in d["buses"] else mixer >= 2 and desk.settings["mode"] == "control"})
                return
            if path == "/api/daily":
                s = self.level("Settings", "RTA")
                if s:
                    self.send_json(200, dict(daily.view(), ok=True, admin=s["role"] == "Admin"))
                return
            if path == "/api/daily/file":
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                ss = sessions.get((q.get("t") or [""])[0])
                if not ss or ss["role"] != "Admin":
                    self.send_json(403, {"ok": False, "error": "Only an Admin can download the reports."})
                    return
                f = daily.file((q.get("day") or [""])[0], (q.get("kind") or [""])[0])
                if not f:
                    self.send_json(404, {"ok": False, "error": "Nothing was logged then."})
                    return
                with open(f, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv" if f.endswith(".csv") else "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(f))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path in ("/api/network", "/api/network/scan"):
                s = self.level("Settings")
                if s:
                    if path.endswith("/scan"):
                        self.send_json(200, {"ok": True, "networks": network.scan()})
                    else:
                        self.send_json(200, dict(network.view(), ok=True, admin=s["role"] == "Admin", internet=syscheck_mod.internet()))
                return
            if path == "/api/midi/buttons":
                s = self.level("Show", "Mixer", "Settings")
                if s:
                    v = midi.view()
                    self.send_json(200, dict(buttons.view(), ok=True, can=access.level(s["role"], "Settings") >= 2, heard=v["heard"][:10],
                                             peers=v["peers"], macros=[{"id": m["id"], "name": m["name"]} for m in macros.d["macros"]],
                                             names={n: x.get("name", "") for n, x in desk.state()["strips"]["ch"].items()}))
                return
            if path == "/api/mics":
                s = self.level("Mixer", "Own monitor mix")
                if s:
                    st = desk.state()
                    self.send_json(200, dict(mics.view({n: v.get("name", "") for n, v in st["strips"]["ch"].items()}), ok=True,
                                             can=access.level(s["role"], "Mixer") >= 2))
                return
            if path == "/api/md/live":
                s, _ = self.session()
                if s:
                    self.send_json(200, dict(md.live(), ok=True))
                else:
                    self.send_json(401, {"ok": False})
                return
            if path == "/api/md":
                s = self.level("Mixer", "Show")
                if s:
                    st = desk.state()
                    self.send_json(200, dict(md.view(), ok=True, can=access.level(s["role"], "Mixer") >= 2,
                                             names={n: v.get("name", "") for n, v in st["strips"]["ch"].items()}, chs=getattr(desk, "KINDS", {}).get("ch", 40),
                                             sources=[{"id": x["id"], "name": x["name"], "captions": bool(x.get("captions"))} for x in listen.config()["sources"]]))
                return
            if path == "/api/setup":
                s, _ = self.session()
                if s:
                    adm = s["role"] == "Admin"
                    self.send_json(200, {"ok": True, "name": setup.name(), "version": VERSION, "up_since": START, "admin": adm,
                                         "backups": setup.list() if adm else [], "drive": (recorder.drives[0]["label"] if recorder and recorder.drives else "")})
                else:
                    self.send_json(401, {"ok": False, "error": "Sign in again."})
                return
            if path == "/api/setup/download":
                # a link, so the token comes in the address (like Listen's streams)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                ss = sessions.get((q.get("t") or [""])[0])
                if not ss or ss["role"] != "Admin":
                    self.send_json(403, {"ok": False, "error": "Only an Admin can download the setup."})
                    return
                f = setup.file((q.get("id") or [""])[0]) if q.get("id") else None
                if q.get("id") and not f:
                    self.send_json(404, {"ok": False, "error": "That backup isn't there any more."})
                    return
                if f:
                    with open(f, "rb") as fh:
                        data, fname = fh.read(), os.path.basename(f)
                else:
                    data, fname = setup.make(ss["name"], "download")
                log.info("setup: %s downloaded %s", ss["name"], fname)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/check":
                if self.level("Mixer", "Settings", "Show", "Lights", "RTA"):
                    self.send_json(200, {"ok": True, "results": syscheck_mod.check(self.check_parts())})
                return
            if path == "/api/wing/patch":
                if self.level("Wing patch"):
                    outs = desk.outputs() if hasattr(desk, "outputs") else {}
                    st = desk.state()
                    used = {}  # "A/3" -> the channel name using that input, so a source reads "A 3 · Lead Vox"
                    for n, v in st["strips"]["ch"].items():
                        if v.get("src_grp") and v.get("src_in"):
                            used.setdefault("%s/%s" % (v["src_grp"], v["src_in"]), v.get("name") or "Ch %s" % n)
                    self.send_json(200, {"ok": True, "status": st["status"], "mode": st.get("mode", ""), "outs": outs, "used": used,
                                         "groups": wing_mod.OUT_SRC, "count": wing_mod.OUTS})
                return
            if path == "/api/rta/inputs":
                if self.level("RTA"):
                    desk.outputs()  # start reading the Wing's card outputs, for "Set it up for me"
                    self.send_json(200, dict(rta_inputs.view(dante, desk), ok=True))
                return
            if path == "/api/rta/live":
                if self.level("RTA"):
                    rta_live.ask()
                    r = recorder.rec if recorder else None
                    self.send_json(200, dict(rta_live.state(), ok=True, rec={"on": True, "secs": round(recorder.now(), 1)} if r else {"on": False}))
                return
            if path == "/api/rta/plan":
                if not self.level("RTA"):
                    return
                which = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("which") or [""])[0]
                if which not in ("mic", "ref"):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                self.send_json(200, dict(rta_patch_mod.plan(which, rta_inputs.get(), desk, dante, cfg), ok=True))
                return
            if path == "/api/foyer":
                if self.level("Foyer", "Show", "Mixer"):
                    self.send_json(200, dict(foyer.view(desk), ok=True))
                return
            if path == "/api/chat":
                s = self.level("Chat")
                if s:
                    u = store.find_user(s["name"]) or {}
                    if not u.get("chat", s["role"] != "Listener"):
                        self.send_json(403, {"ok": False, "error": "Team chat is turned off for you. An Admin can turn it on in Users & PINs."})
                        return
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    try:
                        n = int((q.get("since") or ["0"])[0])
                    except ValueError:
                        n = 0
                    self.send_json(200, {"ok": True, "me": s["name"], "messages": chat.since(n), "can": access.level(s["role"], "Chat") >= 2})
                return
            if path == "/api/users":
                if self.admin() and self.session()[0]:
                    self.send_json(200, {"ok": True, "users": store.public(), "roles": [r["name"] for r in access.roles()]})
                return
            if path == "/api/home":
                # Home: what's on screen, Playback, the band, the room, every device, whether it's recording;
                # each part only for a role that can see that area.
                s, _ = self.session()
                if not s:
                    return
                lv = lambda a: access.level(s["role"], a)
                out = {"ok": True, "can": {"show": lv("Show") >= 2, "mixer": lv("Mixer") >= 2, "foyer": lv("Foyer") >= 2, "lights": lv("Lights") >= 2,
                                           "recording": lv("Recording") >= 2}}
                if lv("Show") >= 1 or lv("Running order") >= 1:
                    st = pp.status()
                    out["pp"] = {k: st[k] for k in ("presentation", "index", "count", "current", "next", "error", "connected")}
                    peers = midi_peers() if midi_peers else []
                    out["playback"] = {"connected": bool(peers), "name": peers[0]["name"] if peers else ""}
                if lv("Mixer") >= 1:
                    n = int(cfg.get("band_dca", 1))
                    dca = ((desk.state().get("strips") or {}).get("dca") or {}).get(str(n)) or {}
                    out["band"] = {"n": n, "name": dca.get("name") or "DCA %d" % n, "muted": bool(dca.get("mute")), "known": "mute" in dca,
                                   "connected": desk.status()["status"] == "connected"}
                if lv("RTA") >= 1:
                    out["dba"] = rta_live.state().get("dba")
                if lv("Foyer") >= 1:
                    fv = foyer.view(desk)
                    out["foyer"] = {"name": fv.get("name"), "a": fv.get("mix_a_name"), "b": fv.get("mix_b_name"), "live": foyer.live(desk)}
                see = lambda area: lv(area) >= 1 or (area == "Mixer" and lv("Own monitor mix") >= 1)
                devs = [d for d in health.devices() if see(d["area"])]
                if recorder and recorder.engine and recorder.engine.on and see("RTA"):
                    ev = recorder.engine.view()
                    devs.append({"id": "engine", "name": "Dante Virtual Soundcard (ASIO)", "ok": ev["status"] == "running", "area": "RTA",
                                 "where": "Recording.dc.html", "why": ev.get("reason") or "", "detail": "%d channels" % ev["channels"]})
                if "playback" in out:
                    devs.append({"id": "playback", "name": "Playback (network MIDI)", "ok": out["playback"]["connected"], "area": "Show",
                                 "where": "NetMidi.dc.html", "why": "Not connected to C3 Life's network MIDI session.", "info": True,
                                 "detail": out["playback"]["name"]})
                if recorder and see("Recording"):
                    rv = recorder.view()
                    devs.append({"id": "drive", "name": "Recording drive", "ok": bool(rv["drive"]), "area": "Recording", "where": "Recording.dc.html",
                                 "why": "Not plugged in.", "info": True, "detail": (rv["drive"] or {}).get("label", "")})
                    new = [d for d in recorder.drives if not d.get("ok")]
                    if new and not rv["drive"]:
                        devs.append({"id": "drive_new", "name": "%s was plugged in" % new[0]["label"], "ok": False, "info": True, "area": "Recording",
                                     "where": "Recording.dc.html", "why": "Choose it on Record to use it for recordings.", "detail": ""})
                    if rv.get("short"):
                        devs.append({"id": "local_full", "name": "This computer is nearly full", "ok": False, "info": True, "alert": True, "area": "Recording", "where": "Recording.dc.html",
                                     "why": "Plug in the recording drive so the recordings on this computer copy across.", "detail": ""})
                    out["rec"] = {"on": bool(rv["recording"]), "secs": (rv["recording"] or {}).get("secs", 0)}
                out["devices"] = devs
                out["app_name"] = setup.name() if setup else "C3 Life"
                out["mics_low"] = mics.view()["low"] if mics else []
                self.send_json(200, out)
                return
            if path == "/api/plan/order":
                if not self.level("Running order"):
                    return
                src = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("from") or ["pp"])[0]
                try:
                    d = plan.order() if src == "pco" else pp.playlist()
                except (OSError, ValueError, LookupError, KeyError) as e:
                    msg = str(e) if isinstance(e, (LookupError, ValueError)) else (pp._why(e) if src == "pp" else "Planning Center didn't answer (%s)." % e)
                    self.send_json(502, {"ok": False, "error": msg})
                    return
                other = set()  # which items are in the other one too
                try:
                    other = {x["title"].strip().lower() for x in (pp.playlist() if src == "pco" else plan.order())["items"]}
                except Exception:
                    other = None
                for x in d["items"]:
                    x["in_other"] = None if other is None else x["title"].strip().lower() in other
                log.info("running order: %s loaded it from %s (%d items)", self.session()[0]["name"], "Planning Center" if src == "pco" else "ProPresenter", len(d["items"]))
                self.send_json(200, dict(d, ok=True, source=src, at=time.strftime("%H:%M")))
                return
            if path == "/api/log":
                if self.level("Running order", "Show", "Mixer"):
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    try:
                        n = int((q.get("since") or ["0"])[0])
                    except ValueError:
                        n = 0
                    self.send_json(200, {"ok": True, "lines": SERVICE_LOG.since(n)})
                return
            if path == "/api/pp/status":
                if self.level("Show", "Running order"):
                    self.send_json(200, dict(pp.status(), ok=True))
                return
            if path == "/api/pp/timers":
                if self.level("Show", "Running order"):
                    try:
                        self.send_json(200, {"ok": True, "timers": pp.timers()})
                    except (OSError, ValueError) as e:
                        self.send_json(502, {"ok": False, "error": pp._why(e), "timers": []})
                return
            if path == "/api/pp/thumb":
                # an <img> can't send the sign-in header, so the token comes in the address (like Listen's streams)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                s, _ = self.session(tok=(q.get("t") or [""])[0])
                if not s:
                    return
                if max(access.level(s["role"], a) for a in ("Show", "Running order")) < 1:
                    self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                    return
                try:
                    i, w = int((q.get("i") or ["0"])[0]), max(80, min(1280, int((q.get("w") or ["320"])[0])))
                    img = pp.thumb((q.get("u") or [""])[0], i, w)
                except (ValueError, OSError) as e:
                    self.send_json(502, {"ok": False, "error": pp._why(e) if isinstance(e, OSError) else "Bad request"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "private, max-age=300")
                self.end_headers()
                self.wfile.write(img)
                return
            if path == "/api/recording":
                s = self.level("Recording")
                if s:
                    out = dict(recorder.view(full=True), ok=True, can=access.level(s["role"], "Recording") >= 2)
                    out["can_play"] = out["can"] and access.level(s["role"], "Mixer") >= 2
                    self.send_json(200, out)
                return
            if path == "/api/macros":
                s = self.level("Macros")
                if s:
                    out = dict(macros.view(), ok=True, can=access.level(s["role"], "Macros") >= 2)
                    if out["can"]:
                        out["choices"] = macros.choices()
                    self.send_json(200, out)
                return
            if path == "/api/devices":
                s, _ = self.session()
                if s:
                    see = lambda area: access.level(s["role"], area) >= 1 or (area == "Mixer" and access.level(s["role"], "Own monitor mix") >= 1)
                    self.send_json(200, {"ok": True, "devices": [d for d in health.devices() if see(d["area"])], "events_mode": health.prefs["events_mode"], "left": health.prefs["left"],
                                         "can": access.level(s["role"], "Settings") >= 2})
                return
            if path == "/api/music":
                s = self.level("Music")
                if s:
                    self.send_json(200, dict(music.view(), ok=True, can=access.level(s["role"], "Music") >= 2, admin=s["role"] == "Admin",
                                             scanning=scanner.state()["running"]))
                return
            if path == "/api/midi":
                if self.level("Show", "Lights", "Mixer"):
                    v = midi.view()
                    self.send_json(200, {"ok": True, "session": v["session"], "port": v["port"], "error": v["error"], "peers": v["peers"], "heard": v["heard"][:20]})
                return
            if path == "/api/lights/midi":
                if self.level("Lights"):
                    self.send_json(200, dict(midi.view(), ok=True))
                return
            if path == "/api/lights":
                if self.level("Lights"):
                    self.send_json(200, dict(lights.view(), ok=True))
                return
            if path == "/api/scan":
                if self.level("Mixer", "Show", "Settings"):
                    self.send_json(200, dict(scanner.state(), ok=True))
                return
            if path == "/api/tio":
                if self.level("Mixer"):
                    self.send_json(200, dict(tio.state(), ok=True))
                return
            if path == "/api/wing/rta":
                s, _ = self.session()
                if not s:
                    return
                st = desk.rta_state()
                if access.level(s["role"], "Mixer") >= 1:
                    self.send_json(200, dict(st, ok=True))
                elif access.level(s["role"], "Own monitor mix") >= 1:
                    # a musician: only while the Wing's RTA is on one of their own mixes
                    mine = st["kind"] == "bus" and st["n"] in monitors.mine(s["name"])
                    self.send_json(200, dict(st, ok=True, rta=st["rta"] if mine else [], mine=mine))
                else:
                    self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                return
            if path == "/api/wing/history":
                if not self.level("Mixer"):
                    return
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                n, since = (q.get("n") or [""])[0], (q.get("since") or ["0"])[0]
                if not n.isdigit() or not 1 <= int(n) <= desk.KINDS.get("ch", 0):
                    self.send_json(400, {"ok": False, "error": "Which channel?"})
                    return
                try:
                    since = float(since)
                except ValueError:
                    since = 0.0
                desk.channel(int(n))  # keeps it open
                self.send_json(200, {"ok": True, "n": int(n), "hist": desk.history(int(n), since), "now": round(time.time(), 2)})
                return
            if path == "/api/wing/tapeq":
                s = self.level("Tap EQ")
                if not s:
                    return
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                n = (q.get("n") or [""])[0]
                if not n.isdigit() or not 1 <= int(n) <= desk.KINDS.get("ch", 0) or not desk.TAP_EQ:
                    self.send_json(400, {"ok": False, "error": "Which channel?" if desk.TAP_EQ else "This desk has no tap EQ."})
                    return
                proc, st = desk.channel(int(n)), desk.state()
                self.send_json(200, {"ok": True, "n": int(n), "status": st["status"], "mode": st["mode"],
                                     "name": st["strips"].get("ch", {}).get(n, {}).get("name", ""),
                                     "eq": {k: v for k, v in proc.items() if k in desk.TAP_EQ},
                                     "can_change": access.level(s["role"], "Tap EQ") >= 2 and desk.settings["mode"] == "control"})
                return
            if path == "/api/wing/channel":
                if not self.level("Mixer"):
                    return
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                n = (q.get("n") or [""])[0]
                if not n.isdigit() or not 1 <= int(n) <= desk.KINDS.get("ch", 0):
                    self.send_json(400, {"ok": False, "error": "Which channel?"})
                    return
                self.send_json(200, dict(desk.status(), ok=True, n=int(n), proc=desk.channel(int(n)), ratios=desk.RATIOS))
                return
            if path in ("/api/wing", "/api/wing/meters"):
                if not self.level("Mixer"):
                    return
                if path.endswith("/meters"):
                    self.send_json(200, dict(desk.meter_state(), ok=True))
                    return
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                since = (q.get("since") or [""])[0]
                if since.lstrip("-").isdigit() and int(since) == desk.version:
                    self.send_json(200, dict(desk.status(), ok=True, changed=False))
                    return
                self.send_json(200, dict(desk.state(), ok=True, changed=True, can_control=access.level(self.session()[0]["role"], "Mixer") >= 2))
                return
            if path == "/api/cues":
                if self.level("Macros", "Mixer"):
                    self.send_json(200, dict(cues.get(), ok=True))
                return
            if path == "/api/cues/check":
                if self.level("Macros", "Mixer"):
                    self.send_json(200, dict(cues_mod.check(cues.get(), cfg, desk), ok=True))
                return
            if path == "/api/nametags":
                if self.level("Show", "Running order"):
                    self.send_json(200, dict(tags.get(), ok=True))
                return
            if path == "/api/access":
                # Anyone on the booth network may see the roles (the menus use them); only an Admin changes them.
                self.send_json(200, {"ok": True, "areas": access_mod.AREAS, "roles": access.roles()})
                return
            if path == "/api/mixes":
                s, _ = self.session()
                if not s:
                    return
                if access.level(s["role"], "Own monitor mix") < 1:
                    self.send_json(403, {"ok": False, "error": "Your role can't use monitor mixes (Access levels)."})
                    return
                m = store.get_mixes(s["name"])
                since = self.path.split("since=", 1)[1].split("&")[0] if "since=" in self.path else ""
                if since.isdigit() and int(since) == int(m.get("version", 0)):
                    self.send_json(200, {"changed": False, "version": m.get("version", 0)})
                    return
                self.send_json(200, {"changed": True, "version": m.get("version", 0), "saves": m.get("saves"), "mymix": m.get("mymix"), "updated": m.get("updated", "")})
                return
            if path.startswith("/api/") or path.startswith("/data") or path.endswith(".json") and "/project/" not in path:
                self.send_error(404, "Not found")
                return
            super().do_GET()

        def do_HEAD(self):
            if not self.client_ok():
                return
            super().do_HEAD()

        def do_POST(self):
            if not self.client_ok():
                return
            path = self.route()
            if path == "/api/login":
                body = self.read_json()
                if not body or not isinstance(body.get("name"), str) or not isinstance(body.get("pin", ""), str):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                name, pin = body["name"], body.get("password") or body.get("pin", "")  # Admins send a password, everyone else a PIN
                if not isinstance(pin, str) or len(pin) > 200:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if sessions.locked_out(name):
                    self.send_json(429, {"ok": False, "error": "Too many wrong PINs. Wait 5 minutes, or ask an Admin."})
                    return
                u = store.find_user(name)
                good = bool(u) and u.get("active", True) and ((not u.get("pin") and not pin) or (u.get("pin") and check_pin(pin, u["pin"])))
                if not good:
                    sessions.failed(name)
                    log.warning("wrong PIN for %r from %s", name, self.client_address[0])
                    self.send_json(401, {"ok": False, "error": "Wrong PIN."})
                    return
                sessions.cleared(name)
                tok, s = sessions.new(u)
                support = self.remote and s["role"] in (cfg.get("remote_support_roles") or [])
                log.info("%s signed in from %s%s", name, self.client_address[0], " (remote support)" if support else "")
                self.send_json(200, {"ok": True, "token": tok, "name": s["name"], "role": s["role"], "expires": int(s["exp"]), "remote_support": support},
                               cookie=tok if support else None)
                return
            if path in ("/api/wing/send", "/api/monitors/fix"):
                s, _ = self.session()
                if not s:
                    return
                mixer = access.level(s["role"], "Mixer")
                body = self.read_json()
                if body is None:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if path.endswith("/fix"):
                    if mixer < 2:
                        self.send_json(403, {"ok": False, "error": "Your role can't do this (Access levels)."})
                        return
                    d = monitors.get()
                    sends = {b: {int(n): v for n, v in desk.bus_sends(b).items() if n.isdigit()} for b in d["buses"]}  # channels (aux sends are "a<N>")
                    n_fixed, err = 0, None
                    for c in monitors.check(sends):
                        for n in c["post"]:
                            err = err or desk.set("ch", n, "send/%d/mode" % c["bus"], "PRE")
                            n_fixed += 1
                        for n in c["linked"]:
                            err = err or desk.set("ch", n, "send/%d/plink" % c["bus"], 0)
                            n_fixed += 1
                    if err:
                        self.send_json(409, {"ok": False, "error": err})
                        return
                    log.info("monitors: %s set %d sends to Pre with pan link off", s["name"], n_fixed)
                    self.send_json(200, {"ok": True, "changed": n_fixed})
                    return
                b, n, f, v = body.get("bus"), body.get("n"), body.get("field"), body.get("value")
                kind = body.get("kind", "ch")  # a channel's send, or an aux input's (the click, a playback)
                if not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= desk.KINDS.get("bus", 0) or not isinstance(n, int) or isinstance(n, bool) or kind not in desk.SENDERS:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if b in monitors.get()["buses"]:
                    err = monitors.may_change(s["name"], mixer, b)
                    if not err and mixer < 2 and access.level(s["role"], "Own monitor mix") < 2:
                        err = "Your role can't change monitor mixes (Access levels)."
                elif mixer >= 2:
                    err = None  # sends to any other bus (the stream, a matrix feed): Mixer control only
                else:
                    err = "Your role can't do this (Access levels)."
                if err:
                    self.send_json(403, {"ok": False, "error": err})
                    return
                if n == 0 and f in desk.BUS_EQ:
                    err = desk.set("bus", b, f, v)  # the mix's own EQ
                elif n == 0:
                    err = desk.set("bus", b, {"lvl": "fdr", "fdr": "fdr", "on": "mute", "mute": "mute", "pan": "pan"}.get(f, "?"), (not v) if f == "on" else v)
                else:
                    err = desk.set(kind, n, "send/%d/%s" % (b, f), v) if f in ("lvl", "on", "pan") else "That can't be changed here."
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                self.send_json(200, {"ok": True})
                return
            if path in ("/api/dante/rename/check", "/api/dante/rename"):
                s = self.level("Dante", need=1 if path.endswith("/check") else 2)
                if not s:
                    return
                body = self.read_json()
                if not isinstance(body, dict):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if path.endswith("/check"):
                    c = dante_names_mod.check(body, dante, cfg)
                    self.send_json(200 if c["ok"] else 400, c)
                    return
                st, out = dante_names_mod.rename(body, body.get("id"), dante, cfg, dante_names)
                for r in out.get("results", []):
                    log.info("dante: %s: %s%s", s["name"], r["text"], "" if r["ok"] else " (failed: %s)" % r["error"])
                self.send_json(st, out)
                return
            if path == "/api/rta/calibrate":
                s = self.level("RTA settings", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                if body.get("clear") is True:
                    rta_live.uncalibrate()
                    log.info("rta: %s cleared the mic's calibration", s["name"])
                    self.send_json(200, dict(rta_live.state(), ok=True, message="Not calibrated any more."))
                    return
                spl, weight = body.get("spl"), body.get("weight", "Z")
                if not isinstance(spl, (int, float)) or isinstance(spl, bool) or not 40 <= spl <= 140 or weight not in ("Z", "A"):
                    self.send_json(400, {"ok": False, "error": "The level is 40 to 140 dB."})
                    return
                ok, msg = rta_live.calibrate(float(spl), weight)
                log.info("rta: %s calibrating: %s", s["name"], msg)
                self.send_json(200 if ok else 409, dict(rta_live.state(), ok=ok, message=msg, **({} if ok else {"error": msg})))
                return
            if path == "/api/power":
                body = self.read_json() or {}
                what = body.get("do") if isinstance(body, dict) else None
                if what == "cycle":
                    s = self.admin() if self.session()[0] else None
                else:
                    s = self.level("Settings", "Mixer", need=2)
                if not s:
                    return
                if what == "off" and daily:
                    daily.send(time.strftime("%Y-%m-%d"), s["name"])  # the day's RTA and activity reports, before the booth shuts down
                err = power.do(what, s["name"])
                self.send_json(200 if not err else 409, {"ok": not err, "pending": power.pending, **({"error": err} if err else {})})
                return
            if path == "/api/chat":
                s = self.level("Chat", need=2)
                if not s:
                    return
                u = store.find_user(s["name"]) or {}
                if not u.get("chat", s["role"] != "Listener"):
                    self.send_json(403, {"ok": False, "error": "Team chat is turned off for you."})
                    return
                body = self.read_json() or {}
                m, err = chat.send(s["name"], s["role"], body.get("text") if isinstance(body, dict) else "", body.get("ch", 0) if isinstance(body, dict) else 0,
                                   body.get("stage") if isinstance(body, dict) else False)
                self.send_json(200 if m else 400, {"ok": bool(m), "message": m, **({"error": err} if err else {})})
                return
            if path == "/api/home/do":
                body = self.read_json() or {}
                what = body.get("do") if isinstance(body, dict) else None
                need = {"play": "Show", "stop": "Show", "band": "Mixer", "mark": None}.get(what, "?")
                if need == "?":
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                s = self.level(need, need=2) if need else self.level("Show", "Running order", "Mixer", "Recording")
                if not s:
                    return
                if what in ("play", "stop"):
                    _, err = macros.step({"dev": "playback", "do": what, "label": what.capitalize()})
                    if not err:
                        log.info("home: %s sent %s to Playback", s["name"], what)
                elif what == "band":
                    n = int(cfg.get("band_dca", 1))
                    err = desk.set("dca", n, "mute", bool(body.get("on")))
                    if not err:
                        log.info("home: %s %s the band (DCA %d)", s["name"], "muted" if body.get("on") else "unmuted", n)
                else:
                    name = (body.get("name") if isinstance(body.get("name"), str) else "") or "Marker"
                    err = None
                    if recorder and recorder.rec:
                        recorder.marker(name[:80], s["name"], quiet=True)
                    log.info("marker: %s (%s)", name[:80], s["name"])
                self.send_json(200 if not err else 409, {"ok": not err, **({"error": err} if err else {})})
                return
            if path in ("/api/pp/trigger", "/api/pp/timer"):
                s = self.level("Show", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                if not isinstance(body, dict):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if path.endswith("/trigger"):
                    err = pp.trigger(body.get("do"), body.get("index"), body.get("layer"), s["name"])
                    out = dict(pp.status(), ok=not err)
                else:
                    err = pp.timer(body.get("id"), body.get("do"), body.get("secs", 0), s["name"])
                    try:
                        out = {"ok": not err, "timers": pp.timers()}
                    except (OSError, ValueError):
                        out = {"ok": not err, "timers": []}
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path.startswith("/api/recording/") and path.split("/")[-1] in ("start", "stop", "marker", "drive", "plan", "play", "stage", "copy"):
                what = path.split("/")[-1]
                s = self.level("Recording", need=2)
                if not s:
                    return
                if what in ("play", "stage") and access.level(s["role"], "Mixer") < 2:
                    self.send_json(403, {"ok": False, "error": "Playing back to the desk needs Mixer control too (Access levels)."})
                    return
                body = self.read_json() or {}
                if not isinstance(body, dict):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                extra = {}
                if what == "start":
                    err = recorder.start(s["name"])
                elif what == "stop":
                    err = recorder.stop(s["name"])
                elif what == "marker":
                    err = recorder.marker(body.get("name") if isinstance(body.get("name"), str) else "", s["name"])
                elif what == "drive":
                    err = recorder.use_drive(body.get("id"), bool(body.get("use")), s["name"])
                elif what == "plan":
                    extra["plan"], err = recorder.play_plan(body.get("name"), body.get("where", "drive"))
                elif what == "play":
                    at, to = body.get("from", 0), body.get("to")
                    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0
                    err = "Bad request" if not num(at) or not (to is None or num(to)) or not isinstance(body.get("loop", False), bool) else \
                        recorder.play(body.get("name"), at, s["name"], body.get("where", "drive"), to, body.get("loop", False),
                                      body.get("part") if isinstance(body.get("part"), str) else "")
                elif what == "copy":
                    err = recorder.copy([(body.get("name"), body.get("where", "drive"))], body.get("to"), s["name"])
                else:
                    err = recorder.back_to_stage(s["name"])
                out = dict(recorder.view(full=True), ok=not err, **extra)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path in ("/api/macros/run", "/api/macros/stop", "/api/macros/undo"):
                s = self.level("Macros", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                err = macros.run(body.get("id"), s["name"]) if path.endswith("/run") else macros.stop(s["name"]) if path.endswith("/stop") else macros.undo(s["name"])
                out = dict(macros.view(), ok=not err)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path == "/api/foyer/do":
                body = self.read_json()
                s = self.level("Foyer setup" if isinstance(body, dict) and body.get("do") == "balance" else "Foyer", need=2)
                if not s:
                    return
                err = foyer.do(desk, body, s["name"], log)
                out = dict(foyer.view(desk), ok=not err)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path in ("/api/helpers/key", "/api/helpers/forget"):
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                if not isinstance(body.get("id"), str) or not isinstance(body.get("key", ""), str):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                err = helpers.pair(body["id"], body.get("key", ""), s["name"]) if path.endswith("/key") else helpers.forget(body["id"], s["name"])
                music.cache = (0.0, None)
                out = dict(music.view(), ok=not err, can=access.level(s["role"], "Music") >= 2, admin=True)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path == "/api/helpers/find":
                s = self.level("Music", "Settings", need=2)
                if not s:
                    return
                started = scanner.start(s["name"])
                self.send_json(200, {"ok": True, "started": started, "scan": scanner.state()})
                return
            if path in ("/api/music/do", "/api/music/propresenter"):
                s = self.level("Music", need=2)
                if not s:
                    return
                err = music.do(self.read_json(), s["name"]) if path == "/api/music/do" else music.open_propresenter(s["name"])
                out = dict(music.view(), ok=not err)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path == "/api/lights/do":
                s = self.level("Lights", need=2)
                if not s:
                    return
                ok, err, msgs = lights.do(self.read_json() or {}, s["name"])
                out = dict(lights.view(), ok=ok, messages=[{"addr": a, "args": x} for a, x in msgs])
                if err:
                    out["error"] = err
                self.send_json(200 if ok else (409 if msgs else 400), out)
                return
            if path == "/api/lights/find":
                if self.admin():
                    self.send_json(200, {"ok": True, "found": lightkey_mod.find(browse=cfg.get("_lightkey_browse"))})
                return
            if path in ("/api/rta/ringout/cut", "/api/rta/ringout/undo"):
                s = self.level("RTA settings", need=2)
                if not s:
                    return
                if access.level(s["role"], "Mixer") < 2:
                    self.send_json(403, {"ok": False, "error": "A Ring-Out cut changes the desk, so it needs Mixer control too (Access levels)."})
                    return
                body = self.read_json() or {}
                if path.endswith("/cut"):
                    err, c = rta_live.ring.cut(desk, body.get("f"), s["name"])
                else:
                    err, c = rta_live.ring.undo(desk, body.get("id"), s["name"]), None
                out = dict(rta_live.ring.view(rta_live.cal["offset"]), ok=not err, cut=c)
                if err:
                    out["error"] = err
                self.send_json(200 if not err else 409, out)
                return
            if path == "/api/wing/patch":
                s = self.level("Wing patch", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                err = desk.set_out(body.get("out"), body.get("grp"), body.get("idx"))
                if not err:
                    log.info("wing: %s patched %s to %s %s", s["name"], body.get("out"), body.get("grp"), body.get("idx"))
                self.send_json(409 if err else 200, {"ok": not err, "error": err or ""})
                return
            if path in ("/api/rta/patch", "/api/rta/unpatch"):
                body = self.read_json() or {}
                which = body.get("which")
                if which not in ("mic", "ref"):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                s, _ = self.session()
                if not s:
                    self.send_json(401, {"ok": False, "error": "Sign in again."})
                    return
                # what this would touch decides who may do it: the Wing needs Wing patch control, Dante needs Dante control
                p = rta_patch_mod.plan(which, rta_inputs.get(), desk, dante, cfg) if path.endswith("/patch") else {"steps": rta_patch_mod.LAST.get(which, [])}
                kinds = {st["kind"] for st in p["steps"]}
                need = ["RTA settings"] + [a for k, a in (("wing", "Wing patch"), ("dante", "Dante")) if k in kinds]
                if any(access.level(s["role"], a) < 2 for a in need):
                    self.send_json(403, {"ok": False, "error": "Your role can't change %s (Access levels)." % " and ".join(need)})
                    return
                if path.endswith("/patch"):
                    st, out = rta_patch_mod.apply(which, body.get("id"), rta_inputs.get(), desk, dante, cfg)
                else:
                    st, out = rta_patch_mod.undo(which, desk, dante)
                for r in out.get("results", []):
                    log.info("rta: %s %s: %s%s", s["name"], "put back" if path.endswith("unpatch") else "patched", r["text"], "" if r["ok"] else " (failed: %s)" % r["error"])
                self.send_json(st, out)
                return
            if path in ("/api/scan", "/api/scan/use", "/api/scan/hide"):
                s = self.admin() if path.endswith("/hide") else self.level("Mixer", "Settings", need=2)
                if not s:
                    return
                body = self.read_json() if path != "/api/scan" else {}
                if body is None:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if path == "/api/scan":
                    started = scanner.start(s["name"])
                    self.send_json(200 if started else 409, {"ok": started, "error": "" if started else "A scan is already running."})
                    return
                if path.endswith("/hide"):
                    ip, hw = body.get("ip"), body.get("hwid") or ""
                    if not isinstance(ip, str) or not _ip_ok(ip) or not isinstance(hw, str) or len(hw) > 40:
                        self.send_json(400, {"ok": False, "error": "Bad request"})
                        return
                    scanner.hide(ip, hw, bool(body.get("hidden", True)))
                    self.send_json(200, dict(scanner.state(), ok=True))
                    return
                ip, port = body.get("ip"), body.get("port")
                if not isinstance(ip, str) or not _ip_ok(ip) or not isinstance(port, int) or isinstance(port, bool):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                err = scanner.use(body.get("kind"), ip, port, s["name"])
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                self.send_json(200, dict(scanner.state(), ok=True))
                return
            if path == "/api/tio/set":
                s = self.level("Mixer", need=2)
                if not s:
                    return
                body = self.read_json()
                if body is None:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                err = tio.set(body.get("n"), body.get("field"), body.get("value"))
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                if body.get("field") == "vph":
                    log.info("tio: %s set phantom power on input %s to %s", s["name"], body.get("n"), "on" if body.get("value") else "off")
                self.send_json(200, {"ok": True})
                return
            if path == "/api/wing/rta":
                s, _ = self.session()
                if not s:
                    return
                body = self.read_json() or {}
                kind, mixer = body.get("kind", "ch"), access.level(s["role"], "Mixer")
                if mixer < 2:
                    # a musician may point it at their own mix (a bus), while mixes aren't locked
                    b = body.get("n")
                    err = "Your role can't do this (Access levels)."
                    if kind == "bus" and isinstance(b, int) and b in monitors.mine(s["name"]) and access.level(s["role"], "Own monitor mix") >= 2:
                        err = monitors.may_change(s["name"], mixer, b)
                    if err:
                        self.send_json(403, {"ok": False, "error": err})
                        return
                err = desk.rta_point(body.get("n"), body.get("tap", "POSTEQ"), kind)
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                self.send_json(200, {"ok": True})
                return
            if path == "/api/wing/tapeq":
                s = self.level("Tap EQ", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                f = body.get("field")
                if f not in desk.TAP_EQ:
                    self.send_json(400, {"ok": False, "error": "That isn't part of the tap EQ."})
                    return
                err = desk.set("ch", body.get("n"), f, body.get("value"))
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                log.info("wing: %s set ch %s tap EQ %s to %r", s["name"], body.get("n"), f, body.get("value"))
                self.send_json(200, {"ok": True})
                return
            if path in ("/api/wing/set", "/api/wing/fade", "/api/wing/find"):
                if path.endswith("/find"):
                    s = self.admin()
                    if s:
                        self.send_json(200, {"ok": True, "found": desk.find()})
                    return
                s = self.level("Mixer", need=2) if path.endswith("/set") else self.level("Mixer", "Macros", need=2)
                if not s:
                    return
                body = self.read_json()
                if body is None:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if path.endswith("/set") and str(body.get("field", "")) in desk.TAP_EQ and access.level(s["role"], "Tap EQ") < 2:
                    self.send_json(403, {"ok": False, "error": "Changing the tap EQ needs Tap EQ control (Access levels)."})
                    return
                if path.endswith("/set"):
                    err = desk.set(body.get("kind", "ch"), body.get("n"), body.get("field"), body.get("value"))
                    if not err and body.get("field") in ("name", "vph", "gain", "col"):
                        log.info("wing: %s set %s %s %s to %r", s["name"], body.get("kind", "ch"), body.get("n"), body.get("field"), body.get("value"))
                else:
                    moves = body.get("moves")
                    err = "Bad request" if not isinstance(moves, list) or not 0 < len(moves) <= 16 or not all(isinstance(m, dict) for m in moves) else desk.fade(moves)
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                self.send_json(200, {"ok": True})
                return
            if path in ("/api/pco/signin", "/api/pco/signout"):
                s = self.admin()
                if not s:
                    return
                if path.endswith("/signout"):
                    plan.sign_out()
                    log.info("pco: %s signed out of Planning Center", s["name"])
                    threading.Thread(target=tags.refresh, daemon=True).start()
                    self.send_json(200, dict(plan.status(), ok=True))
                    return
                body = self.read_json() or {}
                if not all(isinstance(body.get(k), str) and 0 < len(body[k].strip()) <= 200 for k in ("app_id", "secret")):
                    self.send_json(400, {"ok": False, "error": "Type both parts of the Personal Access Token."})
                    return
                err = plan.sign_in(body["app_id"], body["secret"])
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                log.info("pco: %s signed in to Planning Center as %s", s["name"], plan.status()["who"])
                threading.Thread(target=tags.refresh, daemon=True).start()
                self.send_json(200, dict(plan.status(), ok=True))
                return
            if path == "/api/pco/timer":
                s = self.level("Running order", "Show", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                secs = body.get("secs", 0)
                if not isinstance(body.get("id", ""), str) or not isinstance(secs, int) or isinstance(secs, bool) or abs(secs) > 6 * 3600:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                try:
                    err = plan.change(body.get("id", ""), body.get("action", ""), secs)
                except (OSError, ValueError, LookupError) as e:
                    err = "Couldn’t reach Planning Center (%s)." % e.__class__.__name__
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                self.send_json(200, dict(plan.timer_list(), ok=True, signed_in=True))
                return
            if path in ("/api/setup/name", "/api/setup/backup", "/api/setup/restore"):
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                if path.endswith("/name"):
                    err = setup.set_name(body.get("name"), s["name"])
                    self.send_json(400 if err else 200, {"ok": not err, "error": err or "", "name": setup.name()})
                    return
                if path.endswith("/backup"):
                    err, where = setup.backup(s["name"], to_drive=bool(body.get("drive")), label=str(body.get("label") or "")[:30])
                    self.send_json(200, {"ok": not err, "error": err or "", "where": where, "backups": setup.list()})
                    return
                # restoring needs the Admin's password again, like changing Users
                me = store.find_user(s["name"])
                if not me or not isinstance(body.get("password"), str) or not check_pin(body["password"], me.get("pin", "")):
                    sessions.failed(s["name"])
                    self.send_json(401, {"ok": False, "error": "That password isn't right."})
                    return
                err = setup.restore(body.get("id"), s["name"])
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                again = power.do("app", s["name"]) if power else "Restart C3 Life to load it."
                self.send_json(200, {"ok": True, "restarting": not again, "note": "" if not again else "Restored. " + again + " C3 Life loads the restored setup when it starts again."})
                return
            if path == "/api/daily/send":
                s = self.admin()
                if not s:
                    return
                day = (self.read_json() or {}).get("day") or time.strftime("%Y-%m-%d")
                if not isinstance(day, str) or not re.match(r"^\d{4}-\d\d-\d\d$", day):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                err, sent = daily.send(day, s["name"])
                self.send_json(409 if err else 200, dict(daily.view(), ok=not err, error=err or "", sent=sent))
                return
            if path in ("/api/network/join", "/api/network/forget", "/api/network/order"):
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                if path.endswith("/join"):
                    err = network.join(body.get("ssid"), body.get("password") or "", body.get("auth") or "", s["name"])
                elif path.endswith("/forget"):
                    err = network.forget(body.get("ssid"), s["name"])
                else:
                    err = network.order(body.get("ssids"), s["name"])
                self.send_json(409 if err else 200, {"ok": not err, "error": err or ""})
                return
            if path == "/api/midi/buttons/learn":
                if self.level("Settings", need=2):
                    buttons.learn()
                    self.send_json(200, dict(buttons.view(), ok=True))
                return
            if path in ("/api/md/test", "/api/md/clear"):
                s = self.level("Mixer", need=2)
                if not s:
                    return
                (md.test if path.endswith("/test") else md.clear)(s["name"])
                self.send_json(200, dict(md.view(), ok=True))
                return
            if path == "/api/support":
                s = self.level("Mixer", "Settings", "Show", "Lights", "RTA")
                if not s:
                    return
                body = self.read_json() or {}
                note = body.get("note") or ""
                if not isinstance(note, str) or len(note) > 2000:
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                text = syscheck_mod.report_text(s["name"], note.strip(), syscheck_mod.check(self.check_parts()), SERVICE_LOG.since(0)[-200:], cfg)
                r = reports.add({"subject": "Support report from %s" % s["name"], "text": text, "who": s["name"], "page": "Help"}, s["name"])
                log.info("support: %s sent a support report (%s)", s["name"], r["id"])
                self.send_json(200, {"ok": True, "report": reports.view(r)})
                return
            if path == "/api/reports":
                # anyone on the booth network can report an issue, signed in or not
                body = self.read_json(limit=16 * 1024 * 1024)
                err = reports_mod.valid_report(body)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                auth = self.headers.get("Authorization", "")
                s = sessions.get(auth[7:]) if auth.startswith("Bearer ") else None
                r = reports.add(body, s["name"] if s else "")
                log.info("reports: %s reported %r (%d photos)", r["who"], r["subject"], len(r["photos"]))
                self.send_json(200, {"ok": True, "report": reports.view(r)})
                return
            if path == "/api/cues/check":
                # check changes before saving them
                if not self.level("Macros", "Mixer"):
                    return
                body = self.read_json() or {}
                d = {k: body.get(k) for k in ("enabled", "default_how", "cues")}
                err = cues_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                self.send_json(200, dict(cues_mod.check(d, cfg, desk), ok=True))
                return
            if path in ("/api/scripture/add", "/api/scripture/show", "/api/scripture/hide"):
                # Add changes the ProPresenter playlist (the app asks first); Show and Hide only when someone taps.
                s = self.level("Show", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                if path.endswith("/add"):
                    if not all(isinstance(body.get(k), str) for k in ("ref", "look", "playlist")) or not isinstance(body.get("after", -1), (int, type(None))):
                        self.send_json(400, {"ok": False, "error": "Bad request"})
                        return
                    try:
                        rec = bible.add(body["ref"], body.get("v") or "", body["look"], body["playlist"], body.get("after", None), s["name"])
                    except scripture_mod.NotFound as e:
                        self.send_json(409, {"ok": False, "error": str(e)})
                        return
                    except (OSError, ValueError) as e:
                        self.send_json(502, {"ok": False, "error": "ProPresenter didn’t answer (%s)." % e.__class__.__name__})
                        return
                    log.info("scripture: %s added %s (%s) to the ProPresenter playlist", s["name"], rec["reference"], rec["version"])
                    self.send_json(200, {"ok": True, "added": rec, "passages": bible.list()})
                    return
                if path.endswith("/show"):
                    if not isinstance(body.get("id"), str):
                        self.send_json(400, {"ok": False, "error": "Bad request"})
                        return
                    ok, msg = bible.show(body["id"], body.get("slide", 0))
                else:
                    ok, msg = bible.hide()
                self.send_json(200 if ok else 409, {"ok": ok, "error": "" if ok else msg, "showing": bible.showing})
                return
            if path == "/api/nametags/refresh":
                if self.level("Show", "Running order"):
                    self.send_json(200, dict(tags.refresh(), ok=True))
                return
            if path in ("/api/nametags/show", "/api/nametags/hide"):
                # Only ever when someone taps: nothing shows a name tag by itself.
                s = self.level("Show", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                if path.endswith("/show"):
                    if not isinstance(body.get("id"), str):
                        self.send_json(400, {"ok": False, "error": "Bad request"})
                        return
                    ok, msg = tags.show(body["id"])
                else:
                    ok, msg = tags.hide()
                if ok:
                    log.info("nametags: %s %s %s", s["name"], "showed" if path.endswith("/show") else "hid", msg or "the name tag")
                self.send_json(200 if ok else 409, {"ok": ok, "error": "" if ok else msg, "showing": tags.get()["showing"]})
                return
            if path in ("/api/listen/pair", "/api/listen/unpair"):
                if not self.admin():
                    return
                body = self.read_json() or {}
                room = next((r for r in listen.config()["rooms"] if r["id"] == body.get("room")), None)
                if not room:
                    self.send_json(400, {"ok": False, "error": "No such room."})
                    return
                if path.endswith("/pair"):
                    self.send_json(200, {"ok": True, "token": listen.pair_room(room["id"]), "room": room})
                else:
                    listen.unpair_room(room["id"])
                    self.send_json(200, {"ok": True})
                return
            if path in ("/api/listen/public/start", "/api/listen/public/stop"):
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                if path.endswith("/stop"):
                    listen.public.stop()
                    log.info("public: %s stopped the internet feed", s["name"])
                    self.send_json(200, dict(listen.public.public_view(), ok=True))
                    return
                sid = body.get("source") or listen.public.settings()["source"]
                src = next((x for x in listen.config()["sources"] if x["id"] == sid), None)
                err = "No such source." if not src else listen.public.start(src)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("public: %s started the internet feed (%s)", s["name"], sid)
                self.send_json(200, dict(listen.public.public_view(), ok=True))
                return
            if path == "/api/logout":
                s, tok = self.session()
                if s:
                    sessions.drop(tok)
                    self.send_json(200, {"ok": True}, cookie="")
                return
            self.send_error(405, "Not allowed")

        def do_DELETE(self):
            if not self.client_ok():
                return
            path = self.route()
            if path.startswith("/api/scripture/") and len(path) < 60:
                if not self.level("Show", need=2):
                    return
                ok = bible.remove(path[len("/api/scripture/"):])
                self.send_json(200 if ok else 404, {"ok": ok, "passages": bible.list()})
                return
            if path.startswith("/api/listen/transcript/") and path.endswith(".txt"):
                s = self.admin()
                if not s:
                    return
                name = path[len("/api/listen/transcript/"):-4]
                if len(name) > 11 and listen.transcripts.delete(name[:10], name[11:]):
                    log.info("listen: %s deleted the subtitles for %s", s["name"], name)
                    self.send_json(200, {"ok": True, "transcripts": listen.transcripts.list()})
                else:
                    self.send_json(404, {"ok": False, "error": "No transcript for that day."})
                return
            self.send_error(405, "Not allowed")

        def do_PUT(self):
            if not self.client_ok():
                return
            if self.route() == "/api/daily":
                s = self.admin()
                if not s:
                    return
                err = daily.set(self.read_json() or {}, s["name"])
                self.send_json(400 if err else 200, dict(daily.view(), ok=not err, error=err or ""))
                return
            if self.route() == "/api/midi/buttons":
                s = self.level("Settings", need=2)
                if not s:
                    return
                err = buttons.save(self.read_json() or {}, s["name"])
                self.send_json(400 if err else 200, dict(buttons.view(), ok=not err, error=err or ""))
                return
            if self.route() == "/api/mics":
                s = self.level("Mixer", need=2)
                if not s:
                    return
                err = mics.set(self.read_json() or {}, s["name"])
                self.send_json(400 if err else 200, {"ok": not err, "error": err or ""})
                return
            if self.route() == "/api/md":
                s = self.level("Mixer", need=2)
                if not s:
                    return
                err = md.set(self.read_json() or {}, s["name"])
                self.send_json(400 if err else 200, dict(md.view(), ok=not err, error=err or ""))
                return
            if self.route() == "/api/devices":
                s = self.level("Settings", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                err = health.set_prefs(body.get("left"), body.get("events"), body.get("events_mode"))
                if not err:
                    log.info("devices: %s: left out %s, events mode %s", s["name"], ", ".join(sorted(health.left_out())) or "nothing",
                             "on" if health.prefs["events_mode"] else "off")
                self.send_json(400 if err else 200, {"ok": not err, "error": err or ""})
                return
            if self.route() in ("/api/pco/service_type", "/api/pco/length"):
                body = self.read_json() or {}
                if self.route().endswith("/service_type"):
                    if not self.admin():
                        return
                    try:
                        err = plan.set_service_type(str(body.get("name", "")))
                    except (OSError, ValueError) as e:
                        err = "Couldn’t reach Planning Center (%s)." % e.__class__.__name__
                    if err:
                        self.send_json(409, {"ok": False, "error": err})
                        return
                    threading.Thread(target=tags.refresh, daemon=True).start()
                    self.send_json(200, dict(plan.status(), ok=True))
                    return
                s = self.level("Running order", "Show", need=2)
                if not s:
                    return
                secs = body.get("secs")
                if not isinstance(body.get("item"), str) or not isinstance(secs, int) or isinstance(secs, bool) or not 0 < secs <= 6 * 3600:
                    self.send_json(400, {"ok": False, "error": "A length is between 1 second and 6 hours."})
                    return
                err = plan.set_length(body["item"], secs)
                if err:
                    self.send_json(409, {"ok": False, "error": err})
                    return
                log.info("pco: %s changed item %s to %d s in Planning Center", s["name"], body["item"], secs)
                self.send_json(200, dict(plan.timer_list(), ok=True, signed_in=True))
                return
            if self.route() == "/api/reports/settings":
                if not self.admin():
                    return
                err = reports.set_settings(self.read_json() or {})
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                self.send_json(200, dict(reports.public_settings(), ok=True))
                return
            if self.route() == "/api/monitors":
                s = self.level("Mixer", need=2)
                if not s:
                    return
                body = self.read_json()
                d = monitors.get()
                if body is not None:
                    d.update({k: body[k] for k in ("buses", "people", "locked") if k in body})
                err = monitors_mod.valid(d) if body is not None else "Bad request"
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                monitors.save(d)
                log.info("monitors: %s changed monitor mixes%s", s["name"], " (locked)" if d["locked"] else "")
                self.send_json(200, dict(monitors.get(), ok=True))
                return
            if self.route() == "/api/rta/inputs":
                s = self.level("RTA settings", need=2)
                if not s:
                    return
                body = self.read_json()
                d = dict(rta_inputs.get(), **{k: v for k, v in body.items() if k in ("mic", "ref")}) if isinstance(body, dict) else None
                err = rta_inputs_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                rta_inputs.save(d)
                log.info("rta: %s set the room mic to %s (%s %d) and the desk reference to %s (%s %d)", s["name"],
                         d["mic"]["from"], d["mic"]["via"], d["mic"]["ch"], d["ref"]["from"], d["ref"]["via"], d["ref"]["ch"])
                self.send_json(200, dict(rta_inputs.view(dante, desk), ok=True))
                return
            if self.route() == "/api/recording/settings":
                s = self.level("Recording", need=2)
                if not s:
                    return
                body = self.read_json()
                err = recorder.save_settings(body if isinstance(body, dict) else None, s["name"]) if isinstance(body, dict) else "Bad request"
                self.send_json(200 if not err else 400, dict(recorder.view(full=True), ok=not err, **({"error": err} if err else {})))
                return
            if self.route() == "/api/macros":
                s = self.level("Macros", need=2)
                if not s:
                    return
                body = self.read_json(limit=512 * 1024)
                d = dict(macros.get(), **{k: v for k, v in body.items() if k in ("macros", "playback")}) if isinstance(body, dict) else None
                err = macros_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                macros.save(d, s["name"])
                self.send_json(200, dict(macros.view(), ok=True, can=True))
                return
            if self.route() == "/api/rta/ringout":
                s = self.level("RTA settings", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                err = rta_live.ring.settings(on=body.get("on"), sens=body.get("sens"))
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("ringout: %s set the feedback watch %s, %s", s["name"], "on" if rta_live.ring.on else "off", rta_live.ring.sens)
                self.send_json(200, dict(rta_live.ring.view(rta_live.cal["offset"]), ok=True))
                return
            if self.route() == "/api/music":
                s = self.level("Music", need=2)
                if not s:
                    return
                body = self.read_json()
                d = dict(music.get(), **{k: v for k, v in body.items() if k in music_mod.DEFAULT}) if isinstance(body, dict) else None
                err = music_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                music.save(d)
                log.info("music: %s saved %d playlists", s["name"], len(d["playlists"]))
                self.send_json(200, dict(music.view(), ok=True))
                return
            if self.route() == "/api/foyer":
                s = self.level("Foyer setup", need=2)
                if not s:
                    return
                body = self.read_json()
                d = dict(foyer.get(), **body) if isinstance(body, dict) else None
                err = foyer_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                foyer.save(d)
                log.info("foyer: %s set the foyer to %r (mixes bus %d and %d, matrix %d)", s["name"], d["name"].strip(), d["mix_a"], d["mix_b"], d["mtx"])
                self.send_json(200, dict(foyer.view(desk), ok=True))
                return
            if self.route() == "/api/lights/midi":
                s = self.level("Lights", need=2)
                if not s:
                    return
                body = self.read_json()
                d = dict(midi.settings(), **{k: v for k, v in body.items() if k in midi_mod.DEFAULT}) if isinstance(body, dict) else None
                err = midi_mod.valid(d, lights.show)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                was = midi.settings()["follow"]
                midi.save(d)
                if d["follow"] != was:
                    log.info("lights: %s turned Follow Playback %s", s["name"], "on" if d["follow"] else "off")
                self.send_json(200, dict(midi.view(), ok=True))
                return
            if self.route() in ("/api/lights/show", "/api/lights/settings"):
                show = self.route().endswith("/show")
                s = self.level("Lights", need=2) if show else self.admin()
                if not s:
                    return
                body = self.read_json(limit=2 * 1024 * 1024)
                cur = lights.show if show else lights.settings
                d = dict(cur, **{k: v for k, v in body.items() if k in cur}) if isinstance(body, dict) else None
                err = (lightkey_mod.valid_show if show else lightkey_mod.valid_settings)(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                if show:
                    lights.save_show(d)
                    log.info("lights: %s saved the show (%d cues, %d presets, %d fixtures, %d looks)", s["name"], len(d["cues"]), len(d["presets"]), len(d["fixtures"]), len(d["looks"]))
                else:
                    lights.save_settings(d)
                    log.info("lights: %s set the Lightkey link to %s (%s:%s)", s["name"], d["mode"], d["ip"] or "no address", d["port"])
                self.send_json(200, dict(lights.view(), ok=True))
                return
            if self.route() == "/api/scan/settings":
                s = self.admin()
                if not s:
                    return
                body = self.read_json()
                if not isinstance(body, dict) or not isinstance(body.get("auto"), bool):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                scanner.set_auto(body["auto"])
                log.info("scan: %s turned the automatic scan %s", s["name"], "on" if body["auto"] else "off")
                self.send_json(200, dict(scanner.state(), ok=True))
                return
            if self.route() == "/api/tio/settings":
                s = self.admin()
                if not s:
                    return
                body = self.read_json()
                ip = (body or {}).get("ip")
                if body is None or (ip is not None and (not isinstance(ip, str) or (ip.strip() and not _ip_ok(ip.strip())))):
                    self.send_json(400, {"ok": False, "error": "That isn't an IP address, like 192.168.10.40."})
                    return
                err = tio.configure(ip=ip, mode=body.get("mode"), card_from=body.get("card_from"))
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("tio: %s set the Tio to %s%s", s["name"], tio.settings["mode"], " at " + tio.settings["ip"] if tio.settings["ip"] else "")
                self.send_json(200, dict(tio.status(), ok=True))
                return
            if self.route() == "/api/wing/settings":
                s = self.admin()
                if not s:
                    return
                body = self.read_json()
                ip, mode = (body or {}).get("ip"), (body or {}).get("mode")
                if body is None or (ip is not None and (not isinstance(ip, str) or (ip.strip() and not _ip_ok(ip.strip())))):
                    self.send_json(400, {"ok": False, "error": "That isn't an IP address, like 192.168.10.20."})
                    return
                err = desk.configure(ip=ip, mode=mode)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("wing: %s set the Wing to %s%s", s["name"], desk.settings["mode"], " at " + desk.settings["ip"] if desk.settings["ip"] else "")
                self.send_json(200, dict(desk.status(), ok=True))
                return
            if self.route() == "/api/cues":
                s = self.level("Macros", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                d = {k: body.get(k) for k in ("enabled", "default_how", "cues")}
                err = cues_mod.valid(d)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                # "Run straight away" skips the countdown, so only an Admin can choose it
                before = {c["id"]: c["how"] for c in cues.get()["cues"]}
                if s["role"] != "Admin" and any(c["how"] == "now" and before.get(c["id"]) != "now" for c in d["cues"]):
                    self.send_json(403, {"ok": False, "error": "Only an Admin can make a cue run straight away."})
                    return
                # the Run check comes first: nothing is saved if a cue can't work
                chk = cues_mod.check(d, cfg, desk)
                if not chk["ok_all"]:
                    self.send_json(409, {"ok": False, "error": "Run check found problems, so nothing was saved.", "check": chk})
                    return
                cues.save(d)
                log.info("cues: %s saved %d slide cues", s["name"], len(d["cues"]))
                self.send_json(200, dict(d, ok=True))
                return
            if self.route() == "/api/nametags/name":
                s = self.level("Show", "Running order", need=2)
                if not s:
                    return
                body = self.read_json() or {}
                text = body.get("text", "")
                if not isinstance(body.get("item"), str) or not isinstance(text, str) or len(text) > 80:
                    self.send_json(400, {"ok": False, "error": "A name is up to 80 characters."})
                    return
                d = tags.get()
                if not d.get("plan") or not any(t["item_id"] == body["item"] for t in d["tags"]):
                    self.send_json(404, {"ok": False, "error": "That item isn't in this week's plan."})
                    return
                tags.set_name(d["plan"]["id"], body["item"], " ".join(text.split()))
                log.info("nametags: %s typed %r for item %s", s["name"], text, body["item"])
                self.send_json(200, dict(tags.get(), ok=True))
                return
            if self.route() == "/api/nametags/rules":
                if not self.admin():
                    return
                body = self.read_json() or {}
                rules = body.get("rules")
                if rules is not None and not nametags_mod.valid_rules(rules):
                    self.send_json(400, {"ok": False, "error": "Those links weren't understood."})
                    return
                tags.set_rules(rules)
                self.send_json(200, dict(tags.refresh(), ok=True))
                return
            if self.route() == "/api/listen/config":
                if not self.admin():
                    return
                body = self.read_json()
                err = listen_mod.valid_config({k: (body or {}).get(k) for k in ("sources", "groups", "rooms")})
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                listen.save({k: body[k] for k in ("sources", "groups", "rooms")})
                self.send_json(200, {"ok": True})
                return
            if self.route() == "/api/listen/transcripts":
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                err = listen.transcripts.set_days(body.get("days")) if isinstance(body.get("days"), int) else "Choose how many days."
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("listen: %s set saved subtitles to %s days", s["name"], body["days"])
                self.send_json(200, {"ok": True, "days": listen.transcripts.days, "transcripts": listen.transcripts.list()})
                return
            if self.route() == "/api/users":
                s = self.admin()
                if not s:
                    return
                body = self.read_json(limit=256 * 1024) or {}
                me = store.find_user(s["name"])
                # Changing who can sign in needs the Admin's password again, so an unattended signed-in iPad can't.
                if not me or not isinstance(body.get("password"), str) or not check_pin(body["password"], me.get("pin", "")):
                    sessions.failed(s["name"])
                    self.send_json(401, {"ok": False, "error": "That password isn't right."})
                    return
                err = store.save_all(body.get("users"), body.get("removed") or [], [r["name"] for r in access.roles()])
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("users: %s saved Users & PINs (%d people%s)", s["name"], len(body["users"]),
                         ", removed " + ", ".join(body["removed"]) if body.get("removed") else "")
                self.send_json(200, {"ok": True, "users": store.public()})
                return
            if self.route() == "/api/listen/person":
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                me = store.find_user(s["name"])
                # Changing what someone hears needs the Admin's password again, so an unattended signed-in iPad can't.
                if not me or not isinstance(body.get("password"), str) or not check_pin(body["password"], me.get("pin", "")):
                    sessions.failed(s["name"])
                    self.send_json(401, {"ok": False, "error": "That password isn't right."})
                    return
                if not isinstance(body.get("name"), str) or not store.find_user(body["name"]):
                    self.send_json(400, {"ok": False, "error": "No one by that name."})
                    return
                srcs, fixed = body.get("sources", []), body.get("fixed", "")
                if not isinstance(srcs, list) or not all(isinstance(x, str) for x in srcs) or not isinstance(fixed, str):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                err = listen.set_person(body["name"], srcs, fixed)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("listen: %s set what %s can hear: %s", s["name"], body["name"], fixed or ", ".join(srcs) or "groups only")
                self.send_json(200, {"ok": True, "people": listen.config().get("people", {})})
                return
            if self.route() == "/api/listen/public":
                if not self.admin():
                    return
                body = self.read_json()
                if not isinstance(body, dict):
                    self.send_json(400, {"ok": False, "error": "Bad request"})
                    return
                if "source" in body and not any(x["id"] == body["source"] for x in listen.config()["sources"]):
                    self.send_json(400, {"ok": False, "error": "No such source."})
                    return
                before = listen.public.settings()
                err = listen.public.save(body)
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                after = listen.public.settings()
                # The link is only what people are given, so it changes straight away. The rest needs a restart.
                restart = listen.public.on and any(before.get(k) != after.get(k) for k in ("source", "host", "port", "mount", "user", "password", "name"))
                self.send_json(200, dict(listen.public.public_view(), ok=True, restart_needed=restart))
                return
            if self.route() == "/api/access":
                s = self.admin()
                if not s:
                    return
                body = self.read_json() or {}
                me = store.find_user(s["name"])
                if not me or not isinstance(body.get("password"), str) or not check_pin(body["password"], me.get("pin", "")):
                    sessions.failed(s["name"])
                    self.send_json(401, {"ok": False, "error": "That password isn't right."})
                    return
                err = access.save(body.get("roles"))
                if err:
                    self.send_json(400, {"ok": False, "error": err})
                    return
                log.info("access: %s changed the access levels", s["name"])
                self.send_json(200, {"ok": True, "roles": access.roles()})
                return
            if self.route() != "/api/mixes":
                self.send_error(405, "Not allowed")
                return
            s, _ = self.session()
            if not s:
                return
            if access.level(s["role"], "Own monitor mix") < 2:
                self.send_json(403, {"ok": False, "error": "Your role can't change monitor mixes (Access levels)."})
                return
            body = self.read_json()
            if body is None:
                self.send_json(400, {"ok": False, "error": "Bad request"})
                return
            saves, mymix = body.get("saves"), body.get("mymix")
            if saves is not None and not valid_saves(saves):
                self.send_json(400, {"ok": False, "error": "Saved mixes not understood (up to 50, names up to 40 letters)."})
                return
            if mymix is not None and not isinstance(mymix, dict):
                self.send_json(400, {"ok": False, "error": "Mix not understood."})
                return
            m = store.put_mixes(s["name"], saves=saves, mymix=mymix)
            self.send_json(200, {"ok": True, "version": m["version"], "updated": m["updated"]})

        def do_PATCH(self):
            if not self.client_ok():
                return
            self.send_error(405, "Not allowed")

    return Handler


def _my_ips():
    """This PC's own addresses, so a scan doesn't list the C3 Life server as a device without a driver."""
    import socket
    try:
        return sorted(set(socket.gethostbyname_ex(socket.gethostname())[2]))
    except OSError:
        return []


def _own_networks():
    """The private networks this PC is on now: each of its addresses' /24 (192.168.1.0/24, 10.0.5.0/24...)."""
    import socket
    ips = set(_my_ips())
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(("192.0.2.1", 9))  # sends nothing; only picks the address this PC would use
        ips.add(u.getsockname()[0])
        u.close()
    except OSError:
        pass
    out = []
    for ip in sorted(ips):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local:
            n = str(ipaddress.ip_network(ip + "/24", strict=False))
            if n not in out:
                out.append(n)
    return out


def serve(cfg):
    # The audio engine (DVS in ASIO mode, 64 x 64): when it's on, Listen, the RTA and the MD log read it through ffmpeg.
    engine = audio_engine_mod.Engine(cfg, log)
    if engine.on:
        engine.start()
        if not cfg.get("listen_input"):
            cfg["listen_input"] = engine.input_args()
    store = Store(cfg["data_dir"])
    sessions = Sessions(float(cfg.get("session_hours", 12)))
    listen = listen_mod.Listen(cfg["data_dir"], cfg, log)
    access = access_mod.Access(cfg["data_dir"])
    listen.level = lambda role: access.level(role, "Listen")
    dante_names = dante_names_mod.Names(cfg["data_dir"], cfg)  # names kept after a rename, before anything reads them
    dante = dante_mod.DanteReader(cfg, log)
    tags = nametags_mod.NameTags(cfg["data_dir"], cfg, log)
    bible = scripture_mod.Scripture(cfg["data_dir"], cfg, log)
    cues = cues_mod.Cues(cfg["data_dir"])
    reports = reports_mod.Reports(cfg["data_dir"], cfg, log)
    reports.start()  # sends any reports still waiting from before a restart
    plan = pco_mod.Planning(cfg["data_dir"], cfg, log)  # before anything reads the Planning Center token
    desk = desk_mod.make(cfg, cfg["data_dir"], log)  # the sound desk's driver: the Wing, unless config.json says otherwise
    desk.start()
    tio = tio_mod.Tio(cfg["data_dir"], cfg, log)
    tio.start()
    monitors = monitors_mod.Monitors(cfg["data_dir"])
    foyer = foyer_mod.Foyer(cfg["data_dir"])
    rta_inputs = rta_inputs_mod.RtaInputs(cfg["data_dir"])
    lights = lightkey_mod.Lightkey(cfg["data_dir"], cfg, log)
    helpers = helpers_mod.Helpers(cfg["data_dir"], cfg, log)
    music = music_mod.Music(cfg["data_dir"], cfg, log, helpers)
    health = health_mod.Health(cfg, desk, tio, helpers, lights=lights)
    rta_live = rta_live_mod.Analyser(cfg["data_dir"], cfg, log, rta_inputs)
    if int(cfg["port"]) == 0:  # the tests: any free ports, so several servers can run at once
        cfg.setdefault("midi_port", 0)
    midi = midi_mod.Midi(cfg["data_dir"], cfg, log, lights, allowed=lambda ip: any(ipaddress.ip_address(ip) in n for n in cfg["_nets"]))
    midi.start()
    macros = macros_mod.Macros(cfg["data_dir"], cfg, log, desk=desk, music=music, foyer=foyer, lights=lights, midi=midi)
    macros.left_out = health.left_out
    recorder = recorder_mod.Recorder(cfg["data_dir"], cfg, log, engine, desk=desk, dante=dante)
    pp = propresenter_mod.ProPresenter(cfg, log)
    chat = chat_mod.Chat(cfg["data_dir"], log, pp)
    power = power_mod.Power(cfg, log, before=lambda: recorder.rec and recorder.stop("the server power screen"))
    md = mdtalk_mod.MDTalk(cfg["data_dir"], cfg, log, desk, listen=listen, pp=pp)
    md.start()
    mics = shure_mod.Mics(cfg["data_dir"], cfg, log)

    def _mark(arg, who):
        if recorder and recorder.rec:
            recorder.marker("Marker", who, quiet=True)
        log.info("marker: Marker (%s)", who)

    def _lights_go(arg, who):
        ok, err, _ = lights.do({"do": "go"}, who)
        return None if ok else err

    def _describe(do, arg):
        if do == "macro":
            m = next((x for x in macros.d["macros"] if x["id"] == arg), None)
            return "Macro: " + (m["name"] if m else "(deleted)")
        return {"pp_next": "ProPresenter: Next slide", "pp_prev": "ProPresenter: Previous slide", "lights_go": "Lights: Go",
                "play": "Playback: Play", "stop": "Playback: Stop", "marker": "Add a log marker", "ab": "A/B switch"}.get(do, do)
    buttons = midi_buttons_mod.Buttons(cfg["data_dir"], log, describe=_describe, actions={
        "macro": lambda arg, who: macros.run(arg, who),
        "pp_next": lambda arg, who: pp.trigger("next", who=who), "pp_prev": lambda arg, who: pp.trigger("previous", who=who),
        "lights_go": _lights_go,
        "play": lambda arg, who: macros.step({"dev": "playback", "do": "play", "label": "Play"})[1],
        "stop": lambda arg, who: macros.step({"dev": "playback", "do": "stop", "label": "Stop"})[1],
        "marker": _mark})
    midi.buttons = buttons
    buttons.send = lambda m: midi.send(m)  # light an A/B switch's button on the controller

    follow_stop = threading.Event()

    def _follow():
        while not follow_stop.wait(1.0):
            try:
                buttons.follow(desk.state().get("strips", {}).get("ch", {}))
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_follow, daemon=True, name="midi-follow").start()
    network = netwin_mod.Network(log)
    for h in [h for h in log.handlers if isinstance(h, daily_mod.ActivityLog)]:
        log.removeHandler(h)  # one per server (the tests start several)
    log.addHandler(daily_mod.ActivityLog(os.path.join(cfg["data_dir"], "activity")))
    daily = daily_mod.Daily(cfg["data_dir"], cfg, log, rta=rta_live, reports=reports)
    daily.start()
    mics.start()
    setup = setup_mod.Setup(cfg["data_dir"], cfg, log, drives=lambda: list(recorder.drives) if recorder else [], version=VERSION)
    if engine.on:
        recorder.start_loop()  # watches for the recording drive, and ProPresenter's slides when they start or stop it
    cfg.setdefault("_my_ips", _my_ips())
    scanner = scan_mod.Scanner(cfg["data_dir"], cfg, log, wing=desk, tio=tio,
                               wing_find=lambda: desk.find(target=cfg.get("wing_find_target", "255.255.255.255")))
    # Port 0 is only used by the tests (any free port): they scan when they mean to, not by themselves.
    scanner.on_found = [helpers.found, lights.found]  # every C3 Life helper (by its id) and Lightkey (by its Bonjour name)
    if cfg.get("scan_auto", True) and int(cfg["port"]) != 0:
        threading.Thread(target=scanner.loop, daemon=True).start()
        gone = lambda d, name: [name] if d and d.status()["status"] == "no answer" else []
        threading.Thread(target=scanner.watch, args=([lambda: gone(desk, desk.NAME), lambda: gone(tio, "the Tio"), health.check],), daemon=True).start()
    httpd = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), make_handler(cfg, store, sessions, listen, access, dante, tags, bible, cues, reports, plan, desk, tio, monitors, scanner, foyer, rta_inputs, dante_names, lights, rta_live, midi, music, helpers, health, macros, recorder, pp,
                                                                              lambda: [p for p in midi.peers.values() if p.get("data") and time.time() - p["seen"] < 60], power, chat, setup, md, mics, buttons, network, daily))
    httpd.daemon_threads = True
    close = httpd.server_close
    httpd.server_close = lambda: (scanner.stop.set(), follow_stop.set(), md.stop_ev.set(), daily.stop_ev.set(), mics.stop_ev.set(), lights.close(), rta_live.stop(), midi.close(), recorder.close(), engine.stop(), close())
    httpd.store, httpd.sessions, httpd.listen, httpd.access, httpd.tags, httpd.bible, httpd.reports, httpd.plan, httpd.desk, httpd.tio, httpd.monitors, httpd.scanner, httpd.foyer, httpd.rta_inputs, httpd.lights, httpd.rta_live, httpd.midi, httpd.music, httpd.helpers, httpd.health, httpd.macros = store, sessions, listen, access, tags, bible, reports, plan, desk, tio, monitors, scanner, foyer, rta_inputs, lights, rta_live, midi, music, helpers, health, macros
    httpd.md, httpd.setup, httpd.mics, httpd.buttons, httpd.network, httpd.daily = md, setup, mics, buttons, network, daily
    httpd.wing = desk  # the tests, and older code, call it this
    httpd.engine, httpd.recorder, httpd.pp, httpd.power = engine, recorder, pp, power
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="C3 Life web server")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--log", default="")
    ap.add_argument("--set-pin", metavar="NAME", help="set a person's PIN (asks for it), then exit")
    a = ap.parse_args(argv)
    handlers = []
    if sys.stderr is not None:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler(sys.stderr))
    if a.log:
        os.makedirs(os.path.dirname(a.log), exist_ok=True)
        handlers.append(logging.FileHandler(a.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers or [logging.NullHandler()])
    cfg = load_config(a.config)
    if a.set_pin:
        import getpass
        store = Store(cfg["data_dir"])
        u = store.find_user(a.set_pin)
        if not u:
            raise SystemExit("No one called %s." % a.set_pin)
        if u["role"] == "Admin":
            pin = getpass.getpass("New password for %s (8 or more characters): " % a.set_pin)
            if not valid_password(pin):
                raise SystemExit("An Admin password needs 8 or more characters, with at least one letter and one number or symbol.")
            if getpass.getpass("Type it again: ") != pin:
                raise SystemExit("They don't match. Nothing changed.")
        else:
            pin = getpass.getpass("New PIN for %s (4 digits, blank for none): " % a.set_pin)
            if pin and not (pin.isdigit() and len(pin) == 4):
                raise SystemExit("A PIN is 4 digits.")
        store.set_pin(a.set_pin, pin)
        print("Password changed for" if u["role"] == "Admin" else "PIN changed for", a.set_pin)
        return
    if not os.path.isdir(cfg["app_dir"]):
        raise SystemExit("App folder not found: " + cfg["app_dir"])
    httpd = serve(cfg)
    log.info("C3 Life %s serving %s on %s:%s (mixes in %s)", VERSION, cfg["app_dir"], cfg["bind"], cfg["port"], cfg["data_dir"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
