"""The patch list: what's plugged in where, read from the Wing and the Dante network, as a spreadsheet (CSV) laid out
like the church's own patch sheet. It only reads; it never changes anything.

  INPUTS   each Wing channel: FOH, Preamp, Dante (card input), Input Name, Mic / DI / Tracks, Stand, Stereo, where it
           comes from on Dante, and the C3 Life server channel that records it
  OUTPUTS  each Wing card output: what the Wing sends there (a bus, a main, a matrix, an input) and who receives it
  C3 LIFE  each of the C3 Life server's Dante channels: in from, and out to
  DANTE    every route on the network
  CHANGES  what's different since the last patch list

Mic / DI / Tracks and Stand aren't kept anywhere C3 Life can read, so they're typed once (or loaded from the
church's patch sheet) and kept in data/patch_notes.json, by channel name and by channel number."""

import csv
import io
import json
import os
import re
import threading
import time

import patching


def _side_name(grp, i, strips_all):
    """A Wing output source in words: BUS 3 -> "Bus 2 L (Keys)", MAIN 1 -> "Main 1 L", LCL 8 -> "Local in 8"."""
    names = {"BUS": ("bus", "Bus"), "MAIN": ("main", "Main"), "MTX": ("mtx", "Matrix")}
    if grp in names:
        k, word = names[grp]
        n, side = (i + 1) // 2, "L" if i % 2 else "R"
        nm = ((strips_all.get(k) or {}).get(str(n)) or {}).get("name") or ""
        return "%s %d %s" % (word, n, side) + (" (%s)" % nm if nm else ""), nm
    words = {"LCL": "Local in", "AUX": "Aux in", "A": "AES50 A", "B": "AES50 B", "C": "AES50 C", "CRD": "Card in", "USB": "USB in", "OFF": "Off"}
    return ("%s %d" % (words.get(grp, grp), i) if grp != "OFF" else "Off"), ""


