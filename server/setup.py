"""Saved setup: the app's name, and backups of everything C3 Life keeps (data/*.json: people and PINs, access levels,
macros, cues, drivers, foyer, lights, mixes, settings...), on the C3 Life server and on a plugged-in drive.

A backup is one .c3backup file (a zip) with a manifest.json saying what it is. PINs and passwords stay hashed, as they
are on the server. Recordings, report photos and subtitles aren't in it (they're big, and copied their own ways).

  data/backups/                   the last KEEP backups on the server (one is made before every restore)
  <drive>/C3 Life Backups/        backups copied to a recording drive or USB stick (Record finds the drives)

Restoring puts the backup's files back in data/ and restarts C3 Life to load them (a backup of what was there is made
first, so a restore can be undone by restoring that one).
"""

import io
import json
import os
import re
import shutil
import threading
import time
import zipfile

KEEP = 20
DRIVE_FOLDER = "C3 Life Backups"
SKIP_DIRS = {"backups", "reports", "recordings", "transcripts", "speech-model", "listen-cache"}
MAX_FILE = 5 * 1024 * 1024
EXT = ".c3backup"
SAFE = re.compile(r"^[A-Za-z0-9 _.()-]{1,120}$")


class Setup:
    def __init__(self, data_dir, cfg, log, drives=None, version=""):
        self.data, self.cfg, self.log, self.version = data_dir, cfg, log, version
        self.drives = drives or (lambda: [])
        self.dir = os.path.join(data_dir, "backups")
        self.path = os.path.join(data_dir, "setup.json")
        self.lock = threading.Lock()
        self.d = {"app_name": "C3 Life"}
        try:
            with open(self.path, encoding="utf-8") as fh:
                v = json.load(fh)
            if isinstance(v.get("app_name"), str) and v["app_name"].strip():
                self.d["app_name"] = v["app_name"].strip()[:30]
        except (OSError, ValueError, AttributeError):
            pass

    # ---- the name ----
    def name(self):
        return self.d["app_name"]

    def set_name(self, n, who):
        if not isinstance(n, str) or not n.strip() or len(n.strip()) > 30:
            return "A name is 1 to 30 letters."
        with self.lock:
            self.d["app_name"] = n.strip()
            self._save()
        self.log.info("setup: %s renamed the app %r", who, self.d["app_name"])
        return None

    def _save(self):
        os.makedirs(self.data, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=1)
        os.replace(tmp, self.path)

    # ---- making a backup ----
    def _files(self):
        for root, dirs, files in os.walk(self.data):
            rel = os.path.relpath(root, self.data)
            dirs[:] = [x for x in dirs if not (rel == "." and x in SKIP_DIRS)]
            for f in files:
                full = os.path.join(root, f)
                if f.endswith(".tmp") or os.path.getsize(full) > MAX_FILE:
                    continue
                yield full, os.path.relpath(full, self.data).replace(os.sep, "/")

    def make(self, who, label=""):
        """-> the backup's bytes and its file name"""
        buf = io.BytesIO()
        made = time.strftime("%Y-%m-%d %H:%M")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            n = 0
            for full, rel in self._files():
                z.write(full, "data/" + rel)
                n += 1
            z.writestr("manifest.json", json.dumps({"c3life": True, "version": self.version, "made": made, "by": who, "label": label,
                                                    "app_name": self.name(), "files": n}, indent=1))
        tag = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")[:30]
        return buf.getvalue(), "c3life-setup-%s%s%s" % (time.strftime("%Y-%m-%d-%H%M%S"), "-" + tag if tag else "", EXT)

    def backup(self, who, to_drive=False, label=""):
        """-> (error or None, where it went in words)"""
        data, fname = self.make(who, label)
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, fname), "wb") as fh:
            fh.write(data)
        old = sorted(f for f in os.listdir(self.dir) if f.endswith(EXT))
        for f in old[:-KEEP]:
            try:
                os.remove(os.path.join(self.dir, f))
            except OSError:
                pass
        where = "on the C3 Life server"
        if to_drive:
            d = next(iter(self.drives()), None)
            if not d:
                return "No drive is plugged in. The backup is on the C3 Life server.", where
            try:
                folder = os.path.join(d["root"], DRIVE_FOLDER)
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, fname), "wb") as fh:
                    fh.write(data)
                where = "on the C3 Life server and on %s" % d["label"]
            except OSError as e:
                return "The backup couldn't be copied to %s (%s). It's on the C3 Life server." % (d["label"], e.strerror or e), where
        self.log.info("setup: %s made a backup %s (%s)", who, where, fname)
        return None, where

    # ---- the backups there are ----
    def list(self):
        out = []
        for where, folder, key in [("C3 Life server", self.dir, "server")] + \
                [(d["label"], os.path.join(d["root"], DRIVE_FOLDER), "drive:" + d["id"]) for d in self.drives()]:
            try:
                names = [f for f in os.listdir(folder) if f.endswith(EXT)]
            except OSError:
                continue
            for f in names:
                full = os.path.join(folder, f)
                out.append({"id": key + "/" + f, "name": f, "where": where, "size": os.path.getsize(full), "t": os.path.getmtime(full),
                            "at": time.strftime("%d %b %Y %H:%M", time.localtime(os.path.getmtime(full)))})
        return sorted(out, key=lambda x: -x["t"])

    def file(self, bid):
        """A backup's path from its id, or None (only names in the backup folders: nothing else can be read)."""
        if not isinstance(bid, str) or "/" not in bid:
            return None
        key, _, f = bid.rpartition("/")
        if not SAFE.match(f) or not f.endswith(EXT) or ".." in f:
            return None
        if key == "server":
            folder = self.dir
        else:
            d = next((x for x in self.drives() if "drive:" + x["id"] == key), None)
            if not d:
                return None
            folder = os.path.join(d["root"], DRIVE_FOLDER)
        p = os.path.join(folder, f)
        return p if os.path.isfile(p) else None

    # ---- restoring one ----
    def restore(self, bid, who):
        """-> error or None. The files go back in data/; C3 Life must restart to load them."""
        p = self.file(bid)
        if not p:
            return "That backup isn't there any more."
        try:
            with zipfile.ZipFile(p) as z:
                man = json.loads(z.read("manifest.json"))
                if not man.get("c3life"):
                    return "That file isn't a C3 Life backup."
                names = [n for n in z.namelist() if n.startswith("data/") and not n.endswith("/")]
                for n in names:
                    rel = n[5:]
                    if rel.startswith("/") or ".." in rel.split("/") or ":" in rel or rel.split("/")[0] in SKIP_DIRS:
                        return "That backup has a file C3 Life won't restore (%s)." % rel
                err, _ = self.backup(who, label="before restore")
                if err:
                    return err
                for n in names:
                    dest = os.path.join(self.data, *n[5:].split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(n) as src, open(dest + ".tmp", "wb") as out:
                        shutil.copyfileobj(src, out)
                    os.replace(dest + ".tmp", dest)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            return "That backup couldn't be read (%s)." % e.__class__.__name__
        self.log.warning("setup: %s restored %s (%d files, made %s by %s)", who, os.path.basename(p), len(names), man.get("made", "?"), man.get("by", "?"))
        return None
