#!/usr/bin/env python3
"""freemilk-bridge — replay harvested FreeMilk (Milq AI) provider profiles
as an OpenAI-compatible upstream, with a multi-account pool.

FreeMilk's app receives a *provider profile* ({baseURL, apiKey, ...,
refreshCredentials}) from api.milq.ai after sign-in and then talks directly
to that provider in one of three wire formats (OpenAI-compatible Chat
Completions, Anthropic Messages, OpenAI Responses). This bridge holds one
or more such profiles in a local credentials file and exposes them on
localhost as a plain OpenAI-compatible endpoint, so a router (e.g.
9Router) can consume them like any other upstream.

Pool behaviour:
  * The credentials file may hold a single legacy profile (top-level
    fields) and/or an "accounts" list; every entry with baseURL+apiKey
    is a serving account (a pool of 1 behaves exactly like before).
  * Requests are spread across healthy accounts least-recently-used.
  * Each account may carry a "bridgeKey" ("fmb_<accountId>_<secret>",
    issued by login_fetch.py). A request presenting that key as its
    Bearer token is pinned to that account — this is how every account
    becomes its own connection in 9Router and the router's native
    rotation spreads load. Without a recognised key, pool selection
    applies.
  * An account whose upstream answers 401 even after its credential
    refresh is marked dead (persisted in the credentials file) and
    skipped; the request fails over to the next healthy account.

Pure Python 3 standard library — no dependencies.

Configuration (environment):
  FREEMILK_BRIDGE_HOST        bind host            (default 127.0.0.1)
  FREEMILK_BRIDGE_PORT        bind port            (default 18611)
  FREEMILK_BRIDGE_CREDENTIALS credentials path     (default ./credentials.json
                                                    next to this file)
  BRIDGE_API_KEY              optional master bearer key for /v1/* routes
  FREEMILK_BRIDGE_KEY         legacy alias of BRIDGE_API_KEY
  FREEMILK_BRIDGE_CONCURRENCY max parallel upstream calls (default 8)
  FREEMILK_BRIDGE_ACCOUNT_CONCURRENCY
                              max parallel calls per account (default 1)

9Router compatibility: a stock openai-compatible node pointed at
http://127.0.0.1:<port>/v1 works as-is — the router POSTs
/v1/chat/completions with "Authorization: Bearer <connection apiKey>"
and the model name with the node prefix already stripped; the bridge
additionally tolerates prefixed names ("freemilk/opus-5.5") and the
same routes without the /v1 prefix. If a bridge key is set, put that
exact value in the 9Router connection's API key field.

Credentials file — see credentials.example.json. Either a single
profile at top level (legacy) or {"accounts": [profile, ...]} (pool).
Per profile:
  baseURL            (required) provider base URL from the profile
  apiKey             (required) provider credential from the profile
  id                 account id (pool entries; legacy defaults to
                     "default")
  bridgeKey          per-account key this bridge issues; a request
                     bearing it is routed to this account
  protocol           "openai" | "anthropic" | "responses" (default "openai")
  model              upstream model id that the alias "opus-5.5" maps to
  models             {alias: upstreamId} extra alias mappings
  sessionId          sent as X-Session-Affinity when present
                     ("sessionAffinity" is accepted as an alias)
  extraHeaders       extra headers merged into upstream requests
  maxApiTokens       default max_tokens for the Anthropic protocol
  refreshCredentials refresh hook: an object with a URL under one of
                     url/endpoint/refreshUrl, optional method/headers/body.
                     On upstream 401 the bridge calls it once, extracts a
                     new apiKey (and baseURL/refreshCredentials if rotated)
                     from the JSON response, persists the profile back to
                     the credentials file (0600) and retries once.

An optional "milq" object (top level for the legacy account, or inside
each pool entry) holds FreeMilk login material used only by
login_fetch.py; bridge.py ignores it.

Logs go to stderr and NEVER contain apiKeys, bridge keys or refresh
material.
"""

import http.client
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.3.0"
DEFAULT_ALIAS = "opus-5.5"
ANTHROPIC_VERSION = "2023-06-01"

HOST = os.environ.get("FREEMILK_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FREEMILK_BRIDGE_PORT", "18611"))
BRIDGE_KEY = os.environ.get("BRIDGE_API_KEY") \
    or os.environ.get("FREEMILK_BRIDGE_KEY", "")
