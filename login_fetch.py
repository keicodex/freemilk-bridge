#!/usr/bin/env python3
"""login_fetch.py — pure-login helper for freemilk-bridge (no iOS device).

FreeMilk (Milq AI) sign-in is Firebase Authentication on project
``milq-mobile``. After sign-in, the app fetches a *hosted model catalog*
from api.milq.ai; the catalog's per-model endpoints carry the provider
profile ({baseURL, apiKey, refreshCredentials, ...}) that bridge.py
replays. The exact issuance path is assembled dynamically in the app and
is not recoverable statically, so this helper tries an ordered candidate
list with several auth variants and keeps the first response that
contains a profile-shaped object. One live pass with real tokens
resolves which candidate is real; the winner is remembered in the
credentials file.

credentials.json layout (single file, shared with bridge.py). Two
shapes, freely combinable — bridge.py serves every profile it finds:

  Legacy single account:
  {
    ... provider profile fields (baseURL, apiKey, protocol, ...) ...,
    "bridgeKey": "fmb_default_...",     (issued by sync-9router)
    "milq": { ... login material for this account ... }
  }

  Account pool:
  {
    "accounts": [
      {
        "id": "acct-1a2b3c4d",
        "bridgeKey": "fmb_acct-1a2b3c4d_...",
        ... provider profile fields ...,
        "milq": { ... login material for this account ... }
      }, ...
    ]
  }

  A "milq" object holds:
      "firebaseApiKey":      public Firebase client key of the project,
      "firebaseIdToken":     Firebase ID token (1 h lifetime),
      "firebaseRefreshToken":Firebase refresh token (long-lived),
      "firebaseIdTokenExpiry": epoch seconds,
      "sessionCookie":       portal session cookie header value
                             (from signing in at account.milq.ai),
      "milqAccessToken":     Milq access token, if an exchange issued one,
      "milqRefreshToken":    Milq refresh token, if issued,
      "appCheckToken":       X-Firebase-AppCheck value, if ever captured,
      "profileEndpoints":    optional candidate-list override,
      "profileEndpointUsed": set by `fetch` once a candidate answers

Commands:
  login        guided one-shot login: serves the sign-in page, takes
               ONE paste of the token JSON on stdin, stores it, runs
               fetch, and prints a one-line verdict. Start here.
  bulk-add     add MANY accounts in one go: reads token JSONs from a
               file argument (or stdin) — a JSON array, a single
               object, JSONL, or blank-line-separated pastes — stores
               each as its own pool account and fetches its profile.
  set-tokens   read a JSON object from stdin with any of {idToken,
               refreshToken, sessionCookie, milqAccessToken,
               milqRefreshToken, appCheckToken} and store it (0600) as
               the legacy account. Secrets are never taken from argv
               (ps exposure) and never printed.
  refresh      refresh the legacy account's Firebase ID token.
  fetch        ensure fresh ID tokens, then try the candidate
               issuance endpoints for every account that still lacks a
               profile; merge hits into credentials.json (0600).
  sync-9router detect a local 9Router panel, log in ONCE with the
               console password (PANEL_PASSWORD env or interactive
               prompt — never stored, never logged) and idempotently
               register: the `freemilk` node, one connection per pool
               account (apiKey = the account's bridgeKey), and the
               opus-5.5 custom model. Re-running adds new accounts and
               disables connections of removed/dead accounts. It only
               ever touches its own node and that node's connections.
  status       print what is stored (names + expiry only).

Environment:
  FREEMILK_BRIDGE_CREDENTIALS  credentials path (default: alongside
                               bridge.py)
  FREEMILK_SECURETOKEN_URL     override the Firebase token endpoint
                               (tests only; default is Google's)
  FREEMILK_MILQ_BASE_URL       override https://api.milq.ai (tests only)
  PANEL_BASE_URL               9Router panel base for sync-9router
                               (default http://127.0.0.1:2013 — the
                               router's PORT env default)
  PANEL_PASSWORD               9Router console password for
                               sync-9router (or it prompts; never
                               stored, never logged)
  FREEMILK_BRIDGE_BASE_URL     base URL the panel should call
                               (default http://127.0.0.1:18611/v1)
"""

