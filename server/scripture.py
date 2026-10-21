"""Bible Scripture: look up a passage, preview its slides, put it in the ProPresenter playlist and show it.

ProPresenter's network API can't make slides or use ProPresenter's own Bibles, so C3 Life does it this way:
  - the text comes from C3 Life: KJV and the World English Bible British Edition are on the C3 Life server
    (public domain, no internet), ESV, NLT and NIV come from their online services with the church's keys;
  - each look is a ProPresenter message whose name starts with "Scripture" ("Scripture - Full screen",
    "Scripture - Lower third"), styled once in ProPresenter, with text tokens called Reference and Text;
  - Add puts a "Scripture: John 3:16 (NIV)" line in the chosen ProPresenter playlist, so it's in the order;
  - Show puts one slide on screen through that message. Nothing goes on screen by itself.
"""

import gzip
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_DAYS = 14
MAX_VERSES = 60

# (code, name, other names). Codes are the ones API.Bible and the files in bibles\ use.
BOOKS = [
    ("GEN", "Genesis", "gen ge gn"), ("EXO", "Exodus", "exod ex exo"), ("LEV", "Leviticus", "lev le lv"), ("NUM", "Numbers", "num nu nm nb"),
    ("DEU", "Deuteronomy", "deut deu dt"), ("JOS", "Joshua", "josh jos jsh"), ("JDG", "Judges", "judg jdg jg jdgs"), ("RUT", "Ruth", "rth ru"),
    ("1SA", "1 Samuel", "1sam 1sa 1sm 1s 1samuel isamuel"), ("2SA", "2 Samuel", "2sam 2sa 2sm 2s 2samuel iisamuel"),
    ("1KI", "1 Kings", "1kgs 1ki 1kin 1k 1kings ikings"), ("2KI", "2 Kings", "2kgs 2ki 2kin 2k 2kings iikings"),
    ("1CH", "1 Chronicles", "1chr 1ch 1chron 1chronicles"), ("2CH", "2 Chronicles", "2chr 2ch 2chron 2chronicles"),
    ("EZR", "Ezra", "ezr ezra"), ("NEH", "Nehemiah", "neh ne"), ("EST", "Esther", "esth est es"), ("JOB", "Job", "jb job"),
    ("PSA", "Psalms", "ps psa psalm psalms pss psm pslm"), ("PRO", "Proverbs", "prov pro prv pr"), ("ECC", "Ecclesiastes", "eccl ecc eccles qoh"),
    ("SNG", "Song of Songs", "song sos songofsolomon songofsongs canticles sng"), ("ISA", "Isaiah", "isa is"), ("JER", "Jeremiah", "jer je jr"),
    ("LAM", "Lamentations", "lam la"), ("EZK", "Ezekiel", "ezek eze ezk"), ("DAN", "Daniel", "dan da dn"), ("HOS", "Hosea", "hos ho"),
    ("JOL", "Joel", "joel jl"), ("AMO", "Amos", "amos am"), ("OBA", "Obadiah", "obad ob oba"), ("JON", "Jonah", "jonah jnh jon"),
    ("MIC", "Micah", "mic mc"), ("NAM", "Nahum", "nah na nam"), ("HAB", "Habakkuk", "hab hb"), ("ZEP", "Zephaniah", "zeph zep zp"),
    ("HAG", "Haggai", "hag hg"), ("ZEC", "Zechariah", "zech zec zc"), ("MAL", "Malachi", "mal ml"),
    ("MAT", "Matthew", "matt mat mt"), ("MRK", "Mark", "mrk mar mk mr"), ("LUK", "Luke", "luk lk"), ("JHN", "John", "jn jhn joh"),
    ("ACT", "Acts", "act ac"), ("ROM", "Romans", "rom ro rm"), ("1CO", "1 Corinthians", "1cor 1co 1corinthians icorinthians"),
    ("2CO", "2 Corinthians", "2cor 2co 2corinthians iicorinthians"), ("GAL", "Galatians", "gal ga"), ("EPH", "Ephesians", "eph ephes"),
    ("PHP", "Philippians", "phil php pp philippians"), ("COL", "Colossians", "col co"), ("1TH", "1 Thessalonians", "1thess 1th 1thes 1thessalonians"),
    ("2TH", "2 Thessalonians", "2thess 2th 2thes 2thessalonians"), ("1TI", "1 Timothy", "1tim 1ti 1timothy"), ("2TI", "2 Timothy", "2tim 2ti 2timothy"),
    ("TIT", "Titus", "tit ti"), ("PHM", "Philemon", "phlm phm philem"), ("HEB", "Hebrews", "heb"), ("JAS", "James", "jas jm jam"),
    ("1PE", "1 Peter", "1pet 1pe 1pt 1peter"), ("2PE", "2 Peter", "2pet 2pe 2pt 2peter"), ("1JN", "1 John", "1jn 1jo 1john 1jhn"),
    ("2JN", "2 John", "2jn 2jo 2john 2jhn"), ("3JN", "3 John", "3jn 3jo 3john 3jhn"), ("JUD", "Jude", "jude jud"), ("REV", "Revelation", "rev re rv revelations"),
]
NAME = {c: n for c, n, _ in BOOKS}
ESV_NAME = {"SNG": "Song of Solomon"}