CONCURRENCY = int(os.environ.get("FREEMILK_BRIDGE_CONCURRENCY", "8"))
ACCOUNT_CONCURRENCY = int(os.environ.get(
    "FREEMILK_BRIDGE_ACCOUNT_CONCURRENCY", "1"))
_HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.environ.get(
    "FREEMILK_BRIDGE_CREDENTIALS", os.path.join(_HERE, "credentials.json")
)

UPSTREAM_TIMEOUT = 300          # seconds, generous for streamed generations
CONNECT_TIMEOUT = 20
_semaphore = threading.BoundedSemaphore(CONCURRENCY)


def log(msg):
    sys.stderr.write("[%s] freemilk-bridge: %s\n" % (
        time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Account pool (hot-reload on mtime change, atomic persist)
#
# The credentials file is either a legacy single profile (top-level
# baseURL/apiKey) or {"accounts": [entry, ...]}, or both. Every entry
# with baseURL+apiKey becomes a serving Account. Runtime state
# (semaphore, last-used, dead flag) lives on the Account objects; the
# dead flag is also persisted so a restart keeps skipping bad accounts.
# --------------------------------------------------------------------------

class Account:
    def __init__(self, account_id, profile, legacy=False):
        self.id = account_id
        self.profile = profile
        self.legacy = legacy
        self.semaphore = threading.BoundedSemaphore(
            max(1, ACCOUNT_CONCURRENCY))
        self.last_used = 0.0
        self.dead = bool(profile.get("dead")) if isinstance(profile, dict) \
            else False

    def usable(self):
        return bool(self.profile.get("baseURL") and self.profile.get("apiKey"))


class Pool:
    def __init__(self, accounts):
        self.accounts = accounts

    def healthy(self):
        return [a for a in self.accounts if a.usable() and not a.dead]

    def lru(self):
        return sorted(self.healthy(), key=lambda a: a.last_used)

    def by_bridge_key(self, presented):
        if not presented:
            return None
        for account in self.accounts:
            key = account.profile.get("bridgeKey")
            if key and key == presented:
                return account
        return None

    def has_bridge_keys(self):
        return any(a.profile.get("bridgeKey") for a in self.accounts)

    def aliases(self):
        out = []
        for account in self.accounts:
            for alias in known_aliases(account.profile):
                if alias not in out:
                    out.append(alias)
        return out or [DEFAULT_ALIAS]


def _build_pool(data):
    accounts = []
    if not isinstance(data, dict):
        return accounts
    if data.get("baseURL") and data.get("apiKey"):
        accounts.append(Account(str(data.get("accountId") or "default"),
                                data, legacy=True))
    entries = data.get("accounts")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("baseURL") \
                    and entry.get("apiKey"):
                accounts.append(Account(str(entry.get("id") or "acct"),
                                        entry))
    return accounts


class ProfileStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self._pool = None

    def get(self):
        """Return the current Pool, or None when no credentials file
        yields any serving account structure at all."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            with self._lock:
                self._mtime = None
                self._pool = None
            return None
        with self._lock:
            if self._pool is not None and self._mtime == mtime:
                return self._pool
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                log("credentials file unreadable (%s); treating as "
                    "unconfigured" % type(exc).__name__)
                self._mtime = mtime
                self._pool = None
                return None
            accounts = _build_pool(data)
            if not accounts:
                log("credentials file present but no account carries "
                    "baseURL/apiKey; treating as unconfigured")
                self._mtime = mtime
                self._pool = None
                return None
            self._mtime = mtime
            self._pool = Pool(accounts)
            log("credentials loaded: %d account(s), %d healthy" % (
                len(accounts), len(self._pool.healthy())))
            return self._pool

    def _write(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with self._lock:
            self._mtime = None  # force reload on next get()
            self._pool = None

    def _read_raw(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def persist_account(self, account, new_profile):
        """Write one account's (refreshed) profile back to the file,
        preserving every other account and the milq sections."""
        data = self._read_raw()
        if account.legacy:
            for key in list(data.keys()):
                if key not in ("accounts", "milq"):
                    del data[key]
            for key, value in new_profile.items():
                if key not in ("accounts", "milq"):
                    data[key] = value
        else:
            entries = data.get("accounts")
            if not isinstance(entries, list):
                entries = []
                data["accounts"] = entries
            for idx, entry in enumerate(entries):
                if isinstance(entry, dict) \
                        and str(entry.get("id") or "acct") == account.id:
                    merged = dict(entry)
                    merged.update(new_profile)
                    entries[idx] = merged
                    break
            else:
                entries.append(dict(new_profile))
        self._write(data)
        account.profile = new_profile

    def mark_dead(self, account, reason):
        profile = dict(account.profile)
        profile["dead"] = True
        profile["deadReason"] = reason
        profile["deadAt"] = int(time.time())
        account.dead = True
        try:
            self.persist_account(account, profile)
        except OSError as exc:
            log("account %s: could not persist dead flag (%s)"
                % (account.id, type(exc).__name__))
        log("account %s marked dead (%s)" % (account.id, reason))


STORE = ProfileStore(CREDENTIALS_PATH)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def resolve_model(profile, requested):
    """Map a requested model name to the upstream model id.

    Tolerates node-prefixed names ("freemilk/opus-5.5", as a client or
    a combo may send them even though 9Router normally strips the node
    prefix itself): leading path segments are peeled off one by one
    until a known alias matches. A name that already IS the upstream
    id also passes through unchanged."""
    models = profile.get("models")
    if not isinstance(models, dict):
        models = {}
    candidates = [requested]
    if isinstance(requested, str) and "/" in requested:
        parts = requested.split("/")
        candidates += ["/".join(parts[i:]) for i in range(1, len(parts))]
    for cand in candidates:
        if cand in models:
            return models[cand]
        if cand == DEFAULT_ALIAS and profile.get("model"):
            return profile["model"]
    upstream_ids = set(models.values())
    if profile.get("model"):
        upstream_ids.add(profile["model"])
    for cand in candidates:
        if cand in upstream_ids:
            return cand
    return requested


def known_aliases(profile):
    aliases = [DEFAULT_ALIAS]
    if profile:
        models = profile.get("models")
        if isinstance(models, dict):
            for key in models:
                if key not in aliases:
                    aliases.append(key)
    return aliases


def join_url(base, suffix, protocol):
    """Join a provider base URL with a protocol path.

    Handles base URLs that already end in /v1 (common for all three
    protocols) so we never produce /v1/v1/..."""
    base = str(base).rstrip("/")
    if protocol == "anthropic":
        path = "/messages" if base.endswith("/v1") else "/v1/messages"
    elif protocol == "responses":
        path = "/responses"
    else:  # openai
        path = "/chat/completions"
    return base + path


def open_connection(url, timeout):
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443,
            timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(
            parts.hostname, parts.port or 80, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return conn, path


def base_headers(profile, protocol):
    headers = {}
    extra = profile.get("extraHeaders")
    if isinstance(extra, dict):
        for key, value in extra.items():
            headers[str(key)] = str(value)
    if protocol == "anthropic":
        headers["x-api-key"] = str(profile["apiKey"])
        headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
    else:
        headers["Authorization"] = "Bearer " + str(profile["apiKey"])
    session = profile.get("sessionId") or profile.get("sessionAffinity")
    if session:
        headers["X-Session-Affinity"] = str(session)
    headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------
# Credential refresh
# --------------------------------------------------------------------------

def _find_key_material(obj, depth=0):
    """Search a JSON response for rotated credential fields."""
    found = {}
    if depth > 3 or not isinstance(obj, dict):
        return found
    for key, value in obj.items():
        lk = str(key).lower()
        if lk in ("apikey", "api_key", "accesstoken", "access_token", "token") \
                and isinstance(value, str) and value:
            found.setdefault("apiKey", value)
        elif lk in ("baseurl", "base_url") and isinstance(value, str) and value:
            found.setdefault("baseURL", value)
        elif lk == "refreshcredentials" and isinstance(value, dict):
            found.setdefault("refreshCredentials", value)
    for key in ("data", "profile", "credentials", "result"):
        if isinstance(obj.get(key), dict):
            for k, v in _find_key_material(obj[key], depth + 1).items():
                found.setdefault(k, v)
    return found


def try_refresh(account):
    """Attempt ONE credential refresh for an account. Returns the
    merged new profile on success (also persisted), or None. Never
    logs credential material."""
    profile = account.profile
    refresh = profile.get("refreshCredentials")
    if not isinstance(refresh, dict):
        log("upstream 401 but profile has no refreshCredentials; cannot refresh")
        return None
    url = refresh.get("url") or refresh.get("endpoint") or refresh.get("refreshUrl")
    if not url:
        log("refreshCredentials present but no url/endpoint field; "
            "refresh shape unknown — hook point reached, skipping")
        return None
    method = str(refresh.get("method", "POST")).upper()
    headers = {"Content-Type": "application/json"}
    if isinstance(refresh.get("headers"), dict):
        for key, value in refresh["headers"].items():
            headers[str(key)] = str(value)
    body = refresh.get("body")
    if body is None:
        # Fall back to sending the refresh object's own scalar fields
        # (e.g. {"refreshToken": "..."}) as the request body.
        body = {k: v for k, v in refresh.items()
                if k not in ("url", "endpoint", "refreshUrl", "method", "headers")
                and not isinstance(v, (dict, list))}
    payload = json.dumps(body).encode("utf-8") if method != "GET" else None
    try:
        conn, path = open_connection(str(url), CONNECT_TIMEOUT)
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
    except OSError as exc:
        log("refresh request failed (%s)" % type(exc).__name__)
        return None
    if resp.status < 200 or resp.status >= 300:
        log("refresh endpoint answered HTTP %d; giving up" % resp.status)
        return None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        log("refresh endpoint returned non-JSON; giving up")
        return None
    found = _find_key_material(data)
    if "apiKey" not in found:
        log("refresh response contained no new apiKey; giving up")
        return None
    merged = dict(profile)
    merged.update(found)
    STORE.persist_account(account, merged)
    log("account %s: credential refresh succeeded; profile persisted"
        % account.id)
    return merged


# --------------------------------------------------------------------------
# OpenAI -> Anthropic translation
# --------------------------------------------------------------------------

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def openai_to_anthropic(req, upstream_model, profile):
    system_parts = []
    messages = []
    for msg in req.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            text = _text_of(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": _text_of(content),
                }],
            })
            continue
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "text":
                    blocks.append({"type": "text", "text": str(item.get("text", ""))})
                elif itype == "image_url":
                    url = str((item.get("image_url") or {}).get("url", ""))
                    if url.startswith("data:") and ";base64," in url:
                        media, b64 = url[5:].split(";base64,", 1)
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64",
                                       "media_type": media, "data": b64},
                        })
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": args,
            })
        if blocks:
            messages.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": blocks,
            })
    out = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": req.get("max_tokens") or req.get("max_completion_tokens")
        or profile.get("maxApiTokens") or 4096,
        "stream": bool(req.get("stream")),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p"):
        if req.get(key) is not None:
            out[key] = req[key]
    if req.get("stop"):
        stop = req["stop"]
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    tools = req.get("tools")
    if tools:
        converted = []
        for tool in tools:
            fn = tool.get("function") or tool
            converted.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters")
                or {"type": "object", "properties": {}},
            })
        out["tools"] = converted
    return out


_STOP_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def anthropic_to_openai(data, fallback_model):
    text_parts = []
    tool_calls = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = data.get("usage") or {}
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or fallback_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _STOP_MAP.get(data.get("stop_reason"), "stop"),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0),
        },
    }


# --------------------------------------------------------------------------
# OpenAI -> Responses API translation (best-effort)
# --------------------------------------------------------------------------

def openai_to_responses(req, upstream_model):
    instructions = []
    input_items = []
    for msg in req.get("messages", []):
        role = msg.get("role")
        text = _text_of(msg.get("content"))
        if role == "system":
            if text:
                instructions.append(text)
            continue
        input_items.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": text,
        })
    out = {
        "model": upstream_model,
        "input": input_items,
        "stream": bool(req.get("stream")),
    }
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    if req.get("temperature") is not None:
        out["temperature"] = req["temperature"]
    if req.get("max_tokens") or req.get("max_completion_tokens"):
        out["max_output_tokens"] = req.get("max_tokens") \
            or req.get("max_completion_tokens")
    return out


def responses_to_openai(data, fallback_model):
    text_parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text_parts.append(str(block.get("text", "")))
    if not text_parts and isinstance(data.get("output_text"), str):
        text_parts.append(data["output_text"])
    usage = data.get("usage") or {}
    status = data.get("status")
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or fallback_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(text_parts)},
            "finish_reason": "stop" if status in ("completed", None) else "length",
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# --------------------------------------------------------------------------
# Upstream transport
# --------------------------------------------------------------------------

class UpstreamResponse:
    def __init__(self, status, headers, conn, resp):
        self.status = status
        self.headers = headers          # dict, lower-cased keys
        self.conn = conn
        self.resp = resp

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


def call_upstream(profile, protocol, payload):
    """POST the payload to the profile's provider. Returns UpstreamResponse
    (caller streams/reads and must close it). Raises OSError on transport
    failure."""
    url = join_url(profile["baseURL"], "", protocol)
    headers = base_headers(profile, protocol)
    body = json.dumps(payload).encode("utf-8")
    conn, path = open_connection(url, UPSTREAM_TIMEOUT)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    headers_out = {}
    for key, value in resp.getheaders():
        headers_out[key.lower()] = value
    return UpstreamResponse(resp.status, headers_out, conn, resp)


def call_with_refresh(account, protocol, payload):
    """call_upstream for one account + on 401 attempt one credential
    refresh and retry. Returns (UpstreamResponse, auth_failed): when
    auth_failed is True the account's credential was rejected even
    after refreshing and the caller should fail over / mark it dead;
    the response is then already drained and closed."""
    profile = account.profile
    result = call_upstream(profile, protocol, payload)
    if result.status != 401:
        return result, False
    # Drain + close before retrying so the connection is not leaked.
    try:
        result.resp.read()
    except OSError:
        pass
    result.close()
    log("account %s: upstream answered 401; attempting credential "
        "refresh" % account.id)
    refreshed = try_refresh(account)
    if refreshed is not None:
        retry = call_upstream(refreshed, protocol, payload)
        if retry.status != 401:
            return retry, False
        try:
            retry.resp.read()
        except OSError:
            pass
        retry.close()
        return None, True
    # Refresh impossible/failed: re-issue once so the retry verdict is
    # authoritative (a transient 401 should not kill the account if
    # the second call succeeds).
    retry = call_upstream(profile, protocol, payload)
    if retry.status != 401:
        return retry, False
    try:
        retry.resp.read()
    except OSError:
        pass
    retry.close()
    return None, True


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "freemilk-bridge/" + VERSION

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": {
            "message": message,
            "type": "freemilk_bridge_error",
            "code": status,
        }})

    def _begin_chunked(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _write_chunk(self, data):
        if not data:
            return
        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _authorized(self, pool):
        """A request is authorized when it presents the master bridge
        key, OR any account's bridgeKey, OR when no key material is
        configured at all (open localhost mode, legacy behaviour)."""
        presented = self._bearer()
        if BRIDGE_KEY and presented == BRIDGE_KEY:
            return True
        if pool is not None and presented \
                and pool.by_bridge_key(presented) is not None:
            return True
        if not BRIDGE_KEY and not (pool is not None
                                   and pool.has_bridge_keys()):
            return True
        return False

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        raw = self.rfile.read(int(length))
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            pool = STORE.get()
            self._send_json(200, {
                "ok": True,
                "service": "freemilk-bridge",
                "version": VERSION,
                "configured": bool(pool and pool.healthy()),
                "accounts": len(pool.accounts) if pool else 0,
                "accountsHealthy": len(pool.healthy()) if pool else 0,
            })
            return
        if path in ("/v1/models", "/models"):
            pool = STORE.get()
            if not self._authorized(pool):
                self._send_error_json(401, "invalid bridge key")
                return
            now = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": alias,
                    "object": "model",
                    "created": now,
                    "owned_by": "freemilk-bridge",
                } for alias in (pool.aliases() if pool
                                else [DEFAULT_ALIAS])],
            })
            return
        if path == "/":
            self._send_json(200, {
                "service": "freemilk-bridge",
                "version": VERSION,
                "endpoints": ["/health", "/v1/models", "/v1/chat/completions"],
            })
            return
        self._send_error_json(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error_json(404, "not found")
            return
        pool = STORE.get()
        if not self._authorized(pool):
            self._send_error_json(401, "invalid bridge key")
            return
        if pool is None:
            self._send_json(503, {"error": "freemilk-bridge not configured"})
            return
        try:
            req = self._read_json_body()
        except (ValueError, OSError):
            self._send_error_json(400, "request body is not valid JSON")
            return
        if not _semaphore.acquire(timeout=180):
            self._send_error_json(503, "freemilk-bridge busy")
            return
        started = time.time()
        try:
            self._dispatch_chat(pool, req)
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-response")
        except OSError as exc:
            log("upstream transport error (%s)" % type(exc).__name__)
            try:
                self._send_error_json(502, "upstream unreachable")
            except OSError:
                pass
        finally:
            _semaphore.release()
            log("chat done in %.2fs" % (time.time() - started))

    # -- chat handling ------------------------------------------------------
    def _dispatch_chat(self, pool, req):
        """Route the request to one account: the one whose bridgeKey
        was presented, else healthy accounts least-recently-used.
        Auth-dead accounts are marked dead and failed over."""
        presented = self._bearer()
        pinned = pool.by_bridge_key(presented) if presented else None
        if pinned is not None:
            candidates = [pinned] if (pinned.usable() and not pinned.dead) \
                else []
        else:
            candidates = pool.lru()
        if not candidates:
            self._send_error_json(
                503, "freemilk-bridge: no healthy account available")
            return
        attempted = 0
        for account in candidates:
            if account.dead:
                continue
            if not account.semaphore.acquire(timeout=10):
                continue
            attempted += 1
            account.last_used = time.time()
            try:
                outcome = self._serve_account(account, req)
            finally:
                account.semaphore.release()
            if outcome == "served":
                return
            if outcome == "auth_failed":
                STORE.mark_dead(account, "upstream 401 after refresh")
                continue
            # transport_error: try the next account
        if attempted == 0:
            self._send_error_json(
                503, "freemilk-bridge busy (all accounts busy)")
        else:
            self._send_error_json(
                502, "freemilk-bridge: every account failed "
                     "(authentication or transport)")

    def _serve_account(self, account, req):
        """Run one chat against one account. Returns "served",
        "auth_failed" or "transport_error"."""
        profile = account.profile
        protocol = str(profile.get("protocol", "openai")).lower()
        requested = req.get("model") or DEFAULT_ALIAS
        upstream_model = resolve_model(profile, requested)
        stream = bool(req.get("stream"))

        if protocol == "anthropic":
            payload = openai_to_anthropic(req, upstream_model, profile)
        elif protocol == "responses":
            payload = openai_to_responses(req, upstream_model)
        else:
            protocol = "openai"
            payload = dict(req)
            payload["model"] = upstream_model

        try:
            result, auth_failed = call_with_refresh(account, protocol,
                                                    payload)
        except OSError:
            return "transport_error"
        if auth_failed:
            return "auth_failed"
        try:
            if protocol == "openai":
                self._passthrough(result, stream)
            elif protocol == "anthropic":
                if stream and result.status == 200:
                    self._anthropic_stream(result, requested)
                else:
                    self._anthropic_buffered(result, requested)
            else:  # responses
                if stream and result.status == 200:
                    self._responses_stream(result, requested)
                else:
                    self._responses_buffered(result, requested)
        finally:
            result.close()
        return "served"

    def _passthrough(self, result, stream):
        content_type = result.headers.get(
            "content-type", "application/json")
        if stream and result.status == 200:
            self._begin_chunked(200, content_type)
            while True:
                chunk = result.resp.read1(8192) \
                    if hasattr(result.resp, "read1") else result.resp.read(8192)
                if not chunk:
                    break
                self._write_chunk(chunk)
            self._end_chunked()
            return
        raw = result.resp.read()
        self.send_response(result.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # -- Anthropic ----------------------------------------------------------
    def _anthropic_buffered(self, result, requested):
        raw = result.resp.read()
        if result.status != 200:
            self.send_response(result.status)
            self.send_header("Content-Type",
                             result.headers.get("content-type",
                                                "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        try:
            data = json.loads(raw.decode("utf-8"))
            converted = anthropic_to_openai(data, requested)
        except ValueError:
            converted = None
        if converted is None:
            self._send_error_json(502, "upstream returned non-JSON")
            return
        self._send_json(200, converted)

    def _anthropic_stream(self, result, requested):
        self._begin_chunked(200, "text/event-stream")
        state = {"id": "", "model": requested, "finish": None,
                 "tool_index": -1}

        def emit(obj):
            self._write_chunk(b"data: " + json.dumps(obj).encode("utf-8")
                              + b"\n\n")

        def chunk(delta, finish=None):
            return {
                "id": state["id"],
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": state["model"],
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            }

        buffer = b""
        while True:
            line = result.resp.readline()
            if not line:
                break
            buffer += line
            if not line.rstrip(b"\r\n"):
                event, buffer = buffer, b""
                self._anthropic_event(event, state, emit, chunk)
        self._write_chunk(b"data: [DONE]\n\n")
        self._end_chunked()

    def _anthropic_event(self, raw_event, state, emit, chunk):
        data_lines = []
        for line in raw_event.decode("utf-8", "replace").splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return
        try:
            data = json.loads("".join(data_lines))
        except ValueError:
            return
        etype = data.get("type")
        if etype == "message_start":
            msg = data.get("message") or {}
            state["id"] = msg.get("id", state["id"])
            state["model"] = msg.get("model", state["model"])
            emit(chunk({"role": "assistant", "content": ""}))
        elif etype == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                state["tool_index"] += 1
                emit(chunk({"tool_calls": [{
                    "index": state["tool_index"],
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": ""},
                }]}))
        elif etype == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                emit(chunk({"content": delta["text"]}))
            elif delta.get("type") == "input_json_delta":
                emit(chunk({"tool_calls": [{
                    "index": max(state["tool_index"], 0),
                    "function": {
                        "arguments": delta.get("partial_json", "")},
                }]}))
        elif etype == "message_delta":
            delta = data.get("delta") or {}
            if delta.get("stop_reason"):
                emit(chunk({}, _STOP_MAP.get(delta["stop_reason"], "stop")))

    # -- Responses API --------------------------------------------------------
    def _responses_buffered(self, result, requested):
        raw = result.resp.read()
        if result.status != 200:
            self.send_response(result.status)
            self.send_header("Content-Type",
                             result.headers.get("content-type",
                                                "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        try:
            data = json.loads(raw.decode("utf-8"))
            converted = responses_to_openai(data, requested)
        except ValueError:
            converted = None
        if converted is None:
            self._send_error_json(502, "upstream returned non-JSON")
            return
        self._send_json(200, converted)

    def _responses_stream(self, result, requested):
        self._begin_chunked(200, "text/event-stream")
        state = {"id": "", "model": requested, "sent_role": False}

        def emit(obj):
            self._write_chunk(b"data: " + json.dumps(obj).encode("utf-8")
                              + b"\n\n")

        def chunk(delta, finish=None):
            return {
                "id": state["id"],
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": state["model"],
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            }

        buffer = b""
        while True:
            line = result.resp.readline()
            if not line:
                break
            buffer += line
            if line.rstrip(b"\r\n"):
                continue
            event, buffer = buffer, b""
            data_lines = [ln[5:].strip() for ln in
                          event.decode("utf-8", "replace").splitlines()
                          if ln.startswith("data:")]
            if not data_lines:
                continue
            try:
                data = json.loads("".join(data_lines))
            except ValueError:
                continue
            etype = data.get("type")
            if etype == "response.created":
                resp_obj = data.get("response") or {}
                state["id"] = resp_obj.get("id", state["id"])
                state["model"] = resp_obj.get("model", state["model"])
            elif etype == "response.output_text.delta":
                if not state["sent_role"]:
                    state["sent_role"] = True
                    emit(chunk({"role": "assistant", "content": ""}))
                if data.get("delta"):
                    emit(chunk({"content": data["delta"]}))
            elif etype in ("response.completed", "response.incomplete"):
                emit(chunk({}, "stop" if etype == "response.completed"
                           else "length"))
        self._write_chunk(b"data: [DONE]\n\n")
        self._end_chunked()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    profile = STORE.get()
    log("listening on %s:%d (configured=%s, credentials=%s)" % (
        HOST, PORT, profile is not None, CREDENTIALS_PATH))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