import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.environ.get(
    "FREEMILK_BRIDGE_CREDENTIALS", os.path.join(_HERE, "credentials.json"))
MILQ_BASE = os.environ.get("FREEMILK_MILQ_BASE_URL",
                           "https://api.milq.ai").rstrip("/")
SECURETOKEN_URL = os.environ.get(
    "FREEMILK_SECURETOKEN_URL",
    "https://securetoken.googleapis.com/v1/token")

# Public client identifiers of the milq-mobile Firebase project, taken
# from the shipped app's GoogleService-Info.plist (they are identifiers,
# not credentials — every installed copy of the app carries them).
DEFAULT_FIREBASE_API_KEY = "AIzaSyCUPyE6B8H4-8pBJs3cH7vQ9sBdqJ2nF8M"
FIREBASE_PROJECT = "milq-mobile"
FIREBASE_AUTH_DOMAIN = "milq-mobile.firebaseapp.com"

# Candidate profile-issuance endpoints on api.milq.ai, ordered by
# likelihood. Evidence: the app binary (MilqMobile2/HostedModels.swift)
# fetches a hosted model catalog with freeModelsEndpoint /
# premiumModelsEndpoint and per-model HostedModelEndpoint objects; the
# account backend serves the cookie-authenticated /account/api/*
# surface. Paths are assembled dynamically in the client, so the live
# login pass resolves the real one from this list.
DEFAULT_CANDIDATES = [
    ("GET", "/account/api/models"),
    ("GET", "/account/api/hosted-models"),
    ("GET", "/account/api/model-catalog"),
    ("GET", "/account/api/catalog"),
    ("GET", "/account/api/free-models"),
    ("GET", "/account/api/premium-models"),
    ("GET", "/api/models"),
    ("GET", "/api/hosted-models"),
    ("GET", "/api/catalog"),
    ("GET", "/api/config"),
    ("GET", "/account/api/config"),
    ("GET", "/account/api/summary"),
    ("POST", "/account/api/session"),
    ("POST", "/api/auth/exchange"),
    ("POST", "/account/api/auth/exchange"),
]

PROFILE_KEYS = ("baseURL", "apiKey", "protocol", "model", "models",
                "sessionId", "sessionAffinity", "extraHeaders",
                "maxApiTokens", "refreshCredentials")


def log(msg):
    sys.stderr.write("login-fetch: %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# credentials file handling
# --------------------------------------------------------------------------

def load_credentials():
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_credentials(data):
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDENTIALS_PATH)


def milq_section(data, create=False):
    section = data.get("milq")
    if not isinstance(section, dict):
        if not create:
            return {}
        section = {}
        data["milq"] = section
    return section


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_request(method, url, headers=None, body=None, timeout=20):
    payload = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except OSError:
        return 0, b""


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# Firebase token refresh (standard securetoken flow)
# --------------------------------------------------------------------------

def refresh_firebase_tokens(section):
    """Refresh the Firebase ID token inside one milq section dict.
    Mutates the section; performs NO file I/O (callers save)."""
    refresh_token = section.get("firebaseRefreshToken")
    api_key = section.get("firebaseApiKey") or DEFAULT_FIREBASE_API_KEY
    if not refresh_token:
        log("no firebaseRefreshToken stored; cannot refresh")
        return False
    url = SECURETOKEN_URL + "?key=" + urllib.parse.quote(api_key)
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, OSError, ValueError) as exc:
        log("firebase refresh failed (%s)" % type(exc).__name__)
        return False
    id_token = result.get("id_token")
    if not id_token:
        log("firebase refresh response had no id_token")
        return False
    section["firebaseIdToken"] = id_token
    if result.get("refresh_token"):
        section["firebaseRefreshToken"] = result["refresh_token"]
    try:
        ttl = int(result.get("expires_in", 3600))
    except (TypeError, ValueError):
        ttl = 3600
    section["firebaseIdTokenExpiry"] = int(time.time()) + ttl
    section["firebaseApiKey"] = api_key
    log("firebase ID token refreshed (expires in %ds)" % ttl)
    return True


