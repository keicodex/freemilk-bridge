#!/usr/bin/env python3
"""capture_ingest.py — turn one captured FreeMilk app session into
bridge credentials, and run the decisive prepare-replay experiment.

Why this exists (RE round 3, 2026-11-19): the credential that buys
inference — the Milq proxy token — does not exist until the app asks
for it mid-session. A backend "prepare/configs" call mints it
(ConfigsResponse: {apiKey, baseUrl, clientIP, maxToolRounds, ...});
the response carries NO TTL field, so its lifetime is governed by
client-side staleness logic and by rejection-triggered re-prepare
(community reports put the effective rotation at ~45 minutes, after
which a fresh capture is needed). The prepare path is assembled
dynamically in the app — it is not a binary literal — and whether
Milq's server-side client gate covers the prepare namespace is
decidable ONLY from a real capture. This tool consumes that capture.

Commands:
  ingest CAPTURE [--out DIR] [--egress-ip IP]
      Parse a capture export — HAR (Proxyman / Charles / browser
      devtools) or mitmproxy flow JSON (a list of flow dicts, a
      {"flows": [...]} object, a single flow, or JSONL). Exchanges
      are identified by RESPONSE / PAYLOAD SHAPES, never by hardcoded
      paths (the real paths are dynamic and unknown):
        sign-in exchange   request body carries device_check_token,
                           or the response is TokenResponse-shaped
                           {access_token, refresh_token, expires_in,
                           user, intro_credits, ...}
        prepare / configs  response is ConfigsResponse-shaped
                           {apiKey, baseUrl, clientIP, maxToolRounds}
        catalog            response is HostedModelCatalog-shaped
                           {models[], limits, ...}
        provider chat      POST to the prepare-returned baseUrl with
                           a chat-shaped body {model, messages[]}
        session refresh    any later TokenResponse-shaped response
      Emits into DIR (default: alongside this script), both 0600:
        credentials.json   the bridge's existing format — profile
                           from the LAST prepare response, Milq
                           access/refresh tokens from the latest
                           token exchange, plus a refreshCredentials
                           hook built from the captured prepare
                           request so the bridge can attempt a
                           re-prepare when the provider rejects a key
        capture_report.json
                           checklist coverage vs the RE round-3 §0
                           minimal capture set (items 1-6), the
                           prepare request's full header set + path
                           (this is what settles the client-gate
                           question), the prepare response's clientIP
                           vs the --egress-ip note, and prepare
                           timestamps for rotation analysis.
      Exit 0 when credentials were written (missing checklist items
      are warnings, not failures); exit 2 when no prepare exchange
      was found — without one there is no profile to serve.

  replay-prepare CAPTURE [--credentials PATH] [--timeout SECONDS]
      Re-issue the captured prepare request from THIS host: same
      method, path and headers, with the Milq session auth taken
      from the ingested credentials (the stored milqAccessToken
      replaces the captured Authorization value when available).
      MANUAL, operator-invoked only — never wire this into a loop,
      a cron, or the bridge; it is an experiment, run once per
      capture to answer one question:
        * 200 + a ConfigsResponse with a NEW apiKey
              -> prepare is server-replayable: the bridge can
                 self-mint fresh proxy keys on a <45-minute cycle.
        * 200 + the SAME apiKey
              -> replayable, but no rotation was observed.
        * 403 client gate ("This client is not supported...")
              -> prepare sits behind the gate: captured proxy keys
                 are consumables, spent until they expire.
        * anything else / network failure
              -> failed or inconclusive; treat keys as consumables
                 until a replay succeeds.
      Exit 0 for the replayable outcomes, 2 otherwise.

Token values are NEVER printed: output and the report carry field
names, paths, and redacted fingerprints (prefix + length, or a short
sha256) only. No real credentials belong in captures used for tests.

Environment:
  FREEMILK_BRIDGE_CREDENTIALS  default --credentials path for
                               replay-prepare (same meaning as in
                               login_fetch.py)
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))

HOP_BY_HOP = {"host", "content-length", "connection",
              "transfer-encoding", "accept-encoding",
              "proxy-connection", "keep-alive", "te", "trailer",
              "upgrade"}

REDACT_HEADERS = {"authorization", "cookie", "set-cookie",
                  "proxy-authorization", "x-api-key",
                  "x-session-affinity"}


def log(msg):
    sys.stderr.write("capture-ingest: %s\n" % msg)
    sys.stderr.flush()


def redact(value):
    """Prefix + length only — enough to recognise a value across a
    report, never enough to use it."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= 8:
        return "<%d chars>" % len(text)
    return "%s…(%d chars)" % (text[:4], len(text))


