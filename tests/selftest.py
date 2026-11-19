#!/usr/bin/env python3
"""End-to-end self-test for freemilk-bridge against the mock upstream.

Spins up tests/mock_upstream.py on a scratch port and the bridge (as a
subprocess) with a temporary credentials file, then asserts:

  1. unconfigured: /health configured=false, chat -> 503 with the exact
     {"error": "freemilk-bridge not configured"} body
  2. openai profile: /v1/models lists opus-5.5; chat is forwarded with the
     right path, Authorization header, alias->model mapping and
     X-Session-Affinity; SSE streams pass through
  3. anthropic profile: request is translated (system separated, x-api-key
     header, /v1/messages path) and both buffered and streamed responses
     are translated back to OpenAI shape
  4. refresh: a stale key gets a 401, the bridge calls the refresh URL
     once, retries with the rotated key, and persists it to the
     credentials file
  5. 9Router shapes: a node-prefixed model name ("freemilk/opus-5.5")
     resolves to the alias, and the routes also answer without the
     /v1 prefix
  6. BRIDGE_API_KEY: /v1/* rejects missing/wrong bearer keys and
     accepts the exact key a 9Router connection would send

Run:  python3 tests/selftest.py        (exit code 0 = all passed)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mock_upstream  # noqa: E402

MOCK_PORT = 18711
BRIDGE_PORT = 18612
BRIDGE2_PORT = 18613
MOCK2_PORT = 18713
BRIDGE3_PORT = 18614
MOCK = "http://127.0.0.1:%d" % MOCK_PORT
BRIDGE = "http://127.0.0.1:%d" % BRIDGE_PORT
BRIDGE2 = "http://127.0.0.1:%d" % BRIDGE2_PORT

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))


def request(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8")


def wait_health():
    for _ in range(50):
        try:
            status, _ = request("GET", BRIDGE + "/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def main():
    mock = mock_upstream.create_server(MOCK_PORT)
    import threading
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="freemilk-selftest-")
    creds = os.path.join(tmp, "credentials.json")
    env = dict(os.environ)
    env.update({
        "FREEMILK_BRIDGE_HOST": "127.0.0.1",
        "FREEMILK_BRIDGE_PORT": str(BRIDGE_PORT),
        "FREEMILK_BRIDGE_CREDENTIALS": creds,
    })
    bridge = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bridge.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_health(), "bridge did not come up"

        # -- 1. unconfigured -------------------------------------------
        status, raw = request("GET", BRIDGE + "/health")
        health = json.loads(raw)
        check("health ok + unconfigured",
              status == 200 and health["ok"] and health["configured"] is False)
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5",
                               "messages": [{"role": "user", "content": "hi"}]})
        check("unconfigured chat -> 503 exact body",
              status == 503
              and json.loads(raw) == {"error": "freemilk-bridge not configured"},
              "got %d %s" % (status, raw[:120]))

        # -- 2. openai profile ------------------------------------------
        profile = {
            "baseURL": MOCK,
            "apiKey": "mock-key-1",
            "protocol": "openai",
            "model": "upstream-opus-id",
            "sessionId": "sess-123",
        }
        with open(creds, "w") as fh:
            json.dump(profile, fh)
        status, raw = request("GET", BRIDGE + "/v1/models")
        models = [m["id"] for m in json.loads(raw)["data"]]
        check("models lists opus-5.5", status == 200 and "opus-5.5" in models)
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5",
                               "messages": [{"role": "user", "content": "hi"}]})
        data = json.loads(raw)
        check("openai chat 200 + content",
              status == 200
              and data["choices"][0]["message"]["content"] == "Hello from mock")
        status, raw = request("GET", MOCK + "/__last")
        last = json.loads(raw)
        headers = last["headers"]
        check("openai forward: path", last["path"] == "/chat/completions")
        check("openai forward: bearer key",
              headers.get("authorization") == "Bearer mock-key-1")
        check("openai forward: model mapped",
              last["body"].get("model") == "upstream-opus-id")
        check("openai forward: session affinity",
              headers.get("x-session-affinity") == "sess-123")
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5", "stream": True,
                               "messages": [{"role": "user", "content": "hi"}]})
        check("openai stream passthrough",
              status == 200 and "Hello" in raw and "[DONE]" in raw)

        # -- 3. anthropic profile ---------------------------------------
        profile = {
            "baseURL": MOCK,
            "apiKey": "mock-key-1",
            "protocol": "anthropic",
            "model": "upstream-opus-id",
        }
        with open(creds, "w") as fh:
            json.dump(profile, fh)
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5",
                               "messages": [
                                   {"role": "system", "content": "Be terse."},
                                   {"role": "user", "content": "hi"}]})
        data = json.loads(raw)
        check("anthropic chat 200 + translated content",
              status == 200
              and data["choices"][0]["message"]["content"] == "Hello from mock",
              "got %d %s" % (status, raw[:160]))
        status, raw = request("GET", MOCK + "/__last")
        last = json.loads(raw)
        headers = last["headers"]
        check("anthropic forward: path", last["path"] == "/v1/messages")
        check("anthropic forward: x-api-key",
              headers.get("x-api-key") == "mock-key-1")
        check("anthropic forward: anthropic-version",
              headers.get("anthropic-version") == "2023-06-01")
        check("anthropic forward: system separated",
              last["body"].get("system") == "Be terse."
              and all(m["role"] != "system" for m in last["body"]["messages"]))
        check("anthropic forward: model mapped",
              last["body"].get("model") == "upstream-opus-id")
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5", "stream": True,
                               "messages": [{"role": "user", "content": "hi"}]})
        check("anthropic stream translated",
              status == 200 and "Hello" in raw and "from mock" in raw
              and "[DONE]" in raw and '"chat.completion.chunk"' in raw)

        # -- 4. refresh on 401 ------------------------------------------
        request("POST", MOCK + "/__reset")
        profile = {
            "baseURL": MOCK,
            "apiKey": "mock-key-stale",
            "protocol": "openai",
            "refreshCredentials": {"url": MOCK + "/refresh", "method": "POST"},
        }
        with open(creds, "w") as fh:
            json.dump(profile, fh)
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "opus-5.5",
                               "messages": [{"role": "user", "content": "hi"}]})
        data = json.loads(raw)
        check("refresh: chat succeeds after 401",
              status == 200
              and data["choices"][0]["message"]["content"] == "Hello from mock",
              "got %d %s" % (status, raw[:160]))
        with open(creds) as fh:
            persisted = json.load(fh)
        check("refresh: rotated key persisted",
              persisted.get("apiKey") == "mock-key-rotated")
        check("refresh: credentials file mode 0600",
              (os.stat(creds).st_mode & 0o777) == 0o600)
        status, raw = request("GET", MOCK + "/__stats")
        stats = json.loads(raw)
        check("refresh: endpoint hit exactly once",
              stats.get("/refresh") == 1, "stats=%s" % stats)

        # -- 5. 9Router shapes -------------------------------------------
        profile = {
            "baseURL": MOCK,
            "apiKey": "mock-key-1",
            "protocol": "openai",
            "model": "upstream-opus-id",
        }
        with open(creds, "w") as fh:
            json.dump(profile, fh)
        status, raw = request("POST", BRIDGE + "/v1/chat/completions",
                              {"model": "freemilk/opus-5.5",
                               "messages": [{"role": "user", "content": "hi"}]})
        data = json.loads(raw)
        check("9router: prefixed model name accepted",
              status == 200
              and data["choices"][0]["message"]["content"] == "Hello from mock",
              "got %d %s" % (status, raw[:120]))
        status, raw = request("GET", MOCK + "/__last")
        check("9router: prefix stripped before alias mapping",
              json.loads(raw)["body"].get("model") == "upstream-opus-id")
        status, raw = request("POST", BRIDGE + "/chat/completions",
                              {"model": "opus-5.5",
                               "messages": [{"role": "user", "content": "hi"}]})
        check("9router: /chat/completions alias (no /v1) works",
              status == 200
              and json.loads(raw)["choices"][0]["message"]["content"]
              == "Hello from mock")
        status, raw = request("GET", BRIDGE + "/models")
        check("9router: /models alias (no /v1) works",
              status == 200
              and "opus-5.5" in [m["id"] for m in json.loads(raw)["data"]])

        # -- 6. BRIDGE_API_KEY (the 9Router connection key) --------------
        bridge_key = "bridge-secret"
        env2 = dict(env)
        env2["FREEMILK_BRIDGE_PORT"] = str(BRIDGE2_PORT)
        env2["BRIDGE_API_KEY"] = bridge_key
        bridge2 = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bridge.py")],
            env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ok = False
            for _ in range(50):
                try:
                    s, _ = request("GET", BRIDGE2 + "/health")
                    ok = s == 200
                    if ok:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            assert ok, "bridge2 did not come up"
            status, _ = request("GET", BRIDGE2 + "/v1/models")
            check("bridge key: /v1/models without key -> 401",
                  status == 401)
            status, _ = request("GET", BRIDGE2 + "/v1/models",
                                headers={"Authorization":
                                         "Bearer wrong"})
            check("bridge key: wrong key -> 401", status == 401)
            status, raw = request(
                "GET", BRIDGE2 + "/v1/models",
                headers={"Authorization": "Bearer " + bridge_key})
            check("bridge key: correct key -> 200 (console validate "
                  "shape)", status == 200 and "opus-5.5" in raw)
            status, raw = request(
                "POST", BRIDGE2 + "/v1/chat/completions",
                {"model": "opus-5.5",
                 "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer " + bridge_key})
            check("bridge key: chat with connection key -> 200",
                  status == 200
                  and json.loads(raw)["choices"][0]["message"]["content"]
                  == "Hello from mock")
        finally:
            bridge2.terminate()

        # -- 7. account pool: pinning, LRU spread, dead isolation ------
        mock2 = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "mock_upstream.py"),
             str(MOCK2_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pool_creds = os.path.join(tmp, "pool-credentials.json")
        key_a = "fmb_acct-aaaa_" + "a" * 24
        key_b = "fmb_acct-bbbb_" + "b" * 24
        pool_profile = {"accounts": [
            {"id": "acct-aaaa", "bridgeKey": key_a,
             "baseURL": MOCK, "apiKey": "mock-key-1",
             "protocol": "openai", "model": "upstream-a"},
            {"id": "acct-bbbb", "bridgeKey": key_b,
             "baseURL": "http://127.0.0.1:%d" % MOCK2_PORT,
             "apiKey": "mock-key-1",
             "protocol": "openai", "model": "upstream-b"},
        ]}
        with open(pool_creds, "w") as fh:
            json.dump(pool_profile, fh)
        env3 = dict(env)
        env3["FREEMILK_BRIDGE_PORT"] = str(BRIDGE3_PORT)
        env3["FREEMILK_BRIDGE_CREDENTIALS"] = pool_creds
        env3["BRIDGE_API_KEY"] = "master-key"
        bridge3 = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bridge.py")],
            env=env3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        BRIDGE3 = "http://127.0.0.1:%d" % BRIDGE3_PORT
        MOCK2 = "http://127.0.0.1:%d" % MOCK2_PORT
        try:
            ok = False
            for _ in range(50):
                try:
                    s, _ = request("GET", BRIDGE3 + "/health")
                    ok = s == 200
                    if ok:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            assert ok, "bridge3 did not come up"
            status, raw = request("GET", BRIDGE3 + "/health")
            health = json.loads(raw)
            check("pool: health counts 2 healthy accounts",
                  health["configured"] is True
                  and health["accounts"] == 2
                  and health["accountsHealthy"] == 2)
            status, _ = request("GET", BRIDGE3 + "/v1/models")
            check("pool: keyless request rejected once keys exist",
                  status == 401)

            def chat(port_url, key):
                headers = {"Authorization": "Bearer " + key} if key \
                    else None
                return request(
                    "POST", port_url + "/v1/chat/completions",
                    {"model": "opus-5.5",
                     "messages": [{"role": "user", "content": "hi"}]},
                    headers=headers)

            def hits(url):
                _s, raw = request("GET", url + "/__stats")
                return json.loads(raw).get("/chat/completions", 0)

            request("POST", MOCK + "/__reset")
            request("POST", MOCK2 + "/__reset")
            status, _raw = chat(BRIDGE3, key_b)
            check("pool: per-account key pins to account B",
                  status == 200 and hits(MOCK2) == 1 and hits(MOCK) == 0)
            status, _raw = chat(BRIDGE3, key_a)
            check("pool: per-account key pins to account A",
                  status == 200 and hits(MOCK) == 1 and hits(MOCK2) == 1)
            request("POST", MOCK + "/__reset")
            request("POST", MOCK2 + "/__reset")
            chat(BRIDGE3, "master-key")
            chat(BRIDGE3, "master-key")
            check("pool: master key spreads LRU across accounts",
                  hits(MOCK) == 1 and hits(MOCK2) == 1,
                  "A=%d B=%d" % (hits(MOCK), hits(MOCK2)))

            # Account B's credential goes stale with no working
            # refresh: it must die alone while A keeps serving.
            pool_profile["accounts"][1]["apiKey"] = "mock-key-stale"
            pool_profile["accounts"][1]["refreshCredentials"] = {
                "url": MOCK2 + "/no-refresh", "method": "POST"}
            with open(pool_creds, "w") as fh:
                json.dump(pool_profile, fh)
            time.sleep(0.1)
            status, _raw = chat(BRIDGE3, key_b)
            check("pool: pinned dead-end account -> error, not success",
                  status in (502, 503), "got %d" % status)
            with open(pool_creds) as fh:
                persisted = json.load(fh)
            check("pool: failed account persisted as dead",
                  persisted["accounts"][1].get("dead") is True)
            status, raw = chat(BRIDGE3, "master-key")
            check("pool: surviving account keeps serving",
                  status == 200
                  and json.loads(raw)["choices"][0]["message"]["content"]
                  == "Hello from mock")
            status, raw = request("GET", BRIDGE3 + "/health")
            check("pool: health shows 1 healthy after death",
                  json.loads(raw)["accountsHealthy"] == 1)
        finally:
            bridge3.terminate()
            mock2.terminate()
    finally:
        bridge.terminate()
        mock.shutdown()

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