def refresh_firebase(data):
    """Legacy-account wrapper: refresh + persist the whole file."""
    section = milq_section(data, create=True)
    if refresh_firebase_tokens(section):
        save_credentials(data)
        return True
    return False


def ensure_fresh(section):
    """Return a usable ID token for one milq section, refreshing in
    memory when stale. No file I/O; may return a stale token or None
    (callers try anyway)."""
    token = section.get("firebaseIdToken")
    expiry = section.get("firebaseIdTokenExpiry") or 0
    if token and expiry - time.time() > 300:
        return token
    if section.get("firebaseRefreshToken"):
        if refresh_firebase_tokens(section):
            return section.get("firebaseIdToken")
    return token


def ensure_fresh_id_token(data):
    section = milq_section(data)
    before = section.get("firebaseIdToken")
    token = ensure_fresh(section)
    if token != before:
        save_credentials(data)
    return token


# --------------------------------------------------------------------------
# Profile extraction
# --------------------------------------------------------------------------

def _is_profile_shaped(obj):
    if not isinstance(obj, dict):
        return False
    keys = {str(k).lower() for k in obj.keys()}
    has_base = "baseurl" in keys
    has_key = "apikey" in keys or "api_key" in keys
    return has_base and has_key


def find_profiles(obj, depth=0, opus_nearby=False):
    """Collect (profile_dict, opus_nearby) pairs from a JSON tree."""
    found = []
    if depth > 6:
        return found
    if isinstance(obj, dict):
        nearby = opus_nearby or any(
            isinstance(v, str) and "opus" in v.lower()
            for v in obj.values())
        if _is_profile_shaped(obj):
            found.append((obj, nearby))
        for value in obj.values():
            found.extend(find_profiles(value, depth + 1, nearby))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_profiles(item, depth + 1, opus_nearby))
    return found


def normalize_profile(raw):
    profile = {}
    for key in PROFILE_KEYS:
        if key in raw:
            profile[key] = raw[key]
    if "baseURL" not in profile and "baseUrl" in raw:
        profile["baseURL"] = raw["baseUrl"]
    if "apiKey" not in profile and "api_key" in raw:
        profile["apiKey"] = raw["api_key"]
    return profile


# --------------------------------------------------------------------------
# fetch: try candidates x auth variants
# --------------------------------------------------------------------------

def auth_variants_for(section):
    variants = []
    cookie = section.get("sessionCookie")
    id_token = section.get("firebaseIdToken")
    milq_token = section.get("milqAccessToken")
    app_check = section.get("appCheckToken")

    def with_app_check(headers):
        if app_check:
            headers = dict(headers)
            headers["X-Firebase-AppCheck"] = app_check
        return headers

    if cookie:
        variants.append(("cookie", with_app_check({"Cookie": cookie})))
    if id_token:
        variants.append(("firebase-bearer",
                         with_app_check(
                             {"Authorization": "Bearer " + id_token})))
    if cookie and id_token:
        variants.append(("cookie+firebase-bearer", with_app_check({
            "Cookie": cookie,
            "Authorization": "Bearer " + id_token,
        })))
    if milq_token:
        variants.append(("milq-bearer",
                         with_app_check(
                             {"Authorization": "Bearer " + milq_token})))
    if not variants:
        variants.append(("none", with_app_check({})))
    return variants


def auth_variants(data):
    return auth_variants_for(milq_section(data))


def _candidates_for(section):
    override = section.get("profileEndpoints")
    candidates = []
    if isinstance(override, list) and override:
        for item in override:
            if isinstance(item, str):
                candidates.append(("GET", item))
            elif isinstance(item, dict) and item.get("path"):
                candidates.append((str(item.get("method", "GET")).upper(),
                                   str(item["path"])))
    if not candidates:
        candidates = list(DEFAULT_CANDIDATES)
    used = section.get("profileEndpointUsed")
    if used:
        candidates.sort(key=lambda c: 0 if c[1] == used else 1)
    return candidates


