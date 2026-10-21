"""Issue reports: a subject, what happened, who is reporting, and photos.

Each report is kept on the C3 Life server and sent as its own email as soon as there's internet (every minute
it tries again), to the Admin's report address or to another address given with that report. If an Admin
turns it on, each report is also added to a monday.com board as an item, with the text as an update and the
photos attached. Phones can also send a report themselves (their share sheet, over mobile data), which
needs nothing here.
"""

import base64
import json
import mimetypes
import os
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage

EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")
MAX_PHOTOS, MAX_PHOTO_BYTES = 4, 3 * 1024 * 1024
RETRY_S = 60
MONDAY = "https://api.monday.com/v2"


def valid_email(s):
    return isinstance(s, str) and len(s) <= 200 and bool(EMAIL.match(s.strip()))


def valid_report(b):
    """'' if fine, else what's wrong."""
    if not isinstance(b, dict):
        return "Report not understood."
    for k, n in (("subject", 120), ("who", 80)):
        if not isinstance(b.get(k), str) or not b[k].strip() or len(b[k]) > n:
            return "Give a subject and who is reporting." if k == "subject" else "Say who is reporting."
    if not isinstance(b.get("text"), str) or not b["text"].strip() or len(b["text"]) > 5000:
        return "Say what happened (up to 5000 letters)."
    if b.get("to") not in (None, "") and not valid_email(b["to"]):
        return "That email address doesn’t look right."
    photos = b.get("photos") or []
    if not isinstance(photos, list) or len(photos) > MAX_PHOTOS:
        return "Up to %d photos." % MAX_PHOTOS
    for p in photos:
        if not isinstance(p, dict) or not isinstance(p.get("data"), str) or p.get("type") not in ("image/jpeg", "image/png", "image/heic", "image/webp"):
            return "Photos must be pictures (JPEG or PNG)."
        if len(p["data"]) * 3 // 4 > MAX_PHOTO_BYTES:
            return "A photo is too big (3 MB each at most)."
    for k in ("page", "device"):
        if not isinstance(b.get(k, ""), str) or len(b.get(k, "")) > 200:
            return "Report not understood."
    return ""