VERSIONS = {
    "NIV": {"name": "New International Version", "how": "apibible",
            "copyright": "Scripture quotations taken from The Holy Bible, New International Version® NIV®. Copyright © 1973, 1978, 1984, 2011 by Biblica, Inc.™ Used by permission. All rights reserved worldwide."},
    "ESV": {"name": "English Standard Version", "how": "esv",
            "copyright": "Scripture quotations are from the ESV® Bible (The Holy Bible, English Standard Version®), © 2001 by Crossway. Used by permission. All rights reserved."},
    "NLT": {"name": "New Living Translation", "how": "nlt",
            "copyright": "Scripture quotations are taken from the Holy Bible, New Living Translation, copyright © 1996, 2004, 2015 by Tyndale House Foundation. Used by permission. All rights reserved."},
    "KJV": {"name": "King James Version", "how": "file", "copyright": "King James Version (public domain)."},
    "WEBBE": {"name": "World English Bible (British Edition)", "how": "file", "copyright": "World English Bible British Edition (public domain)."},
}
ORDER = ["NIV", "ESV", "NLT", "KJV", "WEBBE"]
SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


class NotFound(Exception):
    pass


def _key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("first ", "1").replace("second ", "2").replace("third ", "3"))


ALIASES = {}
for _c, _n, _o in BOOKS:
    for _a in [_n] + _o.split():
        ALIASES[_key(_a)] = _c


def find_book(text):
    k = _key(text)
    if not k:
        return None
    if k in ALIASES:
        return ALIASES[k]
    hits = {c for a, c in ALIASES.items() if a.startswith(k)} if len(k) >= 2 else set()
    return hits.pop() if len(hits) == 1 else None


def parse(ref):
    """'john3:16', 'John 3:16-18', '1 Cor 13:4-7', 'Ps 23', 'Rom 8:38-9:1' -> (book, c1, v1, c2, v2). v None = whole chapter."""
    t = re.sub(r"(?<=[A-Za-z])\.", " ", str(ref or ""))  # "1 Cor. 13": a full stop after a name
    t = re.sub(r"\s+", " ", t.replace("–", "-").replace("—", "-").replace(".", ":").strip())
    m = re.match(r"^([1-3]?\s?[A-Za-z][A-Za-z ]*?)\s?(\d+)(?:\s?:\s?(\d+))?(?:\s?-\s?(\d+)(?:\s?:\s?(\d+))?)?$", t)
    if not m:
        raise NotFound("Type a book, chapter and verse, e.g. John 3:16 or Psalm 23.")
    book = find_book(m.group(1))
    if not book:
        raise NotFound("Which book is “%s”? Try the full name, e.g. Philippians." % m.group(1).strip())
    c1 = int(m.group(2))
    v1 = int(m.group(3)) if m.group(3) else None
    if m.group(4) and m.group(5):  # 8:38-9:1
        c2, v2 = int(m.group(4)), int(m.group(5))
    elif m.group(4):
        c2, v2 = (c1, int(m.group(4))) if v1 is not None else (int(m.group(4)), None)  # 3:16-18 or Psalm 23-24
    else:
        c2, v2 = c1, v1
    if c2 < c1 or (c2 == c1 and v1 is not None and v2 is not None and v2 < v1):
        raise NotFound("That passage runs backwards.")
    return book, c1, v1, c2, v2


