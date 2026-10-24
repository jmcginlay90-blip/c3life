"""The two daily reports, emailed to the reports address (Internet & reports):

  RTA report       the room level through the day: dB(A) and dB(Z) every 10 seconds and a spectrum every 5 minutes
                   (data/rta_log/<date>.csv and <date>-spectrum.csv), from the room-mic RTA (rta_live.py), with the
                   day's LAeq, the loudest minute, time over 95 and 100 dB(A), and the same for the service times
  Activity report  everything C3 Life did and who did it (data/activity/<date>.log: the service log, kept all day),
                   errors in a list at the top, then warnings, then everything

Logging the RTA keeps the room mic's ffmpeg running all day ("always", from when C3 Life starts) or only between the
times set. Both reports are sent when someone shuts the booth down (Shutdown › Shut down the C3 Life server), or with
Send now; a day that wasn't sent goes the next morning by itself, as an email (that only sends, it changes nothing).
With no internet they wait on the server like any report. Kept 60 days.
"""

import csv
import io
import json
import logging
import math
import os
import re
import threading
import time

KEEP_DAYS = 60
EVERY = 10          # seconds between level readings
SPECTRUM_EVERY = 300
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ActivityLog(logging.Handler):
    """Every INFO-or-worse line C3 Life logs, into data/activity/<date>.log."""

    def __init__(self, folder):
        super().__init__(logging.INFO)
        self.folder = folder
        self.lk = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def emit(self, rec):
        try:
            msg = rec.getMessage().replace("\n", " ")
        except Exception:
            return
        lvl = "ERROR" if rec.levelno >= logging.ERROR else ("WARN" if rec.levelno >= logging.WARNING else "INFO")
        day = time.strftime("%Y-%m-%d", time.localtime(rec.created))
        line = "%s %s %s\n" % (time.strftime("%H:%M:%S", time.localtime(rec.created)), lvl, msg[:500])
        with self.lk:
            try:
                with open(os.path.join(self.folder, day + ".log"), "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass


def _energy_avg(vals):
    return 10 * math.log10(sum(10 ** (v / 10) for v in vals) / len(vals)) if vals else None


class Daily:
    def __init__(self, data_dir, cfg, log, rta=None, reports=None, now=time.time):
        self.cfg, self.log, self.rta, self.reports, self.now = cfg, log, rta, reports, now
        self.rta_dir = os.path.join(data_dir, "rta_log")
        self.act_dir = os.path.join(data_dir, "activity")
        os.makedirs(self.rta_dir, exist_ok=True)
        os.makedirs(self.act_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "daily_reports.json")
        self.lock = threading.Lock()
        self.settings = {"rta": True, "rta_mode": "always", "start": "09:30", "end": "12:30", "service_start": "10:00", "service_end": "12:00",
                         "spectrum": True, "activity": True, "sent": {}}
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            for k in self.settings:
                if isinstance(d.get(k), type(self.settings[k])):
                    self.settings[k] = d[k]
        except (OSError, ValueError, AttributeError):
            pass
        self.last_spectrum = 0.0
        self.last_row = None
        self.stop_ev = threading.Event()

    # ---- settings ----
    def set(self, b, who):
        if not isinstance(b, dict):
            return "Bad request"
        new = json.loads(json.dumps(self.settings))
        for k, v in b.items():
            if k in ("rta", "spectrum", "activity") and isinstance(v, bool):
                new[k] = v
            elif k == "rta_mode" and v in ("always", "times"):
                new[k] = v
            elif k in ("start", "end", "service_start", "service_end") and isinstance(v, str) and TIME.match(v):
                new[k] = v
            else:
                return "Bad request"
        with self.lock:
            self.settings = new
        self._save()
        self.log.info("reports: %s changed the daily reports (RTA log %s, activity report %s)", who,
                      "off" if not new["rta"] else ("all day" if new["rta_mode"] == "always" else new["start"] + "-" + new["end"]),
                      "on" if new["activity"] else "off")
        return None

    def _save(self):
        with self.lock:
            d = json.loads(json.dumps(self.settings))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, self.path)

    # ---- logging the room ----
    def logging_now(self, t=None):
        st = self.settings
        if not st["rta"] or not self.rta:
            return False
        if st["rta_mode"] == "always":
            return True
        hm = time.strftime("%H:%M", time.localtime(t or self.now()))
        return st["start"] <= hm < st["end"] if st["start"] <= st["end"] else (hm >= st["start"] or hm < st["end"])

    def tick(self):
        t = self.now()
        if not self.logging_now(t):
            return
        self.rta.ask()  # keeps the RTA running while we log
        s = self.rta.state()
        if s.get("dba") is None:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        row = [time.strftime("%H:%M:%S", time.localtime(t)), s["dba"], s["dbz"], (s.get("leq") or {}).get("1m"), "" if s.get("calibrated") else "estimate"]
        self.last_row = row
        self._append(os.path.join(self.rta_dir, day + ".csv"), ["time", "dBA", "dBZ", "LAeq 1 min", "calibrated"], row)
        if self.settings["spectrum"] and t - self.last_spectrum >= SPECTRUM_EVERY and s.get("mic"):
            self.last_spectrum = t
            self._append(os.path.join(self.rta_dir, day + "-spectrum.csv"), ["time"] + ["%.0f Hz" % (20 * 2 ** (i / 12)) for i in range(len(s["mic"]))],
                         [row[0]] + s["mic"])

    @staticmethod
    def _append(path, head, row):
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(head)
            w.writerow(row)

    def run(self):
        while not self.stop_ev.wait(EVERY):
            try:
                self.tick()
                self.catch_up()
            except Exception as e:  # noqa: BLE001 - never stop logging
                self.log.debug("daily: %s", e)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    # ---- the reports ----
    def rows(self, day):
        try:
            with open(os.path.join(self.rta_dir, day + ".csv"), encoding="utf-8") as fh:
                return [r for r in csv.DictReader(fh) if r.get("dBA")]
        except OSError:
            return []

    def rta_text(self, day):
        rows = self.rows(day)
        if not rows:
            return None
        st = self.settings
        a = [float(r["dBA"]) for r in rows]
        svc = [float(r["dBA"]) for r in rows if st["service_start"] <= r["time"][:5] < st["service_end"]]
        over = lambda v, lim: len([x for x in v if x > lim]) * EVERY / 60.0
        mins = {}
        for r in rows:
            mins.setdefault(r["time"][:5], []).append(float(r["dBA"]))
        loud = max(mins.items(), key=lambda kv: _energy_avg(kv[1])) if mins else None
        est = any(r.get("calibrated") == "estimate" for r in rows)
        lines = ["RTA report, %s" % day, "",
                 "Logged %s to %s (%d readings, every %d seconds)%s." % (rows[0]["time"][:5], rows[-1]["time"][:5], len(rows), EVERY,
                                                                          " · the mic isn't calibrated, so these are estimates" if est else ""),
                 "Whole day: LAeq %.1f dB(A), highest %.1f dB(A)%s." % (_energy_avg(a), max(a), " (loudest minute %s, %.1f)" % (loud[0], _energy_avg(loud[1])) if loud else ""),
                 "Time over 95 dB(A): %.0f min · over 100 dB(A): %.0f min." % (over(a, 95), over(a, 100))]
        if svc:
            lines.append("Service %s-%s: LAeq %.1f dB(A), highest %.1f dB(A), %.0f min over 95." % (st["service_start"], st["service_end"], _energy_avg(svc), max(svc), over(svc, 95)))
        lines += ["", "Every 15 minutes (LAeq dB(A)):"]
        q = {}
        for r in rows:
            h, m = r["time"][:2], int(r["time"][3:5]) // 15 * 15
            q.setdefault("%s:%02d" % (h, m), []).append(float(r["dBA"]))
        lines += ["  %s  %.1f" % (k, _energy_avg(v)) for k, v in sorted(q.items())]
        lines += ["", "The readings are attached (CSV, opens in Excel or Numbers)."]
        return "\n".join(lines)

    def activity_lines(self, day):
        try:
            with open(os.path.join(self.act_dir, day + ".log"), encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def activity_text(self, day):
        ls = self.activity_lines(day)
        if not ls:
            return None
        errs = [x for x in ls if " ERROR " in x[:16]]
        warns = [x for x in ls if " WARN " in x[:15]]
        signins = [x for x in ls if "signed in" in x]
        out = ["Activity report, %s" % day, "",
               "%d things logged · %d error%s · %d warning%s · %d sign-in%s." % (len(ls), len(errs), "" if len(errs) == 1 else "s", len(warns), "" if len(warns) == 1 else "s",
                                                                               len(signins), "" if len(signins) == 1 else "s")]
        if errs:
            out += ["", "ERRORS (each says what went wrong):"] + ["  " + x for x in errs]
        if warns:
            out += ["", "Warnings:"] + ["  " + x for x in warns]
        out += ["", "Everything, in order:"] + ["  " + x for x in ls]
        return "\n".join(out)

    def send(self, day, who):
        """Both reports for that day, as two emails. -> (error or None, what was sent)"""
        if not self.reports:
            return "Reports aren't set up.", []
        sent = []
        if self.settings["rta"]:
            t = self.rta_text(day)
            if t:
                files = []
                for suffix in ("", "-spectrum"):
                    p = os.path.join(self.rta_dir, day + suffix + ".csv")
                    if os.path.exists(p):
                        with open(p, "rb") as fh:
                            files.append(("rta-%s%s.csv" % (day, suffix), fh.read()))
                self.reports.add({"subject": "RTA report %s" % day, "text": t, "who": who}, who, files=files, kind="daily")
                sent.append("RTA report")
        if self.settings["activity"]:
            t = self.activity_text(day)
            if t:
                self.reports.add({"subject": "Activity report %s" % day, "text": t, "who": who}, who,
                                 files=[("activity-%s.log" % day, "\n".join(self.activity_lines(day)).encode("utf-8"))], kind="daily")
                sent.append("Activity report")
        if not sent:
            return "Nothing was logged on %s." % day, []
        with self.lock:
            self.settings["sent"][day] = time.strftime("%Y-%m-%d %H:%M")
            for k in sorted(self.settings["sent"])[:-KEEP_DAYS]:
                del self.settings["sent"][k]
        self._save()
        self.log.info("reports: %s sent the %s for %s", who, " and ".join(sent), day)
        return None, sent

    def catch_up(self):
        """A day before today with something logged and nothing sent (the booth wasn't shut down): send it now."""
        today = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        for day in self.days():
            if day < today and day not in self.settings["sent"] and day >= time.strftime("%Y-%m-%d", time.localtime(self.now() - 3 * 86400)):
                self.send(day, "C3 Life (not sent at shut down)")
        self.tidy()

    def days(self):
        out = set()
        for d in (self.rta_dir, self.act_dir):
            for f in os.listdir(d):
                m = re.match(r"^(\d{4}-\d\d-\d\d)(\.csv|\.log)$", f)
                if m:
                    out.add(m.group(1))
        return sorted(out, reverse=True)

    def tidy(self):
        cut = time.strftime("%Y-%m-%d", time.localtime(self.now() - KEEP_DAYS * 86400))
        for d in (self.rta_dir, self.act_dir):
            for f in os.listdir(d):
                if f[:10] < cut and re.match(r"^\d{4}-\d\d-\d\d", f):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass

    def file(self, day, kind):
        if not re.match(r"^\d{4}-\d\d-\d\d$", day or "") or kind not in ("rta", "spectrum", "activity"):
            return None
        p = os.path.join(self.act_dir, day + ".log") if kind == "activity" else os.path.join(self.rta_dir, day + ("-spectrum" if kind == "spectrum" else "") + ".csv")
        return p if os.path.exists(p) else None

    def view(self):
        today = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        return {"settings": self.settings, "logging": self.logging_now(), "last": self.last_row, "today": today,
                "days": [{"day": d, "rta": bool(self.rows(d)), "activity": os.path.exists(os.path.join(self.act_dir, d + ".log")),
                          "sent": self.settings["sent"].get(d, "")} for d in self.days()[:14]]}
