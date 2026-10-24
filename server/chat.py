"""Team chat, kept on the C3 Life server so every phone and iPad sees the same messages (Chat).

Three channels: 0 All team, 1 Band, 2 Stage display. A message for the stage display (channel 2, or "Also show on
ProPresenter stage display") is also sent to ProPresenter's stage message (PUT /v1/stage/message), so the band sees it
on the confidence screen. The last 300 messages are kept in data/chat.json, so a restart doesn't lose them.
"""

import json
import os
import threading
import time

KEEP = 300
CHANNELS = ("All team", "Band", "Stage display")


class Chat:
    def __init__(self, data_dir, log, pp=None):
        self.path = os.path.join(data_dir, "chat.json")
        self.log, self.pp = log, pp
        self.lock = threading.Lock()
        self.msgs, self.n = [], 0
        try:
            with open(self.path, encoding="utf-8") as f:
                self.msgs = [m for m in json.load(f).get("messages", []) if isinstance(m, dict)][-KEEP:]
            self.n = max([m.get("id", 0) for m in self.msgs] or [0])
        except (OSError, ValueError, AttributeError):
            pass

    def since(self, n=0):
        with self.lock:
            return [m for m in self.msgs if m["id"] > n]

    def send(self, who, role, text, ch=0, stage=False):
        """-> (message, error or None)"""
        text = (text or "").strip() if isinstance(text, str) else ""
        if not text or len(text) > 500:
            return None, "A message is 1 to 500 characters."
        if not isinstance(ch, int) or isinstance(ch, bool) or not 0 <= ch < len(CHANNELS):
            return None, "Bad request"
        stage = bool(stage) or ch == 2
        err = None
        if stage:
            err = self.to_stage(text)
        with self.lock:
            self.n += 1
            m = {"id": self.n, "at": round(time.time(), 1), "t": time.strftime("%H:%M"), "from": who, "role": role, "text": text, "ch": ch,
                 "stage": stage and not err}
            self.msgs = (self.msgs + [m])[-KEEP:]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"messages": self.msgs}, f)
            os.replace(tmp, self.path)
        self.log.info("chat: %s (%s): %s", who, CHANNELS[ch], text[:80])
        return m, ("Sent to the team, but not to the stage display: %s" % err) if err else None

    def to_stage(self, text):
        if not self.pp:
            return "ProPresenter isn't set up."
        try:
            self.pp._req("/v1/stage/message", method="PUT", body=text)
        except (OSError, ValueError) as e:
            return self.pp._why(e)
        return None
