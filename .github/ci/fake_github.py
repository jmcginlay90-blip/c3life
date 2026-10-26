"""A pretend GitHub for testing Update-C3Life.ps1 (on GitHub's Windows computer, and on Linux with PowerShell).

  python fake_github.py <port> <zip of the new version> [key]
Answers like api.github.com for one repository, owner/c3life: the latest commit on main, the comparison with the
installed one, and the download (a redirect to a short-lived link, as GitHub does). Only with "Bearer <key>"."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, ZIP = int(sys.argv[1]), sys.argv[2]
KEY = sys.argv[3] if len(sys.argv) > 3 else "test-key"
OLD, NEW = "a" * 40, "b" * 40


def commit(sha, msg, parents=1):
    return {"sha": sha, "parents": [{"sha": "p%d" % i} for i in range(parents)],
            "commit": {"message": msg, "committer": {"date": "2026-10-27T10:00:00Z"}}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/download/"):  # the short-lived link: no key needed, like codeload.github.com
            with open(ZIP, "rb") as f:
                return self.send(200, f.read(), "application/zip")
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self.send(401, {"message": "Bad credentials"})
        p = self.path
        if p == "/repos/owner/c3life":
            return self.send(200, {"full_name": "owner/c3life", "permissions": {"pull": True, "push": False, "admin": False}})
        if p == "/repos/owner/c3life/commits/main":
            return self.send(200, commit(NEW, "Merge pull request #14 from owner/simple-pages\n\nSimple pages: Foyer, Listen pages and updates", 2))
        if p == "/repos/owner/c3life/compare/%s...%s" % (OLD, NEW):
            return self.send(200, {"commits": [commit("c" * 40, "Simple pages work in progress"),
                                               commit("d" * 40, "Merge pull request #13 from owner/marker-tools\n\nMarker tools: Previous/Next, several parts", 2),
                                               commit(NEW, "Merge pull request #14 from owner/simple-pages\n\nSimple pages: Foyer, Listen pages and updates", 2)]})
        if p == "/repos/owner/c3life/zipball/" + NEW:
            return self.send(302, b"", "text/plain", {"Location": "http://127.0.0.1:%d/download/%s.zip" % (PORT, NEW)})
        return self.send(404, {"message": "Not Found"})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
