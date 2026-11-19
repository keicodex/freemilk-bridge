#!/usr/bin/env python3
"""Self-test for login_fetch.py against fake Firebase + Milq endpoints.

Fakes:
  POST /v1/token                     securetoken stand-in: rotates rt-1
                                     -> id_token "fresh-id-token",
                                     refresh_token "rt-2"
  GET  /account/api/models           always 401 (a miss candidate)
  GET  /account/api/hosted-models    401 unless Bearer fresh-id-token;
                                     then returns a catalog whose Opus
                                     entry carries a provider profile

Asserts: fetch exits 0, the profile lands in credentials.json (0600),
the Firebase refresh token is rotated, the winning endpoint is
remembered, and no token material appears in stdout/stderr or status.
Also covers the guided `login` command: one paste of a fresh token
JSON stores + fetches with a LOGIN OK verdict; a garbage paste fails
cleanly and stores nothing. And `bulk-add`: a JSON array file adds
several pool accounts (each with profile + issued bridgeKey),
re-adding dedupes by account id, and JSONL stdin tolerates junk
lines while still adding the valid accounts.

Run:  python3 tests/login_selftest.py   (exit code 0 = all passed)
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 18712
BASE = "http://127.0.0.1:%d" % PORT

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition)))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if path == "/v1/token":
            self._json(200, {"id_token": "fresh-id-token",
                             "refresh_token": "rt-2",
                             "expires_in": "3600"})
            return
        self._json(404, {})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/account/api/models":
            self._json(401, {"detail": "Missing account session"})
            return
        if path == "/account/api/hosted-models":
            if self.headers.get("Authorization") != "Bearer fresh-id-token":
                self._json(401, {"detail": "bad token"})
                return
            self._json(200, {"catalog": [
                {"name": "GLM 5.3 Flash",
                 "endpoint": {"baseURL": BASE, "apiKey": "glm-key",
                              "protocol": "openai", "model": "glm"}},
                {"name": "Opus 5.5",
                 "endpoint": {"baseURL": BASE, "apiKey": "profile-key-1",
                              "protocol": "openai",
                              "model": "opus-upstream-1"}},
            ]})
            return
        self._json(404, {})


def run_helper(args, creds, stdin_text=None, extra_env=None):
    env = dict(os.environ)
    env.update({
        "FREEMILK_BRIDGE_CREDENTIALS": creds,
        "FREEMILK_SECURETOKEN_URL": BASE + "/v1/token",
        "FREEMILK_MILQ_BASE_URL": BASE,
    })
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "login_fetch.py")] + args,
        input=stdin_text, capture_output=True, text=True, env=env,
        timeout=60)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), FakeHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="freemilk-login-test-")
    creds = os.path.join(tmp, "credentials.json")
    try:
        # set-tokens: store a stale ID token + refresh token via stdin
        proc = run_helper(["set-tokens"], creds, stdin_text=json.dumps({
            "idToken": "stale-token", "refreshToken": "rt-1"}))
        check("set-tokens exits 0", proc.returncode == 0,
              proc.stdout + proc.stderr)
        check("set-tokens prints no secrets",
              "stale-token" not in proc.stdout + proc.stderr
              and "rt-1" not in proc.stdout + proc.stderr)
        with open(creds) as fh:
            data = json.load(fh)
        check("tokens stored under milq section",
              data["milq"]["firebaseRefreshToken"] == "rt-1")

        # constrain candidates for speed/determinism
        data["milq"]["profileEndpoints"] = [
            "/account/api/models", "/account/api/hosted-models"]
        data["milq"]["firebaseIdTokenExpiry"] = 1  # force refresh
        with open(creds, "w") as fh:
            json.dump(data, fh)

        proc = run_helper(["fetch"], creds)
        check("fetch exits 0", proc.returncode == 0,
              proc.stdout + proc.stderr)
        check("fetch output/logs carry no secrets",
              all(secret not in proc.stdout + proc.stderr for secret in
                  ("fresh-id-token", "rt-2", "profile-key-1")))
        with open(creds) as fh:
            data = json.load(fh)
        check("profile landed (opus entry preferred)",
              data.get("apiKey") == "profile-key-1"
              and data.get("model") == "opus-upstream-1",
              json.dumps({k: data.get(k) for k in ("apiKey", "model")}))
        check("firebase refresh token rotated",
              data["milq"].get("firebaseRefreshToken") == "rt-2")
        check("winning endpoint remembered",
              data["milq"].get("profileEndpointUsed")
              == "/account/api/hosted-models")
        check("credentials mode 0600",
              (os.stat(creds).st_mode & 0o777) == 0o600)

        proc = run_helper(["status"], creds)
        check("status exits 0 + no secrets",
              proc.returncode == 0
              and "fresh-id-token" not in proc.stdout
              and "profile-key-1" not in proc.stdout)

        # guided login: ONE paste of a fresh token JSON (as the
        # sign-in page hands it over) -> stored + profile fetched
        creds2 = os.path.join(tmp, "credentials-login.json")
        proc = run_helper(["login"], creds2, stdin_text=json.dumps({
            "idToken": "fresh-id-token", "refreshToken": "rt-1"}))
        check("login exits 0 with LOGIN OK verdict",
              proc.returncode == 0 and "LOGIN OK" in proc.stdout,
              proc.stdout + proc.stderr)
        check("login output carries no secrets",
              all(secret not in proc.stdout + proc.stderr for secret in
                  ("fresh-id-token", "rt-1", "profile-key-1")))
        with open(creds2) as fh:
            data2 = json.load(fh)
        check("login landed the profile",
              data2.get("apiKey") == "profile-key-1"
              and data2.get("model") == "opus-upstream-1")

        # guided login: garbage paste -> clean failure, nothing stored
        creds3 = os.path.join(tmp, "credentials-login-bad.json")
        proc = run_helper(["login"], creds3, stdin_text="not json")
        check("login rejects a bad paste",
              proc.returncode == 2 and "LOGIN FAILED" in proc.stdout
              and not os.path.exists(creds3))

        # bulk-add: a JSON array file with two accounts
        creds4 = os.path.join(tmp, "credentials-bulk.json")
        batch = os.path.join(tmp, "batch.json")
        with open(batch, "w") as fh:
            json.dump([
                {"idToken": "fresh-id-token", "refreshToken": "rt-a"},
                {"idToken": "fresh-id-token", "refreshToken": "rt-b"},
            ], fh)
        proc = run_helper(["bulk-add", batch], creds4)
        check("bulk-add exits 0, 2/2 ready",
              proc.returncode == 0 and "2/2 account(s) ready"
              in proc.stdout, proc.stdout + proc.stderr)
        check("bulk-add output carries no secrets",
              all(secret not in proc.stdout + proc.stderr for secret in
                  ("fresh-id-token", "rt-a", "rt-b", "profile-key-1",
                   "fmb_acct-")))
        with open(creds4) as fh:
            data4 = json.load(fh)
        accounts = data4.get("accounts") or []
        check("bulk-add stored 2 pool accounts with profiles + keys",
              len(accounts) == 2
              and all(a.get("apiKey") == "profile-key-1" for a in accounts)
              and all((a.get("bridgeKey") or "").startswith(
                  "fmb_" + a["id"] + "_") for a in accounts)
              and accounts[0]["id"] != accounts[1]["id"])
        check("bulk credentials mode 0600",
              (os.stat(creds4).st_mode & 0o777) == 0o600)

        # bulk-add again with the same file: dedupe by account id
        proc = run_helper(["bulk-add", batch], creds4)
        with open(creds4) as fh:
            data4 = json.load(fh)
        check("bulk-add re-run dedupes (still 2 accounts)",
              proc.returncode == 0
              and len(data4.get("accounts") or []) == 2
              and "already has a profile" in proc.stdout)

        # bulk-add via stdin JSONL: junk lines tolerated
        jsonl = "\n".join([
            json.dumps({"idToken": "fresh-id-token",
                        "refreshToken": "rt-c"}),
            "this is not json",
            json.dumps({"unrelated": True}),
        ])
        proc = run_helper(["bulk-add"], creds4, stdin_text=jsonl)
        with open(creds4) as fh:
            data4 = json.load(fh)
        check("bulk-add JSONL adds the one valid account",
              proc.returncode == 0
              and len(data4.get("accounts") or []) == 3
              and "account ?: FAILED" in proc.stdout,
              proc.stdout + proc.stderr)
    finally:
        server.shutdown()

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed),
                                     len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
