#!/usr/bin/env python3
"""Self-test for capture_ingest.py with SYNTHETIC captures (every
value below is fake).

Fixtures:
  1. a full HAR session — sign-in exchange (device_check_token in
     the request), prepare #1, catalog, first provider chat,
     prepare #2 exactly 45 min later with a rotated key, and a
     session refresh — asserting the emitted credentials.json
     (bridge format, 0600), the capture_report.json checklist /
     rotation / redaction, and that no fake secret reaches stdout.
  2. a mitmproxy flow-JSON capture with only sign-in + prepare —
     asserting the flow parser, the emitted credentials, and the
     missing-items report (catalog / chat / 2nd prepare / refresh).
  3. a HAR with no prepare at all — ingest must exit 2 and write
     no credentials.
  4. replay-prepare against a local stub: (a) the stub answers a
     ConfigsResponse with a FRESH apiKey -> verdict REPLAYABLE,
     exit 0, and the stub must see the Milq access token from the
     ingested credentials (not the stale one in the capture);
     (b) the stub answers the 403 client gate -> verdict BLOCKED,
     exit 2.

Run:  python3 tests/capture_selftest.py   (exit code 0 = all passed)
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 18713
BASE = "http://127.0.0.1:%d" % PORT

RESULTS = []
SEEN = {"authorization": None}


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition)))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))


class StubHandler(BaseHTTPRequestHandler):
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

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/v9/runtime/configs":
            SEEN["authorization"] = self.headers.get("Authorization")
            self._json(200, {
                "apiKey": "fake-proxy-key-FRESH",
                "baseUrl": BASE + "/provider/v1",
                "clientIP": "127.0.0.1", "maxToolRounds": 25,
                "adsEnabled": False})
            return
        if self.path == "/v9/gated/configs":
            self._json(403, {
                "detail": "This client is not supported. "
                          "Please use the Milq iOS app."})
            return
        self._json(404, {})

    do_GET = _handle
    do_POST = _handle


def run_tool(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "capture_ingest.py")] + args,
        capture_output=True, text=True, env=env, timeout=60)


def har_entry(started, method, url, req_headers, req_body, status,
              resp_body):
    entry = {
        "startedDateTime": started,
        "request": {"method": method, "url": url,
                    "headers": [{"name": k, "value": v}
                                for k, v in req_headers]},
        "response": {"status": status,
                     "headers": [{"name": "Content-Type",
                                  "value": "application/json"}],
                     "content": {"mimeType": "application/json",
                                 "text": json.dumps(resp_body)}},
    }
    if req_body is not None:
        entry["request"]["postData"] = {
            "mimeType": "application/json",
            "text": json.dumps(req_body)}
    return entry


def signin_entry(started, access="fake-milq-access-1",
                 refresh="fake-milq-refresh-1"):
    return har_entry(
        started, "POST", "https://api.milq.ai/v9/session/begin",
        [("Content-Type", "application/json"),
         ("User-Agent", "FreeMilk/2.3.0")],
        {"idToken": "fake-id-token",
         "device_check_token": "fake-device-check-token",
         "email": "op@example.test"},
        200,
        {"status": "ok", "access_token": access,
         "refresh_token": refresh, "expires_in": 3600,
         "user": {"fullName": "Fake Op"},
         "intro_credits": {"sips": 10}})


def full_har():
    return {"log": {"version": "1.2", "entries": [
        signin_entry("2026-11-19T10:00:00+00:00"),
        har_entry(
            "2026-11-19T10:00:05+00:00", "POST",
            "https://api.milq.ai/v9/runtime/configs",
            [("Authorization", "Bearer fake-milq-access-1"),
             ("X-Custom-Probe", "probe-value")],
            {},
            200,
            {"apiKey": "fake-proxy-key-AAAA",
             "baseUrl": "https://provider.example.test/v1",
             "clientIP": "203.0.113.7", "maxToolRounds": 25,
             "adsEnabled": False, "ratingPromptEnabled": False,
             "discordInvite": "https://discord.example.test/invite"}),
        har_entry(
            "2026-11-19T10:00:08+00:00", "GET",
            "https://api.milq.ai/v9/models/hosted",
            [("Authorization", "Bearer fake-milq-access-1")],
            None,
            200,
            {"models": [
                {"tier": "free", "name": "Opus 5.5", "mode": "agent",
                 "requestFormat": "openai", "contextWindow": 200000,
                 "acceptsImages": True, "isDefault": True,
                 "limit": {}},
                {"tier": "free", "name": "GLM Fake",
                 "isDefault": False}],
             "limits": {"daily": {}}, "agents": []}),
        har_entry(
            "2026-11-19T10:00:15+00:00", "POST",
            "https://provider.example.test/v1/chat/completions",
            [("Authorization", "Bearer fake-proxy-key-AAAA"),
             ("X-Session-Affinity", "fake-session-123")],
            {"model": "opus-upstream-fake",
             "messages": [{"role": "user", "content": "hello"}],
             "stream": True},
            200,
            {"id": "chatcmpl-fake",
             "choices": [{"message": {"role": "assistant",
                                      "content": "hi"}}]}),
        har_entry(
            "2026-11-19T10:45:05+00:00", "POST",
            "https://api.milq.ai/v9/runtime/configs",
            [("Authorization", "Bearer fake-milq-access-1")],
            {},
            200,
            {"apiKey": "fake-proxy-key-BBBB",
             "baseUrl": "https://provider.example.test/v1",
             "clientIP": "203.0.113.7", "maxToolRounds": 25,
             "adsEnabled": False}),
        har_entry(
            "2026-11-19T10:50:00+00:00", "POST",
            "https://api.milq.ai/v9/session/renew",
            [("Content-Type", "application/json")],
            {"refresh_token": "fake-milq-refresh-1"},
            200,
            {"access_token": "fake-milq-access-2",
             "refresh_token": "fake-milq-refresh-2",
             "expires_in": 3600}),
    ]}}


def flow_capture():
    def b64(obj):
        return base64.b64encode(
            json.dumps(obj).encode()).decode()
    return [
        {"id": "flow-1", "type": "http",
         "request": {"method": "POST", "scheme": "https",
                     "host": "api.milq.ai", "port": 443,
                     "path": "/v9/session/begin",
                     "headers": [["Content-Type", "application/json"]],
                     "content": b64({
                         "device_check_token": "fake-flow-dct",
                         "idToken": "fake-flow-id"}),
                     "timestamp_start": 1763000000.0},
         "response": {"status_code": 200,
                      "headers": [["Content-Type", "application/json"]],
                      "content": b64({
                          "access_token": "fake-flow-access-1",
                          "refresh_token": "fake-flow-refresh-1",
                          "expires_in": 3600, "user": {}}),
                      "timestamp_start": 1763000000.5}},
        {"id": "flow-2", "type": "http",
         "request": {"method": "POST", "scheme": "https",
                     "host": "api.milq.ai", "port": 443,
                     "path": "/v9/runtime/configs",
                     "headers": [["Authorization",
                                  "Bearer fake-flow-access-1"]],
                     "content": b64({}),
                     "timestamp_start": 1763000030.0},
         "response": {"status_code": 200,
                      "headers": [["Content-Type", "application/json"]],
                      "content": b64({
                          "apiKey": "fake-flow-key-CCCC",
                          "baseUrl":
                              "https://flow-provider.example.test/v1",
                          "clientIP": "192.0.2.55",
                          "maxToolRounds": 10}),
                      "timestamp_start": 1763000030.5}},
    ]


def replay_har(prepare_path, captured_key):
    return {"log": {"version": "1.2", "entries": [
        signin_entry("2026-11-19T11:00:00+00:00"),
        har_entry(
            "2026-11-19T11:00:05+00:00", "POST", BASE + prepare_path,
            [("Authorization", "Bearer fake-milq-access-0"),
             ("Content-Type", "application/json")],
            {},
            200,
            {"apiKey": captured_key,
             "baseUrl": BASE + "/provider/v1",
             "clientIP": "198.51.100.9", "maxToolRounds": 25,
             "adsEnabled": False}),
    ]}}


def write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), StubHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="freemilk-capture-test-")
    try:
        # ---- fixture 1: full HAR session --------------------------
        har_path = os.path.join(tmp, "full.har")
        write_json(har_path, full_har())
        out1 = os.path.join(tmp, "out-full")
        proc = run_tool(["ingest", har_path, "--out", out1,
                         "--egress-ip", "203.0.113.7"])
        check("ingest(full HAR) exits 0", proc.returncode == 0,
              proc.stdout + proc.stderr)
        secrets = ("fake-milq-access-1", "fake-milq-access-2",
                   "fake-milq-refresh-1", "fake-milq-refresh-2",
                   "fake-proxy-key-AAAA", "fake-proxy-key-BBBB",
                   "fake-device-check-token", "fake-id-token")
        check("ingest output carries no secrets",
              all(s not in proc.stdout + proc.stderr for s in secrets))
        creds_path = os.path.join(out1, "credentials.json")
        report_path = os.path.join(out1, "capture_report.json")
        with open(creds_path) as fh:
            creds = json.load(fh)
        check("credentials carry the LAST prepare's profile",
              creds.get("baseURL") == "https://provider.example.test/v1"
              and creds.get("apiKey") == "fake-proxy-key-BBBB")
        check("protocol/model/sessionId inferred from the chat",
              creds.get("protocol") == "openai"
              and creds.get("model") == "opus-upstream-fake"
              and creds.get("sessionId") == "fake-session-123")
        check("milq section holds the LATEST session tokens",
              creds["milq"].get("milqAccessToken")
              == "fake-milq-access-2"
              and creds["milq"].get("milqRefreshToken")
              == "fake-milq-refresh-2")
        check("prepare endpoint remembered for login_fetch",
              creds["milq"].get("profileEndpointUsed")
              == "/v9/runtime/configs")
        refresh = creds.get("refreshCredentials") or {}
        check("refreshCredentials replays the captured prepare",
              refresh.get("url")
              == "https://api.milq.ai/v9/runtime/configs"
              and refresh.get("method") == "POST"
              and (refresh.get("headers") or {}).get("Authorization")
              == "Bearer fake-milq-access-1")
        check("both artifacts are mode 0600",
              (os.stat(creds_path).st_mode & 0o777) == 0o600
              and (os.stat(report_path).st_mode & 0o777) == 0o600)
        with open(report_path) as fh:
            report_text = fh.read()
        report = json.loads(report_text)
        check("report: full checklist coverage, nothing missing",
              report.get("missing") == []
              and all(item["found"]
                      for item in report["checklist"].values()))
        rotation = report["prepare"]["rotation"]
        check("report: 45-minute rotation measured, key changed",
              rotation["prepareCount"] == 2
              and rotation["intervalsSeconds"] == [2700.0]
              and rotation["keyChanged"] == [True])
        headers = {h["name"]: h["value"]
                   for h in report["prepare"]["request"]["headers"]}
        check("report: prepare headers full set, auth redacted",
              headers.get("X-Custom-Probe") == "probe-value"
              and "fake-milq-access-1" not in headers.get(
                  "Authorization", ""))
        check("report: no fake secret values anywhere",
              all(s not in report_text for s in secrets))
        check("report: clientIP vs egress note matches",
              report["clientIpVsEgress"]["prepareClientIP"]
              == "203.0.113.7"
              and report["clientIpVsEgress"]["matches"] is True)

        # ---- fixture 2: mitmproxy flow JSON, partial --------------
        flow_path = os.path.join(tmp, "flows.json")
        write_json(flow_path, flow_capture())
        out2 = os.path.join(tmp, "out-flow")
        proc = run_tool(["ingest", flow_path, "--out", out2])
        check("ingest(flow JSON) exits 0", proc.returncode == 0,
              proc.stdout + proc.stderr)
        with open(os.path.join(out2, "credentials.json")) as fh:
            creds2 = json.load(fh)
        check("flow credentials parsed from base64 bodies",
              creds2.get("apiKey") == "fake-flow-key-CCCC"
              and creds2["milq"].get("milqAccessToken")
              == "fake-flow-access-1")
        with open(os.path.join(out2, "capture_report.json")) as fh:
            report2 = json.load(fh)
        check("flow report: format + missing items named",
              report2.get("captureFormat") == "mitmproxy-flow-json"
              and sorted(report2.get("missing") or [])
              == ["3_catalog", "4_first_provider_chat",
                  "5_second_prepare", "6_session_refresh"],
              json.dumps(report2.get("missing")))
        check("flow ingest output carries no secrets",
              all(s not in proc.stdout + proc.stderr for s in
                  ("fake-flow-access-1", "fake-flow-refresh-1",
                   "fake-flow-key-CCCC", "fake-flow-dct")))

        # ---- fixture 3: no prepare --------------------------------
        bare_path = os.path.join(tmp, "bare.har")
        write_json(bare_path, {"log": {"version": "1.2", "entries": [
            signin_entry("2026-11-19T09:00:00+00:00")]}})
        out3 = os.path.join(tmp, "out-bare")
        proc = run_tool(["ingest", bare_path, "--out", out3])
        check("ingest without prepare exits 2, writes nothing",
              proc.returncode == 2
              and "no prepare" in proc.stdout
              and not os.path.exists(
                  os.path.join(out3, "credentials.json")))

        # ---- fixture 4a: replay-prepare, replayable ---------------
        har_a = os.path.join(tmp, "replay-ok.har")
        write_json(har_a, replay_har("/v9/runtime/configs",
                                     "fake-proxy-key-AAAA"))
        out_a = os.path.join(tmp, "out-replay-ok")
        proc = run_tool(["ingest", har_a, "--out", out_a])
        check("ingest(replay fixture A) exits 0",
              proc.returncode == 0, proc.stdout + proc.stderr)
        SEEN["authorization"] = None
        proc = run_tool(["replay-prepare", har_a, "--credentials",
                         os.path.join(out_a, "credentials.json")])
        check("replay-prepare exits 0 with REPLAYABLE/FRESH verdict",
              proc.returncode == 0 and "REPLAYABLE" in proc.stdout
              and "FRESH" in proc.stdout,
              proc.stdout + proc.stderr)
        check("stub saw the ingested (not captured) access token",
              SEEN["authorization"] == "Bearer fake-milq-access-1",
              str(SEEN["authorization"]))
        check("replay output carries no secrets",
              all(s not in proc.stdout + proc.stderr for s in
                  ("fake-milq-access-1", "fake-proxy-key-FRESH",
                   "fake-proxy-key-AAAA")))

        # ---- fixture 4b: replay-prepare, client gate --------------
        har_b = os.path.join(tmp, "replay-gate.har")
        write_json(har_b, replay_har("/v9/gated/configs",
                                     "fake-proxy-key-GATED"))
        out_b = os.path.join(tmp, "out-replay-gate")
        proc = run_tool(["ingest", har_b, "--out", out_b])
        check("ingest(replay fixture B) exits 0",
              proc.returncode == 0, proc.stdout + proc.stderr)
        proc = run_tool(["replay-prepare", har_b, "--credentials",
                         os.path.join(out_b, "credentials.json")])
        check("replay-prepare exits 2 with BLOCKED gate verdict",
              proc.returncode == 2 and "BLOCKED" in proc.stdout
              and "client gate" in proc.stdout,
              proc.stdout + proc.stderr)
    finally:
        server.shutdown()

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed),
                                     len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