def label(book, verses):
    """'John 3:16', 'John 3:16–18', 'Romans 8:38–9:1', 'Psalm 23'."""
    name = "Psalm" if book == "PSA" and len({c for c, _, _ in verses}) == 1 else NAME[book]
    (ca, va, _), (cb, vb, _) = verses[0], verses[-1]
    if ca != cb:
        return "%s %d:%d–%d:%d" % (name, ca, va, cb, vb)
    return "%s %d:%d" % (name, ca, va) if va == vb else "%s %d:%d–%d" % (name, ca, va, vb)


def split_slides(book, verses, version, chars):
    """Verses -> slides of at most `chars` letters. Long verses are split at a full stop, semicolon or comma."""
    numbers = len(verses) > 1
    pieces = []
    for c, v, text in verses:
        parts, rest = [], text
        while len(rest) > chars:
            cut = max((rest.rfind(p, 0, chars) for p in (". ", "; ", ": ", "? ", "! ", ", ")), default=-1)
            cut = cut + 1 if cut > chars // 3 else rest.rfind(" ", 0, chars)
            if cut <= 0:
                cut = chars
            parts.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        parts.append(rest)
        for i, p in enumerate(parts):
            pieces.append((c, v, (str(v).translate(SUP) + " " if numbers and i == 0 else "") + p))
    slides, cur = [], []
    for piece in pieces:
        if cur and len(" ".join(x[2] for x in cur)) + 1 + len(piece[2]) > chars:
            slides.append(cur)
            cur = []
        cur.append(piece)
    if cur:
        slides.append(cur)
    out = []
    for s in slides:
        vs = [(c, v, "") for c, v, _ in s]
        vs = [x for i, x in enumerate(vs) if i == 0 or x[:2] != vs[i - 1][:2]]
        out.append({"ref": label(book, vs) + " (" + version + ")", "text": " ".join(x[2] for x in s)})
    return out


def numbered(text):
    """'[16] For God ... [17] For God did not...' -> [(c or None, v, text)]. Also '[4:1]' at a new chapter."""
    out = []
    for m in re.finditer(r"\[(?:(\d+):)?(\d+)\]\s*(.*?)(?=\[(?:\d+:)?\d+\]|$)", text, re.S):
        t = re.sub(r"\s+", " ", m.group(3)).strip()
        if t:
            out.append((int(m.group(1)) if m.group(1) else None, int(m.group(2)), t))
    return out