def fetch_profile_for(owner):
    """Try the candidate issuance endpoints for ONE account (the
    legacy top-level dict, or one pool entry — anything holding a
    "milq" section). On a hit, records profileEndpointUsed in the
    section and returns (profile, path, variant_name, attempts);
    otherwise (None, None, None, attempts). No file I/O: the caller
    merges the profile into the owner dict and saves."""
    section = milq_section(owner, create=True)
    ensure_fresh(section)
    candidates = _candidates_for(section)
    variants = auth_variants_for(section)
    attempts = []
    for method, path in candidates:
        url = MILQ_BASE + path
        for variant_name, headers in variants:
            body = None
            if method == "POST":
                body = {}
                token = section.get("firebaseIdToken")
                if token:
                    body = {"idToken": token}
            status, raw = http_request(method, url, headers=headers,
                                       body=body)
            label = "%s %s [%s] -> HTTP %d" % (method, path, variant_name,
                                               status)
            if status == 200:
                parsed = parse_json(raw)
                profiles = find_profiles(parsed) if parsed is not None \
                    else []
                if profiles:
                    profiles.sort(key=lambda p: 0 if p[1] else 1)
                    profile = normalize_profile(profiles[0][0])
                    if profile.get("baseURL") and profile.get("apiKey"):
                        section["profileEndpointUsed"] = path
                        log("HIT: %s yielded a provider profile" % label)
                        return profile, path, variant_name, attempts
                    label += " (profile-shaped but incomplete)"
                else:
                    label += " (200 but no profile in body)"
            attempts.append(label)
            log("miss: %s" % label)
    return None, None, None, attempts


def _section_has_material(section):
    return any(section.get(name) for name in (
        "firebaseIdToken", "firebaseRefreshToken", "sessionCookie",
        "milqAccessToken"))


def cmd_fetch(data):
    """Fetch profiles for every account that has login material but
    no profile yet: the legacy top-level account and each pool entry.
    Returns 0 when at least one profile is (now) present."""
    targets = []
    if _section_has_material(milq_section(data)):
        targets.append(("legacy account", data))
    entries = data.get("accounts")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) \
                    and _section_has_material(milq_section(entry)) \
                    and not (entry.get("baseURL") and entry.get("apiKey")):
                targets.append(("account %s" % entry.get("id", "?"), entry))
    if not targets:
        if (data.get("baseURL") and data.get("apiKey")) or any(
                isinstance(e, dict) and e.get("baseURL") and e.get("apiKey")
                for e in (entries or [])):
            print("all accounts already carry a provider profile")
            return 0
        print("no login material stored yet — run `login` or "
              "`bulk-add` first")
        return 2
    hits = 0
    all_attempts = []
    for label, owner in targets:
        profile, path, variant, attempts = fetch_profile_for(owner)
        all_attempts.extend("%s: %s" % (label, a) for a in attempts)
        if profile:
            owner.update(profile)
            save_credentials(data)
            hits += 1
            print("%s: profile fetched via %s (auth: %s)"
                  % (label, path, variant))
        else:
            print("%s: no candidate yielded a provider profile" % label)
    if hits:
        return 0
    print("attempts:")
    for line in all_attempts:
        print("  " + line)
    return 2


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

TOKEN_FIELDS = {
    "idToken": "firebaseIdToken",
    "refreshToken": "firebaseRefreshToken",
    "sessionCookie": "sessionCookie",
    "milqAccessToken": "milqAccessToken",
    "milqRefreshToken": "milqRefreshToken",
    "appCheckToken": "appCheckToken",
    "firebaseApiKey": "firebaseApiKey",
}


