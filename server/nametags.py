"""Name tags: who leads each item, from Planning Center, matched to ProPresenter props.

Each week the plan's 9 - Ministry team says who does what ("Welcome", "Pastoral", "Preach", "Close"...).
Each position is linked to an item in the plan by its title, and each person to a ProPresenter prop with
the same name ("Chris Denham"). Anyone without a prop - a guest speaker, or a couple - goes into one
ProPresenter message (default "Name tag") whose Name token is filled in.

Nothing here puts anything on screen by itself: a name tag only shows when someone taps Show.
Reads Planning Center with a Personal Access Token (api.planningcenteronline.com > Personal Access Tokens)
and ProPresenter 7.9+ with its network API (ProPresenter > Settings > Network > Enable Network).
"""

import base64
import itertools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REFRESH_S = 15 * 60
PCO = "https://api.planningcenteronline.com"

# Which 9 - Ministry position goes with which item, by item title (not case sensitive). Headers are skipped,
# and each position takes the first item that matches. An Admin can change these on Running order.
DEFAULT_RULES = [
    {"position": "Welcome", "item": r"^welcome$"},
    {"position": "Prayer (post 2nd song)", "item": r"^(?!.*salvation).*prayer"},
    {"position": "Communion (post 2nd song)", "item": r"communion"},
    {"position": "Pastoral Highlight", "item": r"highlight"},
    {"position": "Pastoral", "item": r"^pastoral(?!.*highlight)"},
    {"position": "Offering (3 min Encouragement)", "item": r"^offering"},
    {"position": "Preach", "item": r"^(message|preach|sermon)$"},
    {"position": "Close", "item": r"^close$"},
]
LEADER = "Meeting Leader"  # not tied to an item: its own button
PLACEHOLDER = re.compile(r"\b(guest|tbc|tba|tbd|placeholder|someone)\b", re.I)
TITLES = re.compile(r"^(ps|pastor|rev|revd|reverend|dr|mr|mrs|ms|miss)\.?\s+", re.I)


def norm(name):
    """For matching names to props: case, spaces, 'and' and titles like 'Ps' don't matter."""
    n = re.sub(r"\s+", " ", str(name or "").replace(" ", " ")).strip().lower()
    n = re.sub(r"\s+and\s+", " & ", n)
    return " & ".join(TITLES.sub("", p.strip()) for p in n.split("&"))


def join_names(names):
    """['Billy Tagg', 'Sophie Tagg'] -> 'Billy & Sophie Tagg'; different surnames stay whole."""
    names = [n for n in names if n]
    if len(names) < 2:
        return names[0] if names else ""
    parts = [n.rsplit(" ", 1) for n in names]
    if all(len(p) == 2 for p in parts) and len({p[1].lower() for p in parts}) == 1:
        return " & ".join(p[0] for p in parts) + " " + parts[0][1]
    return " & ".join(names)


def valid_rules(rules):
    if not isinstance(rules, list) or len(rules) > 40:
        return False
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("position"), str) or not isinstance(r.get("item"), str):
            return False
        if not r["position"].strip() or len(r["position"]) > 80 or len(r["item"]) > 200:
            return False
        try:
            re.compile(r["item"], re.I)
        except re.error:
            return False
    return True


