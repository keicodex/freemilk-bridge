#!/usr/bin/env python3
"""Self-test for `login_fetch.py sync-9router` against a mock 9Router
panel that mirrors the real console API contract:

  POST /api/auth/login            {"password"} -> session cookie
  GET/POST /api/provider-nodes    envelope {nodes}; POST -> {node}
  PUT  /api/provider-nodes/<id>   partial node update
  GET/POST /api/providers         envelope {connections}; the list
                                  STRIPS apiKey exactly like the real
                                  panel, so sync must match by name
  PUT  /api/providers/<id>        partial update (isActive)
  GET/POST /api/models/custom     envelope {models}

Asserts: first sync creates the freemilk node + one connection per
pool account (apiKey = the bridgeKey persisted in credentials.json) +
the opus-5.5 custom model; a second sync changes nothing; dead or
removed accounts get their connections disabled; pre-existing foreign
panel state is never touched; the console password never appears in
any output.

Run:  python3 tests/sync_selftest.py   (exit code 0 = all passed)
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 18714
BASE = "http://127.0.0.1:%d" % PORT
PASSWORD = "panel-pw"

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition)))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))


STATE = {
    "nodes": [{
        "id": "openai-compatible-chat-other", "name": "Other",
        "prefix": "other", "type": "openai-compatible",
        "apiType": "chat", "baseUrl": "http://127.0.0.1:9999/v1",
    }],
    "connections": [{
        "id": "conn-other", "provider": "openai-compatible-chat-other",
        "name": "other-conn", "apiKey": "other-secret", "isActive": True,
    }],
    "customs": [{"providerAlias": "other", "id": "other-model"}],
    "seq": 0,
}


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _authed(self):
        return "auth-token=fake-session" in \
            (self.headers.get("Cookie") or "")

    def do_POST(self):
        body = self._body()
        if self.path == "/api/auth/login":
            if body.get("password") == PASSWORD:
                self._json(200, {"success": True},
                           cookie="auth-token=fake-session; Path=/; "
                                  "HttpOnly")
            else:
                self._json(401, {"error": "Invalid password"})
            return
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/api/provider-nodes":
            STATE["seq"] += 1
            node = dict(body)
            node["id"] = "openai-compatible-chat-fm%d" % STATE["seq"]
            STATE["nodes"].append(node)
            self._json(200, {"success": True, "node": node})
            return
        if self.path == "/api/providers":
            STATE["seq"] += 1
            conn = {"id": "conn-fm%d" % STATE["seq"],
                    "provider": body.get("provider"),
                    "name": body.get("name"),
                    "apiKey": body.get("apiKey"), "isActive": True}
            STATE["connections"].append(conn)
            self._json(200, {"connection": conn})
            return
        if self.path == "/api/models/custom":
            STATE["customs"].append(body)
            self._json(200, {"success": True, "added": body})
            return
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/api/provider-nodes":
            self._json(200, {"nodes": STATE["nodes"]})
            return
        if self.path == "/api/providers":
            safe = [{k: v for k, v in c.items() if k != "apiKey"}
                    for c in STATE["connections"]]
            self._json(200, {"connections": safe})
            return
        if self.path == "/api/models/custom":
            self._json(200, {"models": STATE["customs"]})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        body = self._body()
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/api/provider-nodes/"):
            node_id = self.path.rsplit("/", 1)[1]
            for node in STATE["nodes"]:
                if node["id"] == node_id:
                    node.update(body)
                    self._json(200, {"node": node})
                    return
            self._json(404, {"error": "not found"})
            return
        if self.path.startswith("/api/providers/"):
            conn_id = self.path.rsplit("/", 1)[1]
            for conn in STATE["connections"]:
                if conn["id"] == conn_id:
                    conn.update(body)
                    self._json(200, {"connection": conn})
                    return
            self._json(404, {"error": "not found"})
            return
        self._json(404, {"error": "not found"})


def run_sync(creds, password=PASSWORD):
    env = dict(os.environ)
    env.update({
        "FREEMILK_BRIDGE_CREDENTIALS": creds,
        "PANEL_BASE_URL": BASE,
        "PANEL_PASSWORD": password,
        "FREEMILK_BRIDGE_BASE_URL": "http://127.0.0.1:18611/v1",
    })
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "login_fetch.py"),
         "sync-9router"],
        capture_output=True, text=True, env=env, timeout=60)


def write_creds(path, accounts):
    with open(path, "w") as fh:
        json.dump({"accounts": accounts}, fh)


def profiled(account_id, **extra):
    entry = {"id": account_id, "baseURL": "http://127.0.0.1:18711",
             "apiKey": "profile-key-" + account_id, "protocol": "openai"}
    entry.update(extra)
    return entry


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), PanelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="freemilk-sync-test-")
    creds = os.path.join(tmp, "credentials.json")
    try:
        write_creds(creds, [profiled("acct-aaaa"), profiled("acct-bbbb")])

        proc = run_sync(creds)
        check("sync exits 0", proc.returncode == 0,
              proc.stdout + proc.stderr)
        check("sync output never contains the panel password",
              PASSWORD not in proc.stdout + proc.stderr)
        nodes = [n for n in STATE["nodes"] if n["prefix"] == "freemilk"]
        check("sync created exactly one freemilk node",
              len(nodes) == 1
              and nodes[0]["baseUrl"] == "http://127.0.0.1:18611/v1"
              and nodes[0]["apiType"] == "chat")
        with open(creds) as fh:
            data = json.load(fh)
        keys = {a["id"]: a.get("bridgeKey") for a in data["accounts"]}
        check("sync issued + persisted bridgeKeys",
              all(k and k.startswith("fmb_acct-") for k in keys.values()))
        conns = {c["name"]: c for c in STATE["connections"]
                 if c["provider"] == nodes[0]["id"]}
        check("one connection per account, keyed by bridgeKey",
              set(conns) == {"freemilk-acct-aaaa", "freemilk-acct-bbbb"}
              and conns["freemilk-acct-aaaa"]["apiKey"] == keys["acct-aaaa"]
              and conns["freemilk-acct-bbbb"]["apiKey"] == keys["acct-bbbb"])
        check("custom model registered",
              any(c.get("providerAlias") == "freemilk"
                  and c.get("id") == "opus-5.5" for c in STATE["customs"]))
        check("foreign panel state untouched",
              STATE["nodes"][0]["prefix"] == "other"
              and STATE["connections"][0]["isActive"] is True
              and len(STATE["customs"]) == 2)

        counts = (len(STATE["nodes"]), len(STATE["connections"]),
                  len(STATE["customs"]))
        proc = run_sync(creds)
        check("second sync is a no-op (idempotent)",
              proc.returncode == 0 and "already up to date" in proc.stdout
              and (len(STATE["nodes"]), len(STATE["connections"]),
                   len(STATE["customs"])) == counts,
              proc.stdout + proc.stderr)

        proc = run_sync(creds, password="wrong-pw")
        check("wrong password -> exit 2, nothing changes",
              proc.returncode == 2
              and (len(STATE["nodes"]), len(STATE["connections"]),
                   len(STATE["customs"])) == counts)

        # acct-bbbb dies, acct-cccc joins
        write_creds(creds, [
            dict(profiled("acct-aaaa"), bridgeKey=keys["acct-aaaa"]),
            dict(profiled("acct-bbbb"), bridgeKey=keys["acct-bbbb"],
                 dead=True),
            profiled("acct-cccc"),
        ])
        proc = run_sync(creds)
        conns = {c["name"]: c for c in STATE["connections"]
                 if c["provider"] == nodes[0]["id"]}
        check("dead account's connection disabled, new account added",
              proc.returncode == 0
              and conns["freemilk-acct-bbbb"]["isActive"] is False
              and conns["freemilk-acct-cccc"]["isActive"] is True
              and conns["freemilk-acct-aaaa"]["isActive"] is True,
              proc.stdout + proc.stderr)

        # acct-aaaa removed from the file entirely
        with open(creds) as fh:
            data = json.load(fh)
        data["accounts"] = [a for a in data["accounts"]
                            if a["id"] != "acct-aaaa"]
        write_creds(creds, data["accounts"])
        proc = run_sync(creds)
        conns = {c["name"]: c for c in STATE["connections"]
                 if c["provider"] == nodes[0]["id"]}
        check("removed account's connection disabled",
              proc.returncode == 0
              and conns["freemilk-acct-aaaa"]["isActive"] is False)
    finally:
        server.shutdown()

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed),
                                     len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