def _store_into_section(owner, incoming):
    """Store recognised token fields from a parsed JSON object into
    the milq section of `owner` (any dict). Returns the list of stored
    field names (values are never printed). No file I/O."""
    section = milq_section(owner, create=True)
    stored = []
    for given, stored_name in TOKEN_FIELDS.items():
        value = incoming.get(given)
        if isinstance(value, str) and value:
            section[stored_name] = value
            stored.append(stored_name)
    if incoming.get("idToken") and not incoming.get("idTokenExpiry"):
        section["firebaseIdTokenExpiry"] = int(time.time()) + 3600
    return stored


def _store_tokens(data, incoming):
    stored = _store_into_section(data, incoming)
    if stored:
        save_credentials(data)
    return stored


# --------------------------------------------------------------------------
# Account pool helpers
# --------------------------------------------------------------------------

def derive_account_id(section):
    """Stable account id from the account's own login material, so
    re-adding the same account updates its entry instead of
    duplicating it."""
    seed = (section.get("firebaseRefreshToken")
            or section.get("sessionCookie")
            or section.get("firebaseIdToken")
            or section.get("milqAccessToken") or "")
    return "acct-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def new_bridge_key(account_id):
    return "fmb_%s_%s" % (account_id, secrets.token_hex(12))


def add_account(data, incoming):
    """Create or update one pool account from a token JSON. Returns
    (entry, stored_field_names); entry is None when the input carried
    no recognised fields. New accounts get a fresh bridgeKey; existing
    accounts (same derived id) keep theirs."""
    holder = {}
    stored = _store_into_section(holder, incoming)
    if not stored:
        return None, []
    section = holder["milq"]
    account_id = derive_account_id(section)
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
        data["accounts"] = accounts
    for entry in accounts:
        if isinstance(entry, dict) and entry.get("id") == account_id:
            milq_section(entry, create=True).update(section)
            return entry, stored
    entry = {"id": account_id,
             "bridgeKey": new_bridge_key(account_id),
             "milq": section}
    accounts.append(entry)
    return entry, stored