def fingerprint(value):
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(
        str(value).encode("utf-8")).hexdigest()[:10]


def _norm_key(key):
    return str(key).lower().replace("_", "").replace("-", "")


def _get_norm(obj, *names):
    """First value in dict `obj` whose normalised key matches one of
    the (already normalised) `names`, else None."""
    if not isinstance(obj, dict):
        return None
    wanted = set(names)
    for key, value in obj.items():
        if _norm_key(key) in wanted:
            return value
    return None


def _has_norm(obj, *names):
    if not isinstance(obj, dict):
        return False
    wanted = set(names)
    return any(_norm_key(k) in wanted for k in obj.keys())


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Capture parsing: HAR + mitmproxy flow JSON -> common exchange dicts
#
# Exchange: {index, started (epoch|None), method, url, scheme, host,
#            path, request_headers [(name, value)], request_body,
#            request_body_text, status, response_headers,
#            response_body, response_body_text}
# --------------------------------------------------------------------------

def _parse_iso(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _decode_content(raw, encoding=None):
    """Capture bodies arrive as plain text or base64 (HAR content
    says so via `encoding`; mitmproxy state dumps are base64 without
    saying so). Tolerate both."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    if encoding == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return raw
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
        return decoded.decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return raw


def _parse_body(text):
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _header_pairs(raw):
    """Normalise HAR [{name, value}] / flow [[name, value]] / dict
    header shapes into an ordered [(name, value)] list."""
    pairs = []
    if isinstance(raw, dict):
        raw = list(raw.items())
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                pairs.append((str(item.get("name", "")),
                              str(item.get("value", ""))))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
    return pairs


def header_get(pairs, name):
    wanted = name.lower()
    for key, value in pairs or []:
        if key.lower() == wanted:
            return value
    return None


def _split_url(url):
    parts = urllib.parse.urlparse(url or "")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, parts.netloc, path


def _exchange(index, started, method, url, req_headers, req_text,
              status, resp_headers, resp_text):
    scheme, host, path = _split_url(url)
    return {
        "index": index, "started": started,
        "method": (method or "").upper(), "url": url or "",
        "scheme": scheme, "host": host, "path": path,
        "request_headers": req_headers or [],
        "request_body": _parse_body(req_text),
        "request_body_text": req_text,
        "status": status,
        "response_headers": resp_headers or [],
        "response_body": _parse_body(resp_text),
        "response_body_text": resp_text,
    }


def _normalize_har_entry(entry, index):
    if not isinstance(entry, dict):
        return None
    request = entry.get("request") or {}
    response = entry.get("response") or {}
    post = request.get("postData") or {}
    content = response.get("content") or {}
    return _exchange(
        index,
        _parse_iso(entry.get("startedDateTime")),
        request.get("method"), request.get("url"),
        _header_pairs(request.get("headers")),
        _decode_content(post.get("text")),
        response.get("status") if isinstance(
            response.get("status"), int) else None,
        _header_pairs(response.get("headers")),
        _decode_content(content.get("text"), content.get("encoding")))


def _normalize_flow(flow, index):
    if not isinstance(flow, dict):
        return None
    request = flow.get("request") or {}
    response = flow.get("response") or {}
    url = request.get("url")
    if not url:
        scheme = request.get("scheme") or "https"
        host = request.get("host") or ""
        port = request.get("port")
        netloc = host
        if port and not ((scheme == "https" and port == 443)
                         or (scheme == "http" and port == 80)):
            netloc = "%s:%s" % (host, port)
        url = "%s://%s%s" % (scheme, netloc, request.get("path") or "/")
    started = request.get("timestamp_start")
    if not isinstance(started, (int, float)):
        started = flow.get("timestamp_start")
    status = response.get("status_code", response.get("status"))
    return _exchange(
        index,
        float(started) if isinstance(started, (int, float)) else None,
        request.get("method"), url,
        _header_pairs(request.get("headers")),
        _decode_content(request.get("content")),
        status if isinstance(status, int) else None,
        _header_pairs(response.get("headers")),
        _decode_content(response.get("content")))


def _looks_like_har_entry(entry):
    """HAR entries carry request.postData and a response.content
    OBJECT ({text, encoding, ...}); mitmproxy flows carry
    request.content / response.status_code instead."""
    if not isinstance(entry, dict):
        return False
    request = entry.get("request")
    response = entry.get("response")
    if isinstance(request, dict) and "postData" in request:
        return True
    return isinstance(response, dict) \
        and isinstance(response.get("content"), dict)


def load_capture(path):
    """Read a capture file; return (format_name, [exchange, ...]).
    Raises ValueError with a human message on unreadable input."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError("cannot read %s (%s)"
                         % (path, type(exc).__name__))
    entries = None
    fmt = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(
            parsed.get("log"), dict) \
            and isinstance(parsed["log"].get("entries"), list):
        entries, fmt = parsed["log"]["entries"], "har"
    elif isinstance(parsed, dict) and isinstance(
            parsed.get("flows"), list):
        entries, fmt = parsed["flows"], "mitmproxy-flow-json"
    elif isinstance(parsed, list):
        entries, fmt = parsed, "mitmproxy-flow-json"
    elif isinstance(parsed, dict) and "request" in parsed:
        entries, fmt = [parsed], "mitmproxy-flow-json"
    if entries is None:
        # JSONL fallback: one flow object per line.
        lines = [ln for ln in text.splitlines() if ln.strip()]
        flows = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                flows.append(item)
        if flows:
            entries, fmt = flows, "mitmproxy-flow-json"
    if entries is None:
        raise ValueError("unrecognised capture format — expected a "
                         "HAR export or mitmproxy flow JSON")
    exchanges = []
    for index, entry in enumerate(entries):
        if fmt == "har" or _looks_like_har_entry(entry):
            exchange = _normalize_har_entry(entry, index)
        else:
            exchange = _normalize_flow(entry, index)
        if exchange is not None:
            exchanges.append(exchange)
    if not exchanges:
        raise ValueError("capture contained no HTTP exchanges")
    return fmt, exchanges