class Reports:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.dir = os.path.join(data_dir, "reports")
        os.makedirs(self.dir, exist_ok=True)
        self.sfile = os.path.join(data_dir, "reports-settings.json")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.started = False

    # ---- settings (an Admin changes them in the app; the passwords and tokens stay in config.json) ----
    def settings(self):
        d = {"to": self.cfg.get("report_to", ""), "monday": False, "monday_board": str(self.cfg.get("monday_board", "")), "monday_group": self.cfg.get("monday_group", "")}
        try:
            with open(self.sfile, encoding="utf-8") as f:
                d.update({k: v for k, v in json.load(f).items() if k in d})
        except (OSError, ValueError):
            pass
        return d

    def public_settings(self):
        d = self.settings()
        return dict(d, email_ready=bool(self.cfg.get("smtp_host") and d["to"]), smtp=self.cfg.get("smtp_host", ""),
                    from_addr=self.cfg.get("smtp_from") or self.cfg.get("smtp_user", ""), monday_ready=bool(self.cfg.get("monday_token")))

    def set_settings(self, b):
        d = self.settings()
        if "to" in b:
            if not valid_email(b["to"]):
                return "That email address doesn’t look right."
            d["to"] = b["to"].strip()
        if "monday" in b:
            if not isinstance(b["monday"], bool):
                return "Not understood."
            d["monday"] = b["monday"]
        for k in ("monday_board", "monday_group"):
            if k in b:
                v = str(b[k]).strip()
                if len(v) > 40 or (k == "monday_board" and v and not v.isdigit()):
                    return "A monday.com board id is the number in the board’s web address."
                d[k] = v
        if d["monday"] and not d["monday_board"]:
            return "Give the monday.com board id first."
        with self.lock:
            tmp = self.sfile + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.sfile)
        self.wake.set()
        return ""

    # ---- keeping reports ----
    def _path(self, rid):
        return os.path.join(self.dir, rid)

    def add(self, b, signed_in):
        rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        os.makedirs(self._path(rid))
        photos = []
        for i, p in enumerate(b.get("photos") or []):
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/webp": ".webp"}[p["type"]]
            name = "photo-%d%s" % (i + 1, ext)
            with open(os.path.join(self._path(rid), name), "wb") as f:
                f.write(base64.b64decode(p["data"].split(",")[-1]))
            photos.append(name)
        r = {"id": rid, "subject": b["subject"].strip(), "text": b["text"].strip(), "who": b["who"].strip(), "signed_in": signed_in,
             "to": (b.get("to") or "").strip(), "page": b.get("page", ""), "device": b.get("device", ""), "at": time.strftime("%Y-%m-%d %H:%M"),
             "photos": photos, "email": "waiting", "email_at": "", "email_error": "", "monday": "", "monday_item": "", "tries": 0}
        self._write(r)
        self.wake.set()
        self.start()
        return r

    def _write(self, r):
        tmp = os.path.join(self._path(r["id"]), "report.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=1)
        os.replace(tmp, os.path.join(self._path(r["id"]), "report.json"))

    def get(self, rid):
        try:
            with open(os.path.join(self._path(rid), "report.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def all(self):
        out = []
        for n in sorted(os.listdir(self.dir), reverse=True):
            r = self.get(n)
            if r:
                out.append(r)
        return out

    def view(self, r):
        return {k: r[k] for k in ("id", "subject", "who", "at", "to", "email", "email_at", "email_error", "monday", "photos")}

    # ---- sending ----
    def address(self, r):
        return r["to"] or self.settings()["to"]

    def email(self, r):
        cfg = self.cfg
        if not cfg.get("smtp_host"):
            raise RuntimeError("Email isn’t set up on the C3 Life server yet.")
        to = self.address(r)
        if not to:
            raise RuntimeError("No address to send reports to. An Admin sets it on Report an issue.")
        m = EmailMessage()
        m["Subject"] = "C3 Life issue: " + r["subject"]
        m["From"] = cfg.get("smtp_from") or cfg.get("smtp_user", "")
        m["To"] = to
        m.set_content("%s\n\nReported by: %s%s\nWhen: %s\nScreen: %s\nDevice: %s\n\nSent by C3 Life on the C3 Life server. %d photo(s) attached." % (
            r["text"], r["who"], " (signed in as %s)" % r["signed_in"] if r["signed_in"] and r["signed_in"] != r["who"] else "",
            r["at"], r["page"] or "-", r["device"] or "-", len(r["photos"])))
        for name in r["photos"]:
            with open(os.path.join(self._path(r["id"]), name), "rb") as f:
                data = f.read()
            mt = (mimetypes.guess_type(name)[0] or "image/jpeg").split("/")
            m.add_attachment(data, maintype=mt[0], subtype=mt[1], filename=name)
        port = int(cfg.get("smtp_port") or 587)
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        kw = {"context": ssl.create_default_context()} if port == 465 else {}
        with cls(cfg["smtp_host"], port, timeout=20, **kw) as s:
            if port != 465 and not cfg.get("smtp_plain"):
                s.starttls(context=ssl.create_default_context())
            if cfg.get("smtp_user"):
                s.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
            s.send_message(m)

    def monday_call(self, query, variables):
        req = urllib.request.Request(self.cfg.get("monday_base") or MONDAY, data=json.dumps({"query": query, "variables": variables}).encode(),
                                     headers={"Authorization": self.cfg["monday_token"], "Content-Type": "application/json", "API-Version": "2024-10"})
        with urllib.request.urlopen(req, timeout=20) as f:
            j = json.load(f)
        if j.get("errors") or j.get("error_message"):
            raise RuntimeError("monday.com said: %s" % (j.get("error_message") or j["errors"][0].get("message", "an error")))
        return j["data"]

    def monday_file(self, update_id, path, name):
        boundary = uuid.uuid4().hex
        q = 'mutation ($file: File!) { add_file_to_update (update_id: %s, file: $file) { id } }' % int(update_id)
        with open(path, "rb") as f:
            data = f.read()
        body = b"".join([
            b"--%s\r\nContent-Disposition: form-data; name=\"query\"\r\n\r\n%s\r\n" % (boundary.encode(), q.encode()),
            b"--%s\r\nContent-Disposition: form-data; name=\"variables[file]\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (boundary.encode(), name.encode(), (mimetypes.guess_type(name)[0] or "image/jpeg").encode()),
            data, b"\r\n--%s--\r\n" % boundary.encode()])
        base = (self.cfg.get("monday_base") or MONDAY).rstrip("/")
        req = urllib.request.Request(base + "/file", data=body, headers={"Authorization": self.cfg["monday_token"], "Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=60) as f:
            f.read()

    def to_monday(self, r):
        s = self.settings()
        item = r.get("monday_item")
        if not item:
            d = self.monday_call("mutation ($b: ID!, $g: String, $n: String!) { create_item (board_id: $b, group_id: $g, item_name: $n) { id } }",
                                 {"b": s["monday_board"], "g": s["monday_group"] or None, "n": r["subject"]})
            item = d["create_item"]["id"]
            r["monday_item"] = item
            self._write(r)
        body = "%s<br><br><b>Reported by:</b> %s<br><b>When:</b> %s<br><b>Screen:</b> %s<br><b>Device:</b> %s" % (
            r["text"].replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>"), r["who"], r["at"], r["page"] or "-", r["device"] or "-")
        upd = self.monday_call("mutation ($i: ID!, $t: String!) { create_update (item_id: $i, body: $t) { id } }", {"i": item, "t": body})["create_update"]["id"]
        for name in r["photos"]:
            self.monday_file(upd, os.path.join(self._path(r["id"]), name), name)

    def send_waiting(self):
        """Send every report that hasn't gone yet. Called every minute and straight after a new report."""
        s = self.settings()
        for r in self.all():
            changed = False
            if r["email"] in ("waiting", "failed"):
                try:
                    self.email(r)
                    r.update(email="sent", email_at=time.strftime("%Y-%m-%d %H:%M"), email_error="")
                    self.log.info("reports: sent %r to %s", r["subject"], self.address(r))
                except (OSError, smtplib.SMTPException, RuntimeError) as e:
                    r["tries"] = r.get("tries", 0) + 1
                    r["email_error"] = str(e) if isinstance(e, RuntimeError) else "No internet or the email server said no (%s). Trying again every minute." % e.__class__.__name__
                    r["email"] = "waiting" if not isinstance(e, smtplib.SMTPAuthenticationError) else "failed"
                changed = True
            if s["monday"] and self.cfg.get("monday_token") and r.get("monday") != "added":
                try:
                    self.to_monday(r)
                    r["monday"] = "added"
                except (OSError, ValueError, KeyError, RuntimeError) as e:
                    r["monday"] = "waiting: " + (str(e) if isinstance(e, RuntimeError) else "no internet (%s)" % e.__class__.__name__)
                changed = True
            if changed:
                self._write(r)

    def run(self):
        while True:
            try:
                self.send_waiting()
            except Exception as e:  # never let the loop die
                self.log.error("reports: %s", e)
            self.wake.wait(RETRY_S)
            self.wake.clear()

    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.run, daemon=True).start()