class Scripture:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.files = {}
        self.cache = {}
        self.file = os.path.join(data_dir, "scripture.json")
        self.saved = self._load()
        self.showing = None  # {"id", "slide"}

    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        self.saved["passages"] = [p for p in self.saved.get("passages", []) if p.get("added", 0) > cutoff][-50:]
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.saved, f, indent=1)
        os.replace(tmp, self.file)

    # ---- versions ----
    def bible_dir(self):
        return self.cfg.get("bibles_dir") or os.path.join(HERE, "bibles")

    def ready(self, code):
        """(True, '') or (False, why)."""
        v = VERSIONS.get(code)
        if not v:
            return False, "Not a version C3 Life knows."
        if v["how"] == "file":
            return (True, "") if os.path.exists(os.path.join(self.bible_dir(), code + ".txt.gz")) else (False, "Its file is missing from the C3 Life server.")
        if v["how"] == "esv":
            return (True, "") if self.cfg.get("esv_key") else (False, "Needs an ESV API key (api.esv.org).")
        if v["how"] == "nlt" and self.cfg.get("nlt_key"):
            return True, ""
        if self.cfg.get("apibible_key") and (self.cfg.get("apibible_ids") or {}).get(code):
            return True, ""
        return False, ("Needs an NLT API key (api.nlt.to) or API.Bible." if code == "NLT" else "Needs permission for the NIV and an API.Bible key with it.")

    def versions(self):
        out = []
        for code in self.cfg.get("scripture_versions") or ORDER:
            if code in VERSIONS:
                ok, why = self.ready(code)
                out.append({"code": code, "name": VERSIONS[code]["name"], "ok": ok, "why": why})
        return out

    def default_version(self):
        want = self.cfg.get("scripture_default") or "NIV"
        vs = self.versions()
        if any(v["code"] == want and v["ok"] for v in vs):
            return want
        return next((v["code"] for v in vs if v["ok"]), want)

    # ---- getting the text ----
    def chapter_file(self, code):
        if code not in self.files:
            book = {}
            with gzip.open(os.path.join(self.bible_dir(), code + ".txt.gz"), "rt", encoding="utf-8") as f:
                for line in f:
                    ref, _, text = line.rstrip("\n").partition("\t")
                    b, cv = ref.split(" ")
                    c, v = cv.split(":")
                    book.setdefault((b, int(c)), {})[int(v)] = text
            self.files[code] = book
        return self.files[code]

    def from_file(self, code, book, c1, v1, c2, v2):
        data, out = self.chapter_file(code), []
        for c in range(c1, c2 + 1):
            ch = data.get((book, c))
            if not ch:
                if c == c1:
                    raise NotFound("%s doesn’t have a chapter %d." % (NAME[book], c))
                break
            lo = v1 if (c == c1 and v1) else 1
            hi = v2 if (c == c2 and v2) else max(ch)
            out += [(c, v, ch[v]) for v in range(lo, hi + 1) if v in ch]
        return out

    def http(self, url, headers=None):
        req = urllib.request.Request(url, headers=dict(headers or {}, Accept="application/json"))
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8")

    def spread(self, items, c1):
        """Give verses without a chapter the chapter they're in (online answers only number the verses)."""
        out, c, last = [], c1, 0
        for ch, v, t in items:
            if ch:
                c = ch
            elif v < last:
                c += 1
            out.append((c, v, t))
            last = v
        return out

    def ref_text(self, book, c1, v1, c2, v2, name=None):
        n = name or NAME[book]
        a = "%d:%d" % (c1, v1) if v1 else str(c1)
        b = ("%d:%d" % (c2, v2) if v2 else str(c2)) if (c2, v2) != (c1, v1) else ""
        return n + " " + a + ("-" + b if b else "")

    def from_esv(self, book, c1, v1, c2, v2):
        q = self.ref_text(book, c1, v1, c2, v2, ESV_NAME.get(book))
        url = (self.cfg.get("esv_base") or "https://api.esv.org") + "/v3/passage/text/?" + urllib.parse.urlencode({
            "q": q, "include-passage-references": "false", "include-verse-numbers": "true", "include-first-verse-numbers": "true",
            "include-footnotes": "false", "include-footnote-body": "false", "include-headings": "false", "include-short-copyright": "false",
            "include-selahs": "true", "indent-paragraphs": "0", "indent-poetry": "false", "indent-declares": "0", "indent-psalm-doxology": "0"})
        j = json.loads(self.http(url, {"Authorization": "Token " + self.cfg["esv_key"]}))
        return self.spread(numbered(" ".join(j.get("passages") or [])), c1)

    def from_apibible(self, code, book, c1, v1, c2, v2):
        bid = self.cfg["apibible_ids"][code]
        if v1:
            pid = "%s.%d.%d" % (book, c1, v1) + ("-%s.%d.%d" % (book, c2, v2) if (c2, v2) != (c1, v1) else "")
        else:
            pid = "%s.%d-%s.%d" % (book, c1, book, c2) if c2 != c1 else "%s.%d" % (book, c1)
        url = (self.cfg.get("apibible_base") or "https://api.scripture.api.bible") + "/v1/bibles/%s/passages/%s?" % (urllib.parse.quote(bid), pid) + urllib.parse.urlencode({
            "content-type": "text", "include-notes": "false", "include-titles": "false", "include-chapter-numbers": "false",
            "include-verse-numbers": "true", "include-verse-spans": "false"})
        j = json.loads(self.http(url, {"api-key": self.cfg["apibible_key"]}))
        d = j.get("data") or {}
        return self.spread(numbered(d.get("content", "")), c1), d.get("copyright", "")

    def from_nlt(self, book, c1, v1, c2, v2):
        n = NAME[book].replace(" ", "")
        ref = "%s.%d" % (n, c1) + (".%d" % v1 if v1 else "") + ("-" + ("%d.%d" % (c2, v2) if c2 != c1 else str(v2)) if (c2, v2) != (c1, v1) and v2 else ("-%d" % c2 if c2 != c1 else ""))
        url = (self.cfg.get("nlt_base") or "https://api.nlt.to") + "/api/passages?" + urllib.parse.urlencode({"ref": ref, "version": "NLT", "key": self.cfg["nlt_key"]})
        page = self.http(url)
        out = []
        for m in re.finditer(r'<verse_export[^>]*\bch="(\d+)"[^>]*\bvn="(\d+)"[^>]*>(.*?)</verse_export>', page, re.S):
            body = re.sub(r'<span class="vn">\d+</span>', "", m.group(3))
            body = re.sub(r"<h\d[^>]*>.*?</h\d>|<span class=\"tn\">.*?</span>", "", body, flags=re.S)  # headings and translators' notes
            t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
            if t:
                out.append((int(m.group(1)), int(m.group(2)), t))
        return out

    def passage(self, ref, code):
        code = code or self.default_version()
        book, c1, v1, c2, v2 = parse(ref)
        ok, why = self.ready(code)
        if not ok:
            raise NotFound("%s isn’t set up: %s" % (code, why))
        key = (code, book, c1, v1, c2, v2)
        if key in self.cache:
            return self.cache[key]
        how, extra = VERSIONS[code]["how"], ""
        try:
            if how == "file":
                verses = self.from_file(code, book, c1, v1, c2, v2)
            elif how == "esv":
                verses = self.from_esv(book, c1, v1, c2, v2)
            elif how == "nlt" and self.cfg.get("nlt_key"):
                verses = self.from_nlt(book, c1, v1, c2, v2)
            else:
                verses, extra = self.from_apibible(code, book, c1, v1, c2, v2)
        except (OSError, ValueError, KeyError) as e:
            raise NotFound("Couldn’t get %s from the internet (%s). KJV and WEBBE work with no internet." % (code, e.__class__.__name__))
        if not verses:
            raise NotFound("%s has no %s." % (code, self.ref_text(book, c1, v1, c2, v2)))
        if len(verses) > MAX_VERSES:
            raise NotFound("That’s %d verses. Choose %d or fewer." % (len(verses), MAX_VERSES))
        chars = int(self.cfg.get("scripture_slide_chars") or 220)
        p = {"reference": label(book, verses), "version": code, "version_name": VERSIONS[code]["name"],
             "verses": [{"c": c, "v": v, "text": t} for c, v, t in verses],
             "slides": split_slides(book, verses, code, chars),
             "copyright": extra or VERSIONS[code]["copyright"]}
        with self.lock:
            self.cache[key] = p
            if len(self.cache) > 300:
                self.cache.pop(next(iter(self.cache)))
        return p

    # ---- ProPresenter ----
    def pp(self, method, path, body=None):
        url = (self.cfg.get("propresenter") or "").rstrip("/") + path
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"} if body is not None else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def looks(self):
        prefix = (self.cfg.get("scripture_message_prefix") or "Scripture").lower()
        return [{"uuid": m["id"]["uuid"], "name": m["id"]["name"]} for m in (self.pp("GET", "/v1/messages") or [])
                if (m.get("id") or {}).get("name", "").lower().startswith(prefix)]

    def playlists(self):
        out = []

        def walk(nodes, path):
            for n in nodes or []:
                i = n.get("id") or {}
                if n.get("field_type") == "group":
                    walk(n.get("children"), path + [i.get("name", "")])
                elif i.get("uuid"):
                    out.append({"uuid": i["uuid"], "name": " › ".join(path + [i.get("name", "")])})
        walk(self.pp("GET", "/v1/playlists"), [])
        active = ""
        try:
            a = self.pp("GET", "/v1/playlist/active") or {}
            active = (((a.get("presentation") or {}).get("playlist") or {}).get("uuid")) or ""
        except (OSError, ValueError):
            pass
        return out, active

    def playlist_items(self, pid):
        j = self.pp("GET", "/v1/playlist/" + urllib.parse.quote(pid)) or {}
        return j.get("items") or []

    def pp_state(self):
        """Looks and playlists for the screen; never raises."""
        try:
            looks = self.looks()
            lists, active = self.playlists()
            return {"pp": "ok", "pp_reason": "" if looks else "No message in ProPresenter starts with “%s”." % (self.cfg.get("scripture_message_prefix") or "Scripture"),
                    "looks": looks, "playlists": lists, "active_playlist": active}
        except (OSError, ValueError) as e:
            return {"pp": "unavailable", "pp_reason": "ProPresenter didn’t answer (%s)." % e.__class__.__name__, "looks": [], "playlists": [], "active_playlist": ""}

    def add(self, ref, code, look_uuid, pid, after, who):
        p = self.passage(ref, code)
        look = next((x for x in self.looks() if x["uuid"] == look_uuid), None)
        if not look:
            raise NotFound("That look isn’t in ProPresenter any more.")
        items = self.playlist_items(pid)
        name = "Scripture: %s (%s)" % (p["reference"], p["version"])
        at = len(items)
        if isinstance(after, int) and -1 <= after < len(items):
            at = after + 1
        line = {"id": {"uuid": str(uuid.uuid4()).upper(), "name": name, "index": at}, "type": "header", "is_hidden": False, "is_pco": False,
                "header_color": {"red": 0.71, "green": 0.55, "blue": 0.95, "alpha": 1}}
        new = items[:at] + [line] + items[at:]
        for i, it in enumerate(new):
            (it.setdefault("id", {}))["index"] = i
        self.pp("PUT", "/v1/playlist/" + urllib.parse.quote(pid), new)
        check = [x for x in self.playlist_items(pid) if (x.get("id") or {}).get("name") == name]
        if not check:
            raise NotFound("ProPresenter didn’t keep the new line. Check the playlist isn’t locked.")
        rec = {"id": uuid.uuid4().hex[:12], "reference": p["reference"], "version": p["version"], "slides": p["slides"], "copyright": p["copyright"],
               "look": look, "playlist": pid, "line": name, "added": time.time(), "by": who}
        with self.lock:
            self.saved.setdefault("passages", []).append(rec)
            self._save()
        return rec

    def list(self):
        with self.lock:
            ps = list(self.saved.get("passages", []))
        return [dict(p, slide_count=len(p["slides"])) for p in reversed(ps)]

    def remove(self, pid):
        with self.lock:
            before = len(self.saved.get("passages", []))
            self.saved["passages"] = [p for p in self.saved.get("passages", []) if p["id"] != pid]
            self._save()
            if self.showing and self.showing["id"] == pid:
                self.showing = None
            return len(self.saved["passages"]) < before

    def show(self, pid, slide):
        p = next((x for x in self.saved.get("passages", []) if x["id"] == pid), None)
        if not p:
            return False, "That passage isn’t in the list."
        if not isinstance(slide, int) or not 0 <= slide < len(p["slides"]):
            return False, "No such slide."
        s = p["slides"][slide]
        tok = self.cfg.get("scripture_tokens") or {}
        body = [{"name": tok.get("reference", "Reference"), "text": {"text": s["ref"]}}, {"name": tok.get("text", "Text"), "text": {"text": s["text"]}}]
        try:
            if self.showing and self.showing["look"] != p["look"]["uuid"]:
                self.hide()
            self.pp("POST", "/v1/message/%s/trigger" % p["look"]["uuid"], body)
        except (OSError, ValueError) as e:
            return False, "ProPresenter didn’t answer (%s)." % e.__class__.__name__
        self.showing = {"id": pid, "slide": slide, "look": p["look"]["uuid"]}
        return True, s["ref"]

    def hide(self):
        s = self.showing
        if not s:
            return True, ""
        try:
            self.pp("GET", "/v1/message/%s/clear" % s["look"])
        except (OSError, ValueError) as e:
            return False, "ProPresenter didn’t answer (%s)." % e.__class__.__name__
        self.showing = None
        return True, ""