# --------------------------------------------------------------------------
# Shape-based exchange identification (never path-based)
# --------------------------------------------------------------------------

def is_token_response(obj):
    """Milq TokenResponse: {access_token, refresh_token, expires_in,
    user, intro_credits, ...} (camelCase spellings tolerated)."""
    if not isinstance(obj, dict):
        return False
    return bool(_get_norm(obj, "accesstoken")
                and _get_norm(obj, "refreshtoken")
                and _has_norm(obj, "expiresin", "user", "introcredits",
                              "status"))


def extract_configs(obj, depth=0):
    """Milq ConfigsResponse -> normalised {apiKey, baseUrl, clientIP,
    maxToolRounds}, or None. Requires apiKey + baseUrl AND at least
    one app-config field (clientIP / maxToolRounds / adsEnabled /
    ratingPromptEnabled / discordInvite) at the same level — that is
    what separates a prepare response from the per-model endpoint
    profiles nested inside a catalog, which carry baseUrl+apiKey but
    none of the config fields."""
    if not isinstance(obj, dict) or depth > 2:
        return None
    api_key = _get_norm(obj, "apikey", "proxytoken", "proxykey")
    base_url = _get_norm(obj, "baseurl", "proxybaseurl", "proxyurl")
    if isinstance(api_key, str) and api_key \
            and isinstance(base_url, str) and base_url \
            and _has_norm(obj, "clientip", "maxtoolrounds", "adsenabled",
                          "ratingpromptenabled", "discordinvite"):
        return {"apiKey": api_key, "baseUrl": base_url,
                "clientIP": _get_norm(obj, "clientip"),
                "maxToolRounds": _get_norm(obj, "maxtoolrounds")}
    for key, value in obj.items():
        if _norm_key(key) in ("configs", "data", "result", "profile") \
                and isinstance(value, dict):
            found = extract_configs(value, depth + 1)
            if found:
                return found
    return None


