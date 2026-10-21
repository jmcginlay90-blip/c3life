"""Access levels: what each role may do, kept on the C3 Life server so every device shows and enforces the same.

0 = none, 1 = view, 2 = control, for each area in AREAS. Changed only by an Admin, who types their password again.
"""

import json
import os
import threading

AREAS = ["Mixer", "Wing patch", "Dante", "RTA", "Show", "Lights", "Running order", "Macros", "Chat", "Foyer",
         "Foyer setup", "Settings", "Own monitor mix", "Listen", "RTA settings", "Tap EQ", "Music"]
LISTEN = AREAS.index("Listen")
# RTA settings: where the RTA listens, Set it up for me (with Wing patch and Dante control), the level alert and feedback
# watch. Added after the others, so a saved access.json from before gets each role's usual level for it.
# Tap EQ: the Wing's 3-band EQ on each channel's pre-fader send tap (what the in-ears hear). It belongs to the channel,
# so a change is heard in every monitor mix that has that channel: Control for the sound team, musicians can look.
# Music: Spotify on the Mac mini (Show › Music, through the C3 Life helper) and opening ProPresenter there. Its own area so
# FOH can run the walk-in music without also running slides: Control for Admin, FOH sound and ProPresenter.
NEW_AREA_DEFAULTS = {"RTA settings": {"Admin": 2, "FOH sound": 2, "ProPresenter": 1, "Lighting": 1, "Viewer": 1},
                     "Tap EQ": {"Admin": 2, "FOH sound": 2, "Musician": 1},
                     "Music": {"Admin": 2, "FOH sound": 2, "ProPresenter": 2, "Lighting": 1, "Viewer": 1}}

DEFAULT_ROLES = [
    {"name": "Admin", "note": "Everything, including access levels", "p": [2] * 17, "lock": "15 minutes"},
    {"name": "FOH sound", "note": "Sound desk operator", "p": [2, 2, 1, 2, 1, 1, 1, 2, 2, 2, 1, 0, 2, 1, 2, 2, 2], "lock": "Never during a service"},
    {"name": "ProPresenter", "note": "Lyrics and slides operator", "p": [0, 0, 0, 1, 2, 0, 1, 1, 2, 1, 1, 0, 0, 1, 1, 0, 2], "lock": "30 minutes"},
    {"name": "Lighting", "note": "Events only · Christmas and special services", "p": [0, 0, 0, 1, 1, 2, 1, 1, 2, 1, 1, 0, 0, 1, 1, 0, 1], "lock": "30 minutes"},
    {"name": "Foyer team", "note": "Welcome team · only sees the Foyer screen, Listen and chat", "p": [0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 0, 0, 0, 1, 0, 0, 0], "lock": "5 minutes"},
    {"name": "Musician", "note": "Each musician · only their own monitor mix and chat", "p": [0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 2, 0, 0, 1, 0], "lock": "15 minutes"},
    {"name": "Viewer", "note": "Pastor, band leader · look only", "p": [0, 0, 0, 1, 1, 0, 1, 0, 2, 1, 0, 0, 0, 1, 1, 0, 1], "lock": "10 minutes"},
    {"name": "Listener", "note": "Crèche, kids, welcome or overflow helpers · only Listen", "p": [0] * 13 + [1, 0, 0, 0], "lock": "Never"},
]
LOCKS = ("2 minutes", "5 minutes", "10 minutes", "15 minutes", "30 minutes", "Never during a service", "Never")


def valid(roles):
    if not isinstance(roles, list) or not 0 < len(roles) <= 20:
        return "Roles not understood."
    names = set()
    for r in roles:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not 0 < len(r["name"]) <= 40 or r["name"] in names:
            return "Each role needs its own name."
        names.add(r["name"])
        p = r.get("p")
        if not isinstance(p, list) or len(p) != len(AREAS) or not all(x in (0, 1, 2) for x in p):
            return "%s: each area needs None, View or Control." % r["name"]
        if r.get("lock", "Never") not in LOCKS or not isinstance(r.get("note", ""), str) or len(r.get("note", "")) > 120:
            return "%s: not understood." % r["name"]
    admin = next((r for r in roles if r["name"] == "Admin"), None)
    if not admin or any(x != 2 for x in admin["p"]):
        return "Admin must keep Control of everything, so someone can always fix things."
    return ""


class Access:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "access.json")
        self.lock = threading.Lock()

    def roles(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            r = d["roles"]
            old = d.get("areas") or AREAS[:len(r[0]["p"]) if r else 0]
            if old != AREAS:  # saved before an area was added: keep every level, and give the new area its usual one
                for x in r:
                    p = dict(zip(old, x.get("p", [])))
                    x["p"] = [p[a] if a in p else NEW_AREA_DEFAULTS.get(a, {}).get(x.get("name"), 0) for a in AREAS]
            if not valid(r):
                return r
        except (OSError, ValueError, KeyError):
            pass
        return [dict(x) for x in DEFAULT_ROLES]

    def save(self, roles):
        from c3life_server import write_json
        err = valid(roles)
        if err:
            return err
        # Listen: Control is Admin only (Listen settings); for anyone else it means the same as View.
        for r in roles:
            if r["name"] != "Admin" and r["p"][LISTEN] == 2:
                r["p"][LISTEN] = 1
        with self.lock:
            write_json(self.path, {"areas": AREAS, "roles": roles})
        return ""

    def level(self, role, area):
        if role == "Admin":
            return 2
        r = next((x for x in self.roles() if x["name"] == role), None)
        return r["p"][AREAS.index(area)] if r else 0