class PatchList:
    def __init__(self, data_dir, log, read, seed=None):
        """read() -> (routes, chans, me, wing, strips_all, inputs, outs): the Dante network and the Wing, now."""
        self.dir, self.log, self.read = data_dir, log, read
        self.notes_path = os.path.join(data_dir, "patch_notes.json")
        self.last_path = os.path.join(data_dir, "patchlist_last.json")
        self.lock = threading.Lock()
        self.notes = {"by_name": {}, "by_ch": {}}
        try:
            with open(self.notes_path, encoding="utf-8") as f:
                d = json.load(f)
            self.notes = {"by_name": dict(d.get("by_name") or {}), "by_ch": dict(d.get("by_ch") or {})}
        except (OSError, ValueError, AttributeError):
            if seed and os.path.exists(seed):  # first start: the church's own patch sheet
                try:
                    with open(seed, encoding="utf-8-sig") as f:
                        err, n = self.load_sheet(f.read(), "C3 Life (first start)")
                    if not err:
                        log.info("patch list: loaded the mic and stand of %d channels from %s", n, os.path.basename(seed))
                except OSError:
                    pass

    # ---- the notes: Mic / DI / Tracks, Stand, and the sheet's Preamp ----
    def _save_notes(self):
        tmp = self.notes_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.notes, f, indent=1)
        os.replace(tmp, self.notes_path)

    def load_sheet(self, text, who):
        """The church's patch sheet (CSV: FOH, Preamp, Dante, Input Name, Mic / Di / Tracks, Stand, ...). -> (error, channels read)"""
        if not isinstance(text, str) or not text.strip():
            return "That file is empty.", 0
        if len(text) > 500000:
            return "That file is too big for a patch sheet.", 0
        rows = list(csv.reader(io.StringIO(text)))
        head = next((i for i, r in enumerate(rows) if len(r) > 5 and r[0].strip().upper() == "FOH"), None)
        if head is None:
            return "That doesn't look like a patch sheet: there's no row starting FOH, Preamp, Dante, Input Name.", 0
        by_name, by_ch = {}, {}
        for r in rows[head + 1:]:
            r = (r + [""] * 6)[:6]
            foh, pre, dante, name, mic, stand = (x.strip() for x in r)
            if not (foh or name):
                continue
            if not foh.isdigit():  # the right side of a stereo pair: same channel as the row above
                continue
            note = {k: v for k, v in (("mic", mic), ("stand", stand), ("preamp", pre)) if v and v.lower() not in ("x",)}
            if not note:
                continue
            by_ch[foh] = dict(note, name=name)
            if name:
                by_name[name.lower()] = note
        if not by_ch:
            return "No channels with a mic, stand or preamp were found in that sheet.", 0
        with self.lock:
            self.notes = {"by_name": by_name, "by_ch": by_ch}
            self._save_notes()
        self.log.info("patch list: %s loaded a patch sheet (%d channels)", who, len(by_ch))
        return None, len(by_ch)

    def set_note(self, ch, name, mic, stand, who):
        if not isinstance(ch, int) or isinstance(ch, bool) or not 1 <= ch <= 48:
            return "Choose a channel."
        for v in (mic, stand):
            if not isinstance(v, str) or len(v) > 60:
                return "Keep it to 60 letters."
        with self.lock:
            note = {k: v.strip() for k, v in (("mic", mic), ("stand", stand)) if v.strip()}
            old = self.notes["by_ch"].get(str(ch)) or {}
            self.notes["by_ch"][str(ch)] = dict(note, name=name or "", **({"preamp": old["preamp"]} if old.get("preamp") else {}))
            if name:
                self.notes["by_name"][name.lower()] = dict(note, **({"preamp": old["preamp"]} if old.get("preamp") else {}))
            self._save_notes()
        self.log.info("patch list: %s noted channel %d: %s, %s", who, ch, mic or "-", stand or "-")
        return None

    def _note(self, n, name):
        """The mic and stand for a Wing channel: by its name, or by its number when the sheet's name for that number
        is the same (so a channel that was renamed or moved doesn't get someone else's mic)."""
        key = lambda x: re.sub(r"[^a-z0-9]", "", re.sub(r"\s+[lr]$", "", (x or "").strip().lower()))
        with self.lock:
            nb = self.notes["by_name"].get((name or "").lower())
            if nb:
                return dict(nb)
            c = self.notes["by_ch"].get(str(n)) or {}
            return dict(c) if c and (not c.get("name") or key(c["name"]) == key(name)) else {}

    # ---- the list ----
    def build(self):
        routes, chans, me, wing, strips_all, inputs, outs = self.read()
        wl, ml = wing.lower(), me.lower()
        wing_rx = (chans.get(wing) or {}).get("rx") or {}
        wing_tx = (chans.get(wing) or {}).get("tx") or {}
        into = {}  # (rx device, rx channel number) -> route
        for r in routes:
            if r["rx_device"].lower() == wl:
                n = patching._num(r["rx_channel"], wing_rx)
                if n:
                    into[("wing", n)] = r
        to_me = {}  # (tx device, tx channel) -> this server's channel number
        for r in routes:
            if ml and r["rx_device"].lower() == ml:
                k = patching._num(r["rx_channel"], (chans.get(me) or {}).get("rx") or {})
                if k:
                    to_me[(r["tx_device"].lower(), r["tx_channel"])] = k
        inputs_rows = []
        strips = strips_all.get("ch") or {}
        aux = strips_all.get("aux") or {}
        for kind, table in (("", strips), ("Aux ", aux)):
            for n in sorted(table, key=int):
                s = table[n]
                g, i, name = s.get("src_grp"), s.get("src_in"), s.get("name") or ""
                if not name and (not g or g == "OFF"):
                    continue
                st = isinstance(i, int) and (inputs.get("%s/%d" % (g, i)) or {}).get("mode") == "ST"
                note = self._note(n, name) if not kind else {}
                for side, idx in ((("L", i), ("R", i + 1)) if st else (("", i),)):
                    src, dante, rec = "", "", ""
                    if g == "CRD" and isinstance(idx, int):
                        dante = str(idx)
                        r = into.get(("wing", idx))
                        if r:
                            src = "%s · %s" % (r["tx_device"], r["tx_channel"])
                            k = to_me.get((r["tx_device"].lower(), r["tx_channel"]))
                            rec = "C3 Life %02d" % k if k else ""
                        else:
                            src = "nothing routed"
                    elif g and g != "OFF":
                        src = _side_name(g, idx or 0, strips_all)[0]
                    first = side in ("", "L")
                    inputs_rows.append({"FOH": (kind + str(n)) if first else "", "Preamp": note.get("preamp", "") if first else "",
                                        "Dante": dante, "Input Name": (name + (" " + side if side else "")) if name else "",
                                        "Mic / DI / Tracks": note.get("mic", "") if first else "", "Stand": note.get("stand", "") if first else "",
                                        "Stereo": "Stereo" if st and first else "", "Dante source": src, "Recorded on C3 Life": rec})
        outputs_rows = []
        for n in range(1, 65):
            o = outs.get("CRD/%d" % n)
            gets = sorted("%s · %s" % (r["rx_device"], r["rx_channel"]) for r in routes
                          if r["tx_device"].lower() == wl and patching._num(r["tx_channel"], wing_tx) == n)
            if not o and not gets:
                continue
            what, nm = _side_name(o["grp"], int(o.get("in") or 0), strips_all) if o and o.get("grp") else ("not read yet", "")
            outputs_rows.append({"Wing output": "Card out %d" % n, "Sends": what, "Name": wing_tx.get(n, ""), "Goes to": "; ".join(gets) or "nothing"})
        names_me = {}
        hp_rows = []
        my = chans.get(me) or {}
        for k in range(1, 65):
            rin = next((r for r in routes if r["rx_device"].lower() == ml and patching._num(r["rx_channel"], my.get("rx") or {}) == k), None)
            outs_to = sorted("%s · %s" % (r["rx_device"], r["rx_channel"]) for r in routes
                             if r["tx_device"].lower() == ml and patching._num(r["tx_channel"], my.get("tx") or {}) == k)
            if not rin and not outs_to:
                continue
            hp_rows.append({"Channel": k, "In from": "%s · %s" % (rin["tx_device"], rin["tx_channel"]) if rin else "",
                            "Out to": "; ".join(outs_to)})
            names_me[k] = True
        dante_rows = [{"Receiver": r["rx_device"], "Receiver channel": r["rx_channel"], "From device": r["tx_device"], "From channel": r["tx_channel"]}
                      for r in sorted(routes, key=lambda r: (r["rx_device"].lower(), patching._num(r["rx_channel"], {}) or 0, r["rx_channel"]))]
        seen = sorted({r["rx_device"] for r in routes} | {r["tx_device"] for r in routes} | set(chans))
        return {"made": time.strftime("%Y-%m-%d %H:%M"), "wing": wing, "device": me, "devices": seen,
                "inputs": inputs_rows, "outputs": outputs_rows, "hp": hp_rows, "dante": dante_rows,
                "wing_seen": bool(strips), "outs_seen": bool(outs)}

    @staticmethod
    def _keys(pl):
        """Everything worth comparing, by a key that stays the same when nothing moved."""
        out = {}
        for r in pl["inputs"]:
            if r["Dante"] or r["Dante source"]:
                out["Input %s %s" % (r["Input Name"] or r["FOH"], r["Dante"])] = r["Dante source"]
        for r in pl["outputs"]:
            out[r["Wing output"]] = "%s → %s" % (r["Sends"], r["Goes to"])
        for r in pl["dante"]:
            out["%s %s" % (r["Receiver"], r["Receiver channel"])] = "%s · %s" % (r["From device"], r["From channel"])
        return out

    def changes(self, pl):
        try:
            with open(self.last_path, encoding="utf-8") as f:
                last = json.load(f)
        except (OSError, ValueError):
            return None, []
        a, b = last.get("keys") or {}, self._keys(pl)
        out = []
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                out.append({"What": k, "Was": a.get(k) or "(nothing)", "Now": b.get(k) or "(gone)"})
        return last.get("made"), out

    def remember(self, pl):
        tmp = self.last_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"made": pl["made"], "keys": self._keys(pl)}, f, indent=1)
        os.replace(tmp, self.last_path)

    def csv_text(self, pl, since, changes):
        b = io.StringIO()
        w = csv.writer(b)
        w.writerow(["C3 Life patch list", pl["made"], "Wing: %s" % (pl["wing"] or "not found"), "C3 Life server: %s" % (pl["device"] or "not found")])
        w.writerow([])
        w.writerow(["WHAT CHANGED" + (" SINCE %s" % since if since else "")])
        if since is None:
            w.writerow(["This is the first patch list, so there's nothing to compare it with."])
        elif not changes:
            w.writerow(["Nothing changed."])
        else:
            w.writerow(["What", "Was", "Now"])
            for c in changes:
                w.writerow([c["What"], c["Was"], c["Now"]])
        for title, rows, cols in (
                ("INPUTS", pl["inputs"], ["FOH", "Preamp", "Dante", "Input Name", "Mic / DI / Tracks", "Stand", "Stereo", "Dante source", "Recorded on C3 Life"]),
                ("OUTPUTS (Wing card)", pl["outputs"], ["Wing output", "Sends", "Name", "Goes to"]),
                ("C3 LIFE SERVER (the HP)", pl["hp"], ["Channel", "In from", "Out to"]),
                ("DANTE NETWORK (every route)", pl["dante"], ["Receiver", "Receiver channel", "From device", "From channel"])):
            w.writerow([])
            w.writerow([title])
            w.writerow(cols)
            if not rows:
                w.writerow(["(nothing read: is it switched on and on the network?)"])
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
        return b.getvalue()

    def report(self, keep=True):
        """(csv bytes, text for an email, changes, since) for the patch list now; keep=True remembers it for next time."""
        pl = self.build()
        since, changes = self.changes(pl)
        text = self.csv_text(pl, since, changes)
        lines = ["C3 Life patch list, %s" % pl["made"], "",
                 "%d inputs · %d Wing card outputs · %d C3 Life server channels · %d Dante routes." % (
                     len(pl["inputs"]), len(pl["outputs"]), len(pl["hp"]), len(pl["dante"]))]
        if since is None:
            lines += ["", "This is the first patch list."]
        elif changes:
            lines += ["", "%d change%s since %s:" % (len(changes), "" if len(changes) == 1 else "s", since)] + \
                     ["  %s: %s → %s" % (c["What"], c["Was"], c["Now"]) for c in changes[:40]]
        else:
            lines += ["", "Nothing changed since %s." % since]
        if not pl["wing_seen"]:
            lines += ["", "The Wing didn't answer, so the inputs are missing."]
        lines += ["", "The spreadsheet is attached (opens in Excel or Numbers)."]
        if keep:
            self.remember(pl)
        return text.encode("utf-8-sig"), "\n".join(lines), changes, since

    def view(self):
        pl = self.build()
        since, changes = self.changes(pl)
        with self.lock:
            n = len(self.notes["by_ch"])
        return dict(pl, since=since, changes=changes, notes=n)