def is_catalog(obj):
    """HostedModelCatalog: {models[], limits, agents, testModels}."""
    if not isinstance(obj, dict):
        return False
    models = _get_norm(obj, "models")
    if not isinstance(models, list) or not models \
            or not isinstance(models[0], dict):
        return False
    if _has_norm(obj, "limits", "agents", "testmodels"):
        return True
    return _has_norm(models[0], "tier", "requestformat",
                     "contextwindow", "isdefault", "acceptsimages")


def _body_has_key(obj, wanted_norm, depth=0):
    if depth > 4:
        return False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _norm_key(key) == wanted_norm and value:
                return True
            if _body_has_key(value, wanted_norm, depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_body_has_key(item, wanted_norm, depth + 1)
                   for item in obj)
    return False


def _is_chat_request(exchange):
    body = exchange["request_body"]
    return exchange["method"] == "POST" \
        and isinstance(body, dict) \
        and isinstance(_get_norm(body, "messages"), list) \
        and bool(_get_norm(body, "model"))


def _url_under(url, base):
    if not url or not base:
        return False
    base = base.rstrip("/")
    return url == base or url.startswith(base + "/") \
        or url.startswith(base + "?")


def analyze(exchanges):
    """Classify exchanges by shape. Returns a dict of findings; all
    lists are in capture-time order (timestamp, then file order)."""
    ordered = sorted(exchanges, key=lambda e: (
        e["started"] is None, e["started"] or 0, e["index"]))

    token_exchanges = [e for e in ordered
                       if is_token_response(e["response_body"])]
    signin = None
    for exchange in token_exchanges:
        if _body_has_key(exchange["request_body"], "devicechecktoken"):
            signin = exchange
            break
    if signin is None and token_exchanges:
        signin = token_exchanges[0]
    if signin is None:
        # A device_check_token request whose response was not
        # TokenResponse-shaped still proves the exchange happened.
        for exchange in ordered:
            if _body_has_key(exchange["request_body"],
                             "devicechecktoken"):
                signin = exchange
                break
    refreshes = [e for e in token_exchanges
                 if signin is None or e["index"] != signin["index"]]

    prepares = []
    for exchange in ordered:
        if is_catalog(exchange["response_body"]):
            continue
        configs = extract_configs(exchange["response_body"])
        if configs:
            prepares.append((exchange, configs))

    catalogs = [e for e in ordered if is_catalog(e["response_body"])]

    base_urls = [cfg["baseUrl"] for _e, cfg in prepares]
    chats = []
    for exchange in ordered:
        if not _is_chat_request(exchange):
            continue
        if any(_url_under(exchange["url"], base) for base in base_urls):
            chats.append(exchange)
        elif not base_urls and "api.milq.ai" not in exchange["host"]:
            chats.append(exchange)

    return {"signin": signin, "refreshes": refreshes,
            "token_exchanges": token_exchanges, "prepares": prepares,
            "catalogs": catalogs, "chats": chats}


# --------------------------------------------------------------------------
# Emitted artifacts
# --------------------------------------------------------------------------