def parse_batch(text):
    """Parse bulk input into a list of token-JSON dicts. Accepts a
    JSON array, a single JSON object, JSONL, or blank-line-separated
    pastes (each chunk may itself be a pretty-printed object)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except ValueError:
        pass
    items = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                items.append(parsed)
                continue
            if isinstance(parsed, list):
                items.extend(x for x in parsed if isinstance(x, dict))
                continue
        except ValueError:
            pass
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
    return items


def cmd_bulk_add(data, source):
    """bulk-add [file|-]: add many accounts in one go, fetching each
    profile immediately. Per-account verdicts; exit 0 when at least
    one account ends up with a profile."""
    if source and source != "-":
        try:
            with open(source, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print("bulk-add: cannot read %s (%s)"
                  % (source, type(exc).__name__))
            return 2
    else:
        print("paste the token JSONs (array, JSONL, or blank-line "
              "separated), then press Ctrl-D:")
        sys.stdout.flush()
        text = sys.stdin.read()
    items = parse_batch(text)
    if not items:
        print("bulk-add: no token JSON objects found in the input")
        return 2
    ready = 0
    for incoming in items:
        entry, stored = add_account(data, incoming)
        if entry is None:
            print("account ?: FAILED (no recognised token fields)")
            continue
        if entry.get("baseURL") and entry.get("apiKey"):
            print("account %s: already has a profile (material updated)"
                  % entry["id"])
            ready += 1
            save_credentials(data)
            continue
        profile, path, variant, _attempts = fetch_profile_for(entry)
        if profile:
            entry.update(profile)
            ready += 1
            print("account %s: OK (profile via %s, auth %s)"
                  % (entry["id"], path, variant))
        else:
            print("account %s: FAILED (tokens stored, but no candidate "
                  "endpoint yielded a profile — retry with `fetch`)"
                  % entry["id"])
        save_credentials(data)
    print("bulk-add: %d/%d account(s) ready" % (ready, len(items)))
    if ready:
        print("next: `login_fetch.py sync-9router` registers every "
              "ready account as its own 9Router connection")
    return 0 if ready else 2


# --------------------------------------------------------------------------
# sync-9router: detect a local 9Router panel and register the bridge
#
# Panel contract (verified against the 9Router source): console auth is
# POST /api/auth/login {"password": ...} -> session cookie. Nodes:
# GET/POST /api/provider-nodes, PUT /api/provider-nodes/<id>; creating
# an openai-compatible node takes {name, prefix, apiType: "chat",
# baseUrl, type: "openai-compatible"} and the server derives each
# connection's providerSpecificData from it. Connections: GET/POST
# /api/providers (list envelope {connections: [...]}, apiKey STRIPPED
# on read — match by name), PUT /api/providers/<id> accepts partial
# updates including {"isActive": false}. Custom models: GET/POST
# /api/models/custom (envelope {models: [...]}). The executor calls
# {baseUrl}/chat/completions with "Authorization: Bearer <connection
# apiKey>" and the model string WITHOUT the node prefix.
# --------------------------------------------------------------------------

PANEL_BASE_DEFAULT = "http://127.0.0.1:2013"
BRIDGE_BASE_DEFAULT = "http://127.0.0.1:18611/v1"
NODE_NAME = "FreeMilk"
NODE_PREFIX = "freemilk"
CUSTOM_MODEL_ID = "opus-5.5"


class Panel:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookie = None

    def request(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=payload,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                cookies = resp.headers.get_all("Set-Cookie") or []
                return resp.status, parse_json(raw), cookies
        except urllib.error.HTTPError as err:
            raw = err.read()
            cookies = err.headers.get_all("Set-Cookie") \
                if err.headers else []
            return err.code, parse_json(raw), cookies or []
        except OSError:
            return 0, None, []

    def login(self, password):
        status, _data, cookies = self.request(
            "POST", "/api/auth/login", {"password": password})
        if status != 200 or not cookies:
            return False
        pairs = [c.split(";")[0] for c in cookies if "=" in c]
        if not pairs:
            return False
        self.cookie = "; ".join(pairs)
        return True


def panel_accounts(data):
    """(account_id, bridge_key) pairs for every profiled, non-dead
    account. Issues (and persists) a bridgeKey for any account that
    lacks one."""
    out = []
    changed = False

    def consider(owner, account_id):
        nonlocal changed
        if not (owner.get("baseURL") and owner.get("apiKey")):
            return
        if not owner.get("bridgeKey"):
            owner["bridgeKey"] = new_bridge_key(account_id)
            changed = True
        if not owner.get("dead"):
            out.append((account_id, owner["bridgeKey"]))

    consider(data, str(data.get("accountId") or "default"))
    entries = data.get("accounts")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                consider(entry, str(entry.get("id") or "acct"))
    if changed:
        save_credentials(data)
    return out


def cmd_sync_9router(data):
    panel_base = os.environ.get("PANEL_BASE_URL", PANEL_BASE_DEFAULT)
    bridge_base = os.environ.get("FREEMILK_BRIDGE_BASE_URL",
                                 BRIDGE_BASE_DEFAULT)
    password = os.environ.get("PANEL_PASSWORD")
    if not password:
        import getpass
        try:
            password = getpass.getpass("9Router console password: ")
        except (EOFError, KeyboardInterrupt):
            password = ""
    if not password:
        print("sync-9router: no console password given (set "
              "PANEL_PASSWORD or type it at the prompt)")
        return 2
    panel = Panel(panel_base)
    if not panel.login(password):
        print("sync-9router: panel login failed at %s — wrong "
              "password, or no 9Router listening there (set "
              "PANEL_BASE_URL if it lives elsewhere)" % panel_base)
        return 2
    password = None  # never kept around, never logged

    accounts = panel_accounts(data)
    actions = []

    status, payload, _c = panel.request("GET", "/api/provider-nodes")
    if status != 200 or not isinstance(payload, dict):
        print("sync-9router: cannot list panel nodes (HTTP %d) — "
              "is this really a 9Router panel?" % status)
        return 2
    nodes = payload.get("nodes") or []
    node = next((n for n in nodes
                 if n.get("prefix") == NODE_PREFIX
                 and n.get("type") == "openai-compatible"), None)
    if node is None:
        status, created, _c = panel.request("POST", "/api/provider-nodes", {
            "name": NODE_NAME, "prefix": NODE_PREFIX, "apiType": "chat",
            "baseUrl": bridge_base, "type": "openai-compatible"})
        node = created.get("node") if isinstance(created, dict) else None
        if status not in (200, 201) or not node:
            print("sync-9router: node creation failed (HTTP %d)" % status)
            return 2
        actions.append("created node %s (prefix %s -> %s)"
                       % (NODE_NAME, NODE_PREFIX, bridge_base))
    elif node.get("baseUrl") != bridge_base:
        panel.request("PUT", "/api/provider-nodes/" + str(node["id"]), {
            "name": node.get("name") or NODE_NAME, "prefix": NODE_PREFIX,
            "apiType": "chat", "baseUrl": bridge_base})
        actions.append("updated node baseUrl -> %s" % bridge_base)
    node_id = node["id"]

    status, payload, _c = panel.request("GET", "/api/providers")
    conns = (payload or {}).get("connections") or []
    ours = [c for c in conns if c.get("provider") == node_id]
    by_name = {c.get("name"): c for c in ours}
    desired = {"freemilk-%s" % aid: key for aid, key in accounts}
    for name, key in desired.items():
        if name not in by_name:
            cstatus, _p, _c = panel.request("POST", "/api/providers", {
                "provider": node_id, "apiKey": key, "name": name})
            if cstatus in (200, 201):
                actions.append("created connection %s" % name)
            else:
                actions.append("FAILED to create connection %s "
                               "(HTTP %d)" % (name, cstatus))
    for conn in ours:
        if conn.get("name") not in desired and conn.get("isActive"):
            panel.request("PUT", "/api/providers/" + str(conn["id"]),
                          {"isActive": False})
            actions.append("disabled stale connection %s"
                           % conn.get("name"))

    status, payload, _c = panel.request("GET", "/api/models/custom")
    customs = (payload or {}).get("models") or []
    if not any(c.get("providerAlias") == NODE_PREFIX
               and c.get("id") == CUSTOM_MODEL_ID for c in customs):
        panel.request("POST", "/api/models/custom", {
            "providerAlias": NODE_PREFIX, "id": CUSTOM_MODEL_ID,
            "type": "llm", "name": "Opus 5.5 (FreeMilk)"})
        actions.append("registered custom model freemilk/%s"
                       % CUSTOM_MODEL_ID)

    for line in actions:
        print("sync: " + line)
    if not actions:
        print("sync: 9Router already up to date")
    print("sync-9router: OK — node '%s', %d account connection(s), "
          "model freemilk/%s" % (NODE_PREFIX, len(desired),
                                 CUSTOM_MODEL_ID))
    return 0


def cmd_set_tokens(data):
    raw = sys.stdin.read()
    try:
        incoming = json.loads(raw) if raw.strip() else {}
    except ValueError:
        print("stdin is not valid JSON")
        return 2
    if not isinstance(incoming, dict):
        print("expected a JSON object on stdin")
        return 2
    stored = _store_tokens(data, incoming)
    if not stored:
        print("nothing stored (no recognised fields in input)")
        return 2
    print("stored: %s" % ", ".join(sorted(stored)))
    return 0


def cmd_status(data):
    section = milq_section(data)
    configured = bool(data.get("baseURL") and data.get("apiKey"))
    print("bridge configured (legacy account): %s" % configured)
    print("credentials file: %s" % CREDENTIALS_PATH)
    present = [name for name in (
        "firebaseIdToken", "firebaseRefreshToken", "sessionCookie",
        "milqAccessToken", "milqRefreshToken", "appCheckToken")
        if section.get(name)]
    print("stored auth material: %s" % (", ".join(present) or "none"))
    expiry = section.get("firebaseIdTokenExpiry")
    if expiry:
        print("firebase ID token expiry: %s (%ds from now)" % (
            time.strftime("%Y-%m-%dT%H:%M:%S%z",
                          time.localtime(expiry)),
            int(expiry - time.time())))
    if section.get("profileEndpointUsed"):
        print("profile endpoint resolved: %s"
              % section["profileEndpointUsed"])
    entries = data.get("accounts")
    if isinstance(entries, list) and entries:
        print("pool accounts: %d" % len(entries))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state = "dead" if entry.get("dead") else (
                "ready" if entry.get("baseURL") and entry.get("apiKey")
                else "no profile yet")
            print("  %s: %s, bridgeKey %s" % (
                entry.get("id", "?"), state,
                "issued" if entry.get("bridgeKey") else "not issued"))
    return 0


def _start_auth_server():
    """Start the sign-in page server on 127.0.0.1:18620 in a thread.
    Returns the server, or None when auth.html is missing / the port
    is unavailable (login can still proceed — the page is a helper,
    the tokens are what matter)."""
    import http.server
    import threading
    page = os.path.join(_HERE, "auth.html")
    if not os.path.exists(page):
        return None
    handler = http.server.SimpleHTTPRequestHandler
    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 18620),
            lambda *a, **kw: handler(*a, directory=_HERE, **kw))
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cmd_serve_auth():
    if not os.path.exists(os.path.join(_HERE, "auth.html")):
        print("auth.html not found next to login_fetch.py")
        return 2
    server = _start_auth_server()
    if server is None:
        print("could not bind 127.0.0.1:18620 (already in use?)")
        return 2
    print("serving the sign-in page at http://127.0.0.1:18620/auth.html "
          "(localhost is an authorised Firebase origin; tunnel or open "
          "locally, complete Google sign-in, then hand the two tokens "
          "to set-tokens)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_login(data):
    """Guided one-shot login: serve the sign-in page, accept ONE paste
    of the token JSON, store it, fetch the provider profile, and print
    a one-line verdict. Tokens come from stdin only — never argv,
    never logs."""
    server = _start_auth_server()
    print("Step 1: open http://127.0.0.1:18620/auth.html in a browser "
          "on this machine (or tunnel port 18620) and sign in with "
          "Google. The page shows a JSON object with your ID token "
          "and refresh token.")
    print("Step 2: paste that JSON here, then press Ctrl-D:")
    sys.stdout.flush()
    raw = sys.stdin.read()
    if server is not None:
        server.shutdown()
    try:
        incoming = json.loads(raw) if raw.strip() else {}
    except ValueError:
        incoming = {}
    if not isinstance(incoming, dict):
        incoming = {}
    stored = _store_tokens(data, incoming)
    if not stored:
        print("LOGIN FAILED — the paste contained no recognised token "
              "fields (expected idToken + refreshToken, or "
              "sessionCookie). Nothing was stored.")
        return 2
    print("tokens stored: %s" % ", ".join(sorted(stored)))
    print("Step 3: fetching the provider profile from FreeMilk...")
    rc = cmd_fetch(load_credentials())
    if rc == 0:
        print("LOGIN OK — provider profile fetched; the bridge is "
              "configured. Next: `login_fetch.py sync-9router` "
              "registers it in 9Router automatically.")
        print("Tip: adding many accounts at once? Use "
              "`login_fetch.py bulk-add <file>` with one token JSON "
              "per account.")
        return 0
    print("LOGIN FAILED — tokens are stored, but no candidate "
          "endpoint yielded a provider profile (see attempts above). "
          "Re-run `login_fetch.py fetch` to retry.")
    return 2


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    data = load_credentials()
    if command == "set-tokens":
        return cmd_set_tokens(data)
    if command == "bulk-add":
        return cmd_bulk_add(data, argv[2] if len(argv) > 2 else None)
    if command == "sync-9router":
        return cmd_sync_9router(data)
    if command == "refresh":
        return 0 if refresh_firebase(data) else 2
    if command == "fetch":
        return cmd_fetch(data)
    if command == "status":
        return cmd_status(data)
    if command == "serve-auth":
        return cmd_serve_auth()
    if command == "login":
        return cmd_login(data)
    print("unknown command: %s" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