class NameTags:
    def __init__(self, data_dir, cfg, log):
        self.cfg, self.log = cfg, log
        self.lock = threading.Lock()
        self.file = os.path.join(data_dir, "nametags.json")  # typed names and the Admin's position links
        self.saved = self._load()
        self.data = {"status": "starting", "reason": "", "at": "", "plan": None, "tags": [], "leader": None, "props": 0}
        self.showing = None  # what we put on screen, so Hide can take it off
        self.started = False

    # ---- settings ----
    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.saved, f, indent=1)
        os.replace(tmp, self.file)

    def rules(self):
        r = self.saved.get("rules") or self.cfg.get("nametag_rules")
        return r if valid_rules(r) else DEFAULT_RULES

    def set_rules(self, rules):
        with self.lock:
            if rules:
                self.saved["rules"] = rules
            else:
                self.saved.pop("rules", None)  # back to the defaults
            self._save()

    def set_name(self, plan_id, item_id, text):
        """A name typed in the app for one item this week. Empty puts the Planning Center name back."""
        with self.lock:
            typed = self.saved.setdefault("typed", {})
            if text:
                typed.setdefault(str(plan_id), {})[str(item_id)] = text
            else:
                typed.get(str(plan_id), {}).pop(str(item_id), None)
            # only keep the last few weeks
            for old in sorted(typed, key=lambda k: typed[k].get("_at", 0) if isinstance(typed[k], dict) else 0)[:-8]:
                typed.pop(old, None)
            typed.get(str(plan_id), {})["_at"] = time.time()
            self._save()
            tag = next((t for t in self.data.get("tags", []) if t["item_id"] == str(item_id)), None)
        if not text or not tag:
            self.refresh()  # put the Planning Center name back
            return
        with self.lock:
            tag.update(text=text, source="typed")
        self.match([tag])

    # ---- talking to Planning Center and ProPresenter ----
    def configured(self):
        return bool(self.cfg.get("pco_app_id") and self.cfg.get("pco_secret"))

    def pco(self, path):
        """GET every page of a Planning Center list. Returns (data, included)."""
        base = self.cfg.get("pco_base") or PCO
        auth = base64.b64encode(("%s:%s" % (self.cfg["pco_app_id"], self.cfg["pco_secret"])).encode()).decode()
        url, data, inc = base + path, [], []
        for _ in range(10):
            req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.load(r)
            d = j.get("data")
            data += d if isinstance(d, list) else [d]
            inc += j.get("included") or []
            url = ((j.get("links") or {}).get("next")) or ""
            if not url:
                break
            if base != PCO:  # tests: keep paging on the fake server
                url = base + urllib.parse.urlsplit(url).path + "?" + urllib.parse.urlsplit(url).query
        return data, inc

    def pp_url(self):
        return (self.cfg.get("propresenter") or "").rstrip("/")

    def pp(self, method, path, body=None):
        if not self.pp_url():
            raise urllib.error.URLError("ProPresenter hasn't been found on the network yet. Open it with Settings > Network turned on; the network scan finds it.")
        req = urllib.request.Request(self.pp_url() + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"} if body is not None else {})
        with urllib.request.urlopen(req, timeout=4) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def pp_list(self, what):
        """ProPresenter props or messages: {normalised name: (uuid, name)}."""
        out = {}
        for x in self.pp("GET", "/v1/" + what) or []:
            i = x.get("id") or {}
            if i.get("name") and i.get("uuid"):
                out[norm(i["name"])] = (i["uuid"], i["name"])
        return out

    # ---- building this week's name tags ----
    def pick_plan(self, st):
        today = time.strftime("%Y-%m-%d")
        past, _ = self.pco("/services/v2/service_types/%s/plans?filter=past&order=-sort_date&per_page=1" % st)
        if past and str(past[0]["attributes"].get("sort_date", ""))[:10] == today:
            return past[0]  # it's Sunday and the service has started
        fut, _ = self.pco("/services/v2/service_types/%s/plans?filter=future&order=sort_date&per_page=1" % st)
        return fut[0] if fut else (past[0] if past else None)

    def build(self):
        name = self.cfg.get("pco_service_type") or "Sunday Morning"
        types, _ = self.pco("/services/v2/service_types?per_page=100")
        st = next((t for t in types if t["attributes"].get("name", "").strip().lower() == name.strip().lower()), None)
        if not st:
            raise LookupError("No service type called %s in Planning Center." % name)
        plan = self.pick_plan(st["id"])
        if not plan:
            raise LookupError("No plans in %s." % name)
        pid = plan["id"]
        items, inc = self.pco("/services/v2/service_types/%s/plans/%s/items?include=item_notes&per_page=100" % (st["id"], pid))
        notes = {n["id"]: n["attributes"] for n in inc if n.get("type") == "ItemNote"}
        people, pinc = self.pco("/services/v2/service_types/%s/plans/%s/team_members?include=team&per_page=100" % (st["id"], pid))
        teams = {t["id"]: t["attributes"].get("name", "") for t in pinc if t.get("type") == "Team"}
        want = (self.cfg.get("nametag_team") or "9 - Ministry").strip().lower()
        roster, everyone = {}, []
        for p in people:
            a = p["attributes"]
            if str(a.get("status", "")).upper().startswith("D"):  # declined
                continue
            everyone.append(a.get("name", ""))
            tid = ((p.get("relationships") or {}).get("team") or {}).get("data") or {}
            if teams.get(tid.get("id"), "").strip().lower() == want:
                roster.setdefault(a.get("team_position_name", ""), []).append(a.get("name", ""))
        cats = [c.lower() for c in (self.cfg.get("nametag_note_categories") or ["Hosting"])]

        def full(first):
            # "Chris" in a note -> "Chris Denham" from the plan's people, when only one Chris is serving
            hits = {n for n in everyone if n.split(" ")[0].lower() == first.strip().lower()}
            return hits.pop() if len(hits) == 1 else first.strip()

        def from_note(it):
            for ref in ((it.get("relationships") or {}).get("item_notes") or {}).get("data") or []:
                n = notes.get(ref["id"]) or {}
                if str(n.get("category_name", "")).lower() in cats and str(n.get("content", "")).strip():
                    # "Joel Burden - Chris to intro": the names come before any dash, bracket or colon
                    who = re.split(r"\s[-–—]\s|[(:\n]", n["content"].strip())[0]
                    parts = re.split(r"\s*(?:&|\band\b|,|/)\s*", who.strip())
                    return [full(x) if " " not in x.strip() else x.strip() for x in parts if x.strip()]
            return []

        items = sorted((i for i in items if i["attributes"].get("item_type") != "header"), key=lambda i: i["attributes"].get("sequence") or 0)
        typed = (self.saved.get("typed") or {}).get(str(pid), {})
        tags, used = [], set()
        for rule in self.rules():
            pat = re.compile(rule["item"], re.I)
            it = next((i for i in items if i["id"] not in used and pat.search(i["attributes"].get("title", "").strip())), None)
            names = [n for n in roster.get(rule["position"], []) if not PLACEHOLDER.search(n)]
            if not it:
                if names:
                    tags.append({"id": "pos:" + rule["position"], "item": "", "item_id": "", "position": rule["position"], "people": names,
                                 "text": join_names(names), "source": "ministry", "no_item": True})
                continue
            used.add(it["id"])
            source = "ministry"
            noted = from_note(it)
            if not names:
                names, source = noted, "note"
            elif len(names) > 1 and {n.lower() for n in noted} == {n.lower() for n in names}:
                names = noted  # "Billy & Sophie" in the note: keep their order
            text = join_names(names)
            if typed.get(it["id"]):
                text, source = typed[it["id"]], "typed"
            placeholder = [n for n in roster.get(rule["position"], []) if PLACEHOLDER.search(n)]
            if not text and not placeholder and rule["position"] not in roster:
                continue  # nobody on that position this week, and nothing to say: no tag
            tags.append({"id": it["id"], "item": it["attributes"].get("title", ""), "item_id": it["id"], "position": rule["position"],
                         "people": names, "text": text, "source": source if text else "missing",
                         "placeholder": placeholder[0] if placeholder and source != "ministry" else ""})
        lead = [n for n in roster.get(LEADER, []) if not PLACEHOLDER.search(n)]
        leader = {"id": "leader", "item": "", "item_id": "", "position": LEADER, "people": lead, "text": join_names(lead), "source": "ministry"} if lead else None
        a = plan["attributes"]
        return {"plan": {"id": pid, "dates": a.get("dates", ""), "title": a.get("title") or a.get("series_title") or "", "service_type": st["attributes"].get("name", "")},
                "tags": tags, "leader": leader, "items": [{"id": i["id"], "title": i["attributes"].get("title", "")} for i in items]}

    def match(self, tags):
        """Give each tag the ProPresenter prop (or the Name tag message) that will show it."""
        if not self.pp_url():
            for t in tags:
                t["match"] = {"kind": "none", "why": "ProPresenter's address isn't set on the C3 Life server."}
            return 0, ""
        try:
            props = self.pp_list("props")
            msgs = self.pp_list("messages")
        except (OSError, ValueError) as e:
            for t in tags:
                t["match"] = {"kind": "none", "why": "ProPresenter didn't answer."}
            return 0, "ProPresenter didn't answer (%s)." % e.__class__.__name__
        msg = msgs.get(norm(self.cfg.get("nametag_message") or "Name tag"))
        for t in tags:
            if not t["text"]:
                t["match"] = {"kind": "none", "why": "Needs a name."}
            elif self.prop_for(t, props):
                uuid, pname = self.prop_for(t, props)
                t["match"] = {"kind": "prop", "uuid": uuid, "name": pname}
            elif msg:
                t["match"] = {"kind": "message", "uuid": msg[0], "name": msg[1]}
            else:
                t["match"] = {"kind": "none", "why": "No prop called %s, and no %s message in ProPresenter." % (t["text"], self.cfg.get("nametag_message") or "Name tag")}
        return len(props), ""

    @staticmethod
    def prop_for(t, props):
        """A prop with the tag's name; for a couple, either way round ("Sophie & Billy Tagg")."""
        tries = [t["text"]]
        if t.get("source") != "typed" and 1 < len(t.get("people") or []) < 4:
            tries += [join_names(list(p)) for p in itertools.permutations(t["people"])]
        return next((props[norm(x)] for x in tries if norm(x) in props), None)

    def refresh(self):
        if not self.configured():
            d = {"status": "off", "reason": "Planning Center isn't set up on the C3 Life server yet.", "plan": None, "tags": [], "leader": None, "props": 0}
        else:
            try:
                d = dict(self.build(), status="ok", reason="")
            except (OSError, ValueError, KeyError, LookupError) as e:
                why = str(e) if isinstance(e, LookupError) else ("Planning Center refused the token." if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403)
                                                               else "Couldn't reach Planning Center (%s)." % e.__class__.__name__)
                d = {"status": "unavailable", "reason": why, "plan": None, "tags": [], "leader": None, "props": 0}
        all_tags = d["tags"] + ([d["leader"]] if d.get("leader") else [])
        d["props"], d["pp_reason"] = self.match(all_tags) if all_tags else (0, "")
        d["at"] = time.strftime("%H:%M")
        with self.lock:
            if d["status"] == "unavailable" and self.data.get("tags"):
                keep = {k: self.data[k] for k in ("plan", "tags", "leader", "items") if k in self.data}
                d.update(keep, stale=True)  # keep last week's good read rather than nothing
            # a name typed while Planning Center was being read: keep it, rather than the read's older copy
            typed = (self.saved.get("typed") or {}).get(str((d.get("plan") or {}).get("id")), {})
            late = [t for t in d["tags"] if t.get("item_id") and typed.get(t["item_id"]) and t["text"] != typed[t["item_id"]]]
            for t in late:
                t.update(text=typed[t["item_id"]], source="typed")
            self.data = d
        if late:
            self.match(late)
        return self.get()

    def run(self):
        while True:
            try:
                self.refresh()
            except Exception as e:  # never let the loop die
                self.log.error("nametags: %s", e)
            time.sleep(REFRESH_S)

    def get(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.run, daemon=True).start()
            time.sleep(0.05)
        with self.lock:
            d = json.loads(json.dumps(self.data))
        d["showing"] = self.showing["id"] if self.showing else ""
        d["rules"] = self.rules()
        d["default_rules"] = self.rules() == DEFAULT_RULES
        return d

    def find(self, tag_id):
        with self.lock:
            return next((t for t in self.data.get("tags", []) + ([self.data["leader"]] if self.data.get("leader") else []) if t["id"] == tag_id), None)

    # ---- on screen, only when someone taps ----
    def show(self, tag_id):
        t = self.find(tag_id)
        if not t:
            return False, "That name tag isn't in this week's plan."
        m = t.get("match") or {}
        if m.get("kind") == "none" or not m.get("kind"):
            return False, m.get("why") or "No ProPresenter prop or message for this name."
        try:
            if self.showing:
                self.hide()
            if m["kind"] == "prop":
                self.pp("GET", "/v1/prop/%s/trigger" % m["uuid"])
            else:
                token = self.cfg.get("nametag_token") or "Name"
                self.pp("POST", "/v1/message/%s/trigger" % m["uuid"], [{"name": token, "text": {"text": t["text"]}}])
        except (OSError, ValueError) as e:
            return False, "ProPresenter didn't answer (%s)." % e.__class__.__name__
        self.showing = {"id": tag_id, "kind": m["kind"], "uuid": m["uuid"]}
        return True, t["text"]

    def hide(self):
        s = self.showing
        if not s:
            return True, ""
        try:
            self.pp("GET", "/v1/%s/%s/clear" % ("prop" if s["kind"] == "prop" else "message", s["uuid"]))
        except (OSError, ValueError) as e:
            return False, "ProPresenter didn't answer (%s)." % e.__class__.__name__
        self.showing = None
        return True, ""