def _write_private_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _redacted_headers(pairs):
    out = []
    for name, value in pairs or []:
        if _norm_key(name) in REDACT_HEADERS \
                or "token" in _norm_key(name) \
                or _norm_key(name).endswith("apikey"):
            out.append({"name": name, "value": redact(value)})
        else:
            out.append({"name": name, "value": value})
    return out


def _checklist_item(exchange, extra=None):
    item = {"found": exchange is not None}
    if exchange is not None:
        item["exchange"] = exchange["index"]
        item["at"] = _iso(exchange["started"])
        item["method"] = exchange["method"]
        item["path"] = exchange["path"]
        item["status"] = exchange["status"]
    if extra:
        item.update(extra)
    return item


def build_credentials(findings):
    """Bridge credentials (existing format) from the findings: the
    profile of the LAST prepare, session material of the LATEST
    token exchange, and a refreshCredentials hook replaying the
    FIRST prepare request (the canonical one, right after sign-in).
    Returns (credentials_dict, notes)."""
    notes = []
    last_exchange, last_cfg = findings["prepares"][-1]
    creds = {"baseURL": last_cfg["baseUrl"], "apiKey": last_cfg["apiKey"]}

    chats = findings["chats"]
    if chats:
        chat = chats[0]
        path = chat["path"].split("?")[0]
        if path.endswith("/messages"):
            creds["protocol"] = "anthropic"
        elif path.endswith("/responses"):
            creds["protocol"] = "responses"
        else:
            creds["protocol"] = "openai"
        model = _get_norm(chat["request_body"], "model")
        if isinstance(model, str) and model:
            creds["model"] = model
        affinity = header_get(chat["request_headers"],
                              "X-Session-Affinity")
        if affinity:
            creds["sessionId"] = affinity
    else:
        notes.append("no provider chat captured — protocol/model "
                     "left at bridge defaults")
        if findings["catalogs"]:
            models = _get_norm(findings["catalogs"][0]["response_body"],
                               "models") or []
            for entry in models:
                if isinstance(entry, dict) \
                        and _get_norm(entry, "isdefault"):
                    model_id = _get_norm(entry, "id", "name")
                    if isinstance(model_id, str) and model_id:
                        creds["model"] = model_id
                    break

    # refreshCredentials: re-issuing the captured prepare request is
    # exactly the bridge's 401 refresh hook — its response is a
    # ConfigsResponse, whose apiKey/baseUrl the bridge extracts.
    first_exchange = findings["prepares"][0][0]
    headers = {}
    for name, value in first_exchange["request_headers"]:
        if _norm_key(name) in HOP_BY_HOP:
            continue
        headers[name] = value
    refresh = {"url": first_exchange["url"],
               "method": first_exchange["method"]}
    if headers:
        refresh["headers"] = headers
    if first_exchange["method"] != "GET":
        if first_exchange["request_body"] is not None:
            refresh["body"] = first_exchange["request_body"]
        elif first_exchange["request_body_text"]:
            refresh["body"] = first_exchange["request_body_text"]
    creds["refreshCredentials"] = refresh

    milq = {}
    token_exchanges = findings["token_exchanges"]
    if token_exchanges:
        latest = token_exchanges[-1]
        body = latest["response_body"]
        access = _get_norm(body, "accesstoken")
        refresh_token = _get_norm(body, "refreshtoken")
        if isinstance(access, str) and access:
            milq["milqAccessToken"] = access
        if isinstance(refresh_token, str) and refresh_token:
            milq["milqRefreshToken"] = refresh_token
        expires_in = _get_norm(body, "expiresin")
        if latest["started"] is not None and expires_in is not None:
            try:
                milq["milqAccessTokenExpiry"] = int(
                    latest["started"]) + int(expires_in)
            except (TypeError, ValueError):
                pass
    else:
        notes.append("no TokenResponse captured — credentials carry "
                     "no Milq session material")
    milq["profileEndpointUsed"] = first_exchange["path"].split("?")[0]
    milq["profileEndpoints"] = [
        {"method": first_exchange["method"],
         "path": first_exchange["path"].split("?")[0]}]
    creds["milq"] = milq
    return creds, notes


def build_report(fmt, capture_path, exchanges, findings, egress_ip,
                 notes):
    prepares = findings["prepares"]
    intervals = []
    key_changed = []
    for prev, cur in zip(prepares, prepares[1:]):
        if prev[0]["started"] is not None \
                and cur[0]["started"] is not None:
            intervals.append(round(cur[0]["started"]
                                   - prev[0]["started"], 1))
        else:
            intervals.append(None)
        key_changed.append(prev[1]["apiKey"] != cur[1]["apiKey"])

    first_exchange, _cfg = prepares[0]
    last_exchange, last_cfg = prepares[-1]
    signin = findings["signin"]
    latest_token = findings["token_exchanges"][-1] \
        if findings["token_exchanges"] else None

    checklist = {
        "1_signin_exchange": _checklist_item(signin, {
            "deviceCheckTokenInRequest": bool(
                signin and _body_has_key(signin["request_body"],
                                         "devicechecktoken")),
            "tokenResponseShaped": bool(
                signin and is_token_response(signin["response_body"])),
        } if signin else None),
        "2_prepare": _checklist_item(first_exchange),
        "3_catalog": _checklist_item(
            findings["catalogs"][0] if findings["catalogs"] else None),
        "4_first_provider_chat": _checklist_item(
            findings["chats"][0] if findings["chats"] else None),
        "5_second_prepare": _checklist_item(
            prepares[1][0] if len(prepares) > 1 else None),
        "6_session_refresh": _checklist_item(
            findings["refreshes"][0] if findings["refreshes"] else None),
    }

    report = {
        "tool": "capture_ingest.py",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "captureFile": os.path.basename(capture_path),
        "captureFormat": fmt,
        "exchangeCount": len(exchanges),
        "checklist": checklist,
        "missing": [name for name, item in checklist.items()
                    if not item["found"]],
        "prepare": {
            "request": {
                "method": first_exchange["method"],
                "url": first_exchange["url"],
                "path": first_exchange["path"],
                "headers": _redacted_headers(
                    first_exchange["request_headers"]),
                "bodyFieldNames": sorted(
                    str(k) for k in first_exchange["request_body"])
                if isinstance(first_exchange["request_body"], dict)
                else [],
            },
            "response": {
                "baseUrl": last_cfg["baseUrl"],
                "clientIP": last_cfg["clientIP"],
                "maxToolRounds": last_cfg["maxToolRounds"],
                "apiKeyRedacted": redact(last_cfg["apiKey"]),
                "apiKeyFingerprint": fingerprint(last_cfg["apiKey"]),
            },
            "profileFromExchange": last_exchange["index"],
            "allPrepares": [
                {"exchange": ex["index"], "at": _iso(ex["started"]),
                 "clientIP": cfg["clientIP"],
                 "apiKeyFingerprint": fingerprint(cfg["apiKey"])}
                for ex, cfg in prepares],
            "rotation": {
                "prepareCount": len(prepares),
                "intervalsSeconds": intervals,
                "keyChanged": key_changed,
                "note": "the ConfigsResponse carries no TTL field; "
                        "intervals between captured prepares are "
                        "the only rotation evidence available",
            },
        },
        "clientIpVsEgress": {
            "prepareClientIP": last_cfg["clientIP"],
            "captureEgressIp": egress_ip,
            "matches": (egress_ip is not None
                        and last_cfg["clientIP"] == egress_ip)
            if egress_ip is not None and last_cfg["clientIP"]
            else None,
        },
        "session": {
            "tokenExchanges": len(findings["token_exchanges"]),
            "latestAccessTokenRedacted": redact(_get_norm(
                latest_token["response_body"], "accesstoken"))
            if latest_token else None,
            "refreshTokenPresent": bool(latest_token and _get_norm(
                latest_token["response_body"], "refreshtoken")),
        },
        "credentialsWritten": "credentials.json",
        "warnings": notes,
    }
    return report


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ingest(capture_path, out_dir, egress_ip):
    try:
        fmt, exchanges = load_capture(capture_path)
    except ValueError as exc:
        print("ingest: %s" % exc)
        return 2
    findings = analyze(exchanges)
    print("ingest: %s capture, %d exchange(s)"
          % (fmt, len(exchanges)))
    if not findings["prepares"]:
        print("ingest: no prepare/configs exchange found (no "
              "ConfigsResponse-shaped response in the capture) — "
              "there is no provider profile to emit. Capture one "
              "genuine app session that includes the prepare call "
              "(RE round-3 report, checklist item 2).")
        return 2
    creds, notes = build_credentials(findings)
    report = build_report(fmt, capture_path, exchanges, findings,
                          egress_ip, notes)

    os.makedirs(out_dir, exist_ok=True)
    creds_path = os.path.join(out_dir, "credentials.json")
    if os.path.exists(creds_path):
        try:
            with open(creds_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict):
                merged = dict(existing)
                merged.update(creds)
                creds = merged
                print("ingest: merged into the existing "
                      "credentials.json (pool accounts and other "
                      "keys preserved)")
        except (OSError, ValueError):
            log("existing credentials.json unreadable; overwriting")
    _write_private_json(creds_path, creds)
    report_path = os.path.join(out_dir, "capture_report.json")
    _write_private_json(report_path, report)

    labels = {"1_signin_exchange": "sign-in exchange",
              "2_prepare": "prepare/configs",
              "3_catalog": "hosted-model catalog",
              "4_first_provider_chat": "first provider chat",
              "5_second_prepare": "second prepare",
              "6_session_refresh": "session refresh"}
    for name in sorted(report["checklist"]):
        item = report["checklist"][name]
        if item["found"]:
            print("  checklist %s: found (exchange #%d, %s %s)"
                  % (labels[name], item["exchange"], item["method"],
                     item["path"]))
        else:
            print("  checklist %s: MISSING" % labels[name])
    rotation = report["prepare"]["rotation"]
    if rotation["prepareCount"] > 1:
        print("  prepares: %d; interval(s) %s; key changed: %s"
              % (rotation["prepareCount"],
                 rotation["intervalsSeconds"],
                 rotation["keyChanged"]))
    for note in notes:
        print("  warning: %s" % note)
    print("ingest: wrote %s (0600)" % creds_path)
    print("ingest: wrote %s (0600)" % report_path)
    print("ingest: next — `login_fetch.py status`, then "
          "`login_fetch.py sync-9router`; and run "
          "`capture_ingest.py replay-prepare %s` once to learn "
          "whether fresh proxy keys can be minted from this host."
          % os.path.basename(capture_path))
    return 0


def _load_milq_access_token(credentials_path):
    try:
        with open(credentials_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sections = []
    if isinstance(data.get("milq"), dict):
        sections.append(data["milq"])
    entries = data.get("accounts")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) \
                    and isinstance(entry.get("milq"), dict):
                sections.append(entry["milq"])
    for section in sections:
        token = section.get("milqAccessToken")
        if isinstance(token, str) and token:
            return token
    return None


def _send(method, url, headers, body_text, timeout):
    payload = None
    if body_text is not None and method != "GET":
        payload = body_text.encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except OSError:
        return 0, b""


def cmd_replay_prepare(capture_path, credentials_path, timeout):
    try:
        _fmt, exchanges = load_capture(capture_path)
    except ValueError as exc:
        print("replay-prepare: %s" % exc)
        return 2
    findings = analyze(exchanges)
    if not findings["prepares"]:
        print("replay-prepare: the capture contains no prepare "
              "exchange to replay")
        return 2
    exchange, captured_cfg = findings["prepares"][0]

    headers = {}
    for name, value in exchange["request_headers"]:
        if _norm_key(name) in HOP_BY_HOP:
            continue
        headers[name] = value
    token = _load_milq_access_token(credentials_path)
    if token:
        replaced = False
        for name in list(headers):
            if name.lower() == "authorization":
                scheme = headers[name].split(" ", 1)[0] \
                    if " " in headers[name] else "Bearer"
                headers[name] = "%s %s" % (scheme, token)
                replaced = True
        if not replaced:
            headers["Authorization"] = "Bearer " + token
        print("replay-prepare: session auth from %s (access token %s)"
              % (credentials_path, redact(token)))
    else:
        print("replay-prepare: no stored Milq access token found in "
              "%s — replaying with the captured headers verbatim"
              % credentials_path)
    if exchange["request_body_text"]:
        headers.setdefault("Content-Type", "application/json")

    print("replay-prepare: %s %s (captured exchange #%d)"
          % (exchange["method"], exchange["url"], exchange["index"]))
    status, raw = _send(exchange["method"], exchange["url"], headers,
                        exchange["request_body_text"], timeout)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = None

    if status == 200:
        configs = extract_configs(parsed)
        if configs:
            if configs["apiKey"] != captured_cfg["apiKey"]:
                print("REPLAY VERDICT: REPLAYABLE — prepare answered "
                      "200 with a FRESH apiKey (%s, was %s). The "
                      "bridge can self-mint proxy keys from this "
                      "host; no re-capture is needed per rotation."
                      % (fingerprint(configs["apiKey"]),
                         fingerprint(captured_cfg["apiKey"])))
                return 0
            print("REPLAY VERDICT: REPLAYABLE — prepare answered 200 "
                  "but returned the SAME apiKey (%s); no rotation "
                  "observed on this replay."
                  % fingerprint(configs["apiKey"]))
            return 0
        print("REPLAY VERDICT: FAILED — HTTP 200 but the response "
              "is not ConfigsResponse-shaped; cannot mint keys "
              "this way.")
        return 2
    if status == 403 and parsed is not None \
            and "client is not supported" in json.dumps(parsed).lower():
        print("REPLAY VERDICT: BLOCKED — the prepare namespace sits "
              "behind the client gate (403 \"This client is not "
              "supported\"). Captured proxy keys are consumables: "
              "each capture yields one key, spent until the "
              "provider rejects it (~45 min per community reports); "
              "only a fresh genuine-app capture mints another.")
        return 2
    if status == 0:
        print("REPLAY VERDICT: INCONCLUSIVE — the request never "
              "completed (network error / timeout). Nothing was "
              "decided; do not treat this as a gate answer.")
        return 2
    print("REPLAY VERDICT: FAILED — HTTP %d (not a configs "
          "response). Treat captured proxy keys as consumables "
          "until a replay succeeds." % status)
    return 2


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _flag_value(args, flag):
    if flag in args:
        pos = args.index(flag)
        if pos + 1 < len(args):
            return args[pos + 1], args[:pos] + args[pos + 2:]
    return None, args


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    rest = argv[2:]
    if command == "ingest":
        out_dir, rest = _flag_value(rest, "--out")
        egress_ip, rest = _flag_value(rest, "--egress-ip")
        if not rest:
            print("usage: capture_ingest.py ingest CAPTURE "
                  "[--out DIR] [--egress-ip IP]")
            return 2
        return cmd_ingest(rest[0], out_dir or _HERE, egress_ip)
    if command == "replay-prepare":
        creds_path, rest = _flag_value(rest, "--credentials")
        timeout_raw, rest = _flag_value(rest, "--timeout")
        if not rest:
            print("usage: capture_ingest.py replay-prepare CAPTURE "
                  "[--credentials PATH] [--timeout SECONDS]")
            return 2
        if not creds_path:
            creds_path = os.environ.get(
                "FREEMILK_BRIDGE_CREDENTIALS",
                os.path.join(_HERE, "credentials.json"))
        try:
            timeout = int(timeout_raw) if timeout_raw else 20
        except ValueError:
            timeout = 20
        return cmd_replay_prepare(rest[0], creds_path, timeout)
    print("unknown command: %s" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
