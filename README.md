<p align="center">
  <img src="docs/assets/logo.svg" alt="freemilk-bridge logo" width="128" height="128">
</p>

<h1 align="center">freemilk-bridge</h1>

<p align="center">
  <strong>Turn FreeMilk (Milq AI) accounts into a pooled, OpenAI-compatible upstream for 9Router.</strong><br>
  One bridge, many accounts — each account becomes its own 9Router connection.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Python-3.8%2B-blue.svg" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/dependencies-0%20%C2%B7%20stdlib%20only-brightgreen.svg" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/9Router-supported-8a2be2.svg" alt="9Router supported">
  <img src="https://img.shields.io/badge/accounts-multi--account%20pool-orange.svg" alt="Multi-account pool">
  <img src="https://img.shields.io/badge/version-0.3.0-informational.svg" alt="Version 0.3.0">
</p>

---

FreeMilk's app receives a **provider profile** (`baseURL`, `apiKey`, …) from
`api.milq.ai` after sign-in and talks directly to that provider.
freemilk-bridge holds those profiles for **one or many accounts** and
re-exposes them on localhost as a plain OpenAI-compatible endpoint — so a
router such as **9Router** can consume FreeMilk's hosted models (including
the "Thirsty Model" **Opus 5.5**) like any other provider, with the
router's native connection rotation spreading load across your accounts.

## ⚠️ Reality check — read before installing

FreeMilk accounts are **born in the app**, and that decides how this
bridge is fed:

- **Enrolment is device-born.** A Milq account record is created at
  the first sign-in *inside the real FreeMilk iOS app*. That sign-in
  exchange carries a **DeviceCheck token** minted by genuine Apple
  hardware, and Milq's backend rejects every non-app client on its
  app API namespaces with
  `403 "This client is not supported. Please use the Milq iOS app."`
  Firebase sign-in replayed from a server is blocked too — the app's
  Firebase API key is app-restricted.
- **Web sign-in alone cannot enrol an account.** A Google account
  that has never signed in inside the app cannot be enrolled from any
  web or server path. The Firebase web sign-in page in this repo —
  even opened at the correct `localhost` hostname — produces Firebase
  tokens only; it does not perform the enrolment exchange and does
  not by itself fetch a provider profile, because profile issuance
  lives behind the same client gate.
- **What the bridge actually consumes** is session material from an
  account that is *already enrolled*: the **provider profile + Milq
  tokens** from one genuine app session (for example, an MITM capture
  of your own session on your own iPhone), stored in
  `credentials.json` (see *Credentials format*). From there the
  bridge serves requests and attempts refreshes server-side.
- **Known unknowns, stated plainly.** Profile issuance is IP-aware
  (the backend returns a `clientIP` when a profile is prepared), and
  whether Milq token refresh and profile re-issuance work from a
  server is per-account — it can only be verified from a real
  capture, not promised in advance.
- **The account portal is not a credential source.**
  `account.milq.ai` exposes account and signing administration only —
  no model catalog, no provider profile.

## 🚀 Quickstart

```bash
# 1. Prerequisite: an enrolled FreeMilk account + one captured app
#    session from it (provider profile + Milq tokens). Accounts are
#    born inside the iOS app — web sign-in cannot enrol one.
#    See "Reality check" above.

# 2. Install (renders + enables the systemd service; ./run.sh works too)
bash install.sh

# 3. Feed the captured session material — once per account…
printf '%s' '<captured session JSON>' | python3 login_fetch.py set-tokens
#    …or many accounts at once:
python3 login_fetch.py bulk-add captures.json

# 4. Auto-register in 9Router (asks for the console password once)
python3 login_fetch.py sync-9router
```

> ⚠️ **Sign-in only works from the literal `localhost` hostname.**
> Open the sign-in page as `http://localhost:18620/auth.html`. The
> IP form `http://127.0.0.1:18620/auth.html` — and any tunnel or
> public URL (ngrok, cloudflared, localhost.run, GitHub Pages) —
> fails with `auth/unauthorized-domain`: Firebase's allowlist for
> the FreeMilk project covers `localhost` and Milq's own domains
> only. Do not expose the auth page through a tunnel for login.

That's the whole flow. Step 4 detects your local 9Router, creates the
`freemilk` node, registers **one connection per account**, and adds the
`freemilk/opus-5.5` model. Add the model to any combo in the panel and
you're serving. Re-running step 4 after adding accounts only adds the
new connections.

<details>
<summary>Sample session</summary>

```console
$ bash install.sh
install: freemilk-bridge service enabled + started (port 18611)
install: bridge healthy at http://127.0.0.1:18611/health

$ # Session material captured from an enrolled account's app session
$ # (provider profile + Milq tokens — see "Reality check" above)
$ printf '%s' '<captured session JSON>' | python3 login_fetch.py set-tokens
stored: milqAccessToken, milqRefreshToken

$ python3 login_fetch.py status
bridge configured (legacy account): True
stored auth material: milqAccessToken, milqRefreshToken

$ python3 login_fetch.py sync-9router
9Router console password: ********
sync: created node FreeMilk (prefix freemilk -> http://127.0.0.1:18611/v1)
sync: created connection freemilk-acct-1a2b3c4d
sync: registered custom model freemilk/opus-5.5
sync-9router: OK — node 'freemilk', 1 account connection(s), model freemilk/opus-5.5
```

</details>

> 🤖 **Letting an AI agent drive?** Point it at [AGENTS.md](AGENTS.md) —
> a complete unattended runbook (install → feed captured material →
> sync → verify → troubleshoot) with the safety rules baked in.

## ✨ Features

| | Feature | What it does |
|---|---|---|
| <img src="docs/assets/icons/pool.svg" width="28"> | **Multi-account pool** | Serve from many FreeMilk accounts at once; requests spread least-recently-used across healthy accounts. A single account is just a pool of 1. |
| <img src="docs/assets/icons/key.svg" width="28"> | **Per-account bridge keys** | Every account gets its own key (`fmb_<accountId>_…`). Presenting it pins the request to that account — this is what makes router-side rotation work. |
| <img src="docs/assets/icons/router.svg" width="28"> | **9Router auto-registration** | `sync-9router` detects the local panel and idempotently creates the node, one connection per account, and the custom model. Re-runs add new accounts and disable stale connections. |
| <img src="docs/assets/icons/refresh.svg" width="28"> | **Self-healing credentials** | On an upstream 401 the bridge runs the profile's refresh hook once, persists the rotated credential (0600), and retries. |
| <img src="docs/assets/icons/isolate.svg" width="28"> | **Dead-account isolation** | An account that fails auth even after refresh is marked dead and skipped; the rest of the pool keeps serving. |
| <img src="docs/assets/icons/bolt.svg" width="28"> | **Streaming, three wire formats** | OpenAI passthrough (SSE byte-for-byte), Anthropic Messages translation, and OpenAI Responses translation — buffered and streamed. |
| <img src="docs/assets/icons/login.svg" width="28"> | **Captured-session input** | You feed the bridge session material captured from an enrolled account's genuine app session (provider profile + Milq tokens). Enrolment itself happens once, inside the iOS app — web sign-in cannot create accounts (see the reality check). Bulk-add whole batches of captures from one file. |
| <img src="docs/assets/icons/shield.svg" width="28"> | **Localhost-only, secret-safe** | Binds 127.0.0.1, credentials file is 0600, and logs/outputs never contain keys or tokens. Zero dependencies — Python 3 standard library only. |

## 🏗️ Architecture

```mermaid
flowchart LR
    U["Your apps / clients"] --> CB
    subgraph Panel["9Router panel"]
        CB["Combo"] --> M["Model<br/>freemilk/opus-5.5"]
        M --> N["Node 'freemilk'<br/>http://127.0.0.1:18611/v1"]
        N --> C1["Connection<br/>freemilk-acct-A"]
        N --> C2["Connection<br/>freemilk-acct-B"]
    end
    C1 --> B["freemilk-bridge<br/>routes by bridgeKey,<br/>LRU across the pool"]
    C2 --> B
    B --> P1["Account A profile"]
    B --> P2["Account B profile"]
    P1 --> UP["Milq-issued provider<br/>OpenAI · Anthropic · Responses"]
    P2 --> UP
```

### How accounts show up in 9Router

| FreeMilk account | In `credentials.json` | 9Router connection | Key the router sends |
|---|---|---|---|
| Account A | `accounts[0]`, id `acct-1a2b3c4d` | `freemilk-acct-1a2b3c4d` | `fmb_acct-1a2b3c4d_…` |
| Account B | `accounts[1]`, id `acct-5e6f7a8b` | `freemilk-acct-5e6f7a8b` | `fmb_acct-5e6f7a8b_…` |
| Legacy single account | top-level profile | `freemilk-default` | `fmb_default_…` |

The router rotates across its connections; each connection's key pins
the request to one account in the bridge. A connection used *without* a
per-account key (or with the optional master `BRIDGE_API_KEY`) falls
back to pool selection.

## 🔧 Configuration

Environment variables (bridge): `FREEMILK_BRIDGE_HOST` (default
`127.0.0.1` — keep it localhost-only), `FREEMILK_BRIDGE_PORT` (`18611`),
`FREEMILK_BRIDGE_CREDENTIALS`, `BRIDGE_API_KEY` (optional master key),
`FREEMILK_BRIDGE_CONCURRENCY` (`8`), `FREEMILK_BRIDGE_ACCOUNT_CONCURRENCY`
(`1` per account). For `sync-9router`: `PANEL_BASE_URL` (default
`http://127.0.0.1:2013`), `PANEL_PASSWORD`, `FREEMILK_BRIDGE_BASE_URL`
(default `http://127.0.0.1:18611/v1`).

## 🔒 Security notes

- The bridge is a **localhost service**. Do not bind it to a public
  interface; your router reaches it on the same box.
- `credentials.json` holds live provider credentials and Milq /
  Firebase tokens — it is written **0600**, is gitignored, and no tool
  here prints its values (`status` shows field names and states only).
- Token JSONs and the panel password are accepted **via stdin / prompt
  / env only** — never via argv — and are never logged.
- `sync-9router` touches **only** the `freemilk` node, that node's
  connections, and the `freemilk/opus-5.5` custom model.

<details>
<summary><strong>Manual 9Router wiring</strong> (fallback — sync-9router does exactly this)</summary>

1. **Provider Nodes → Add node**
   - Type: *OpenAI Compatible*, Name: `FreeMilk`, Prefix: `freemilk`
   - API type: *Chat*, Base URL: `http://127.0.0.1:18611/v1`
2. **Connections → Add** on that node, once per account:
   - Name: `freemilk-<accountId>` (e.g. `freemilk-acct-1a2b3c4d`)
   - API key: the account's `bridgeKey` from `credentials.json`
     (run `sync-9router` once to have keys issued, or set a master
     `BRIDGE_API_KEY` on the bridge and use that value)
3. **Models → Custom models → Add**: provider alias `freemilk`,
   model id `opus-5.5`.
4. **Combos**: add `freemilk/opus-5.5` to any combo.

> Note: the console's *validate* button fetches `{baseUrl}/models`
> from your browser session and may refuse localhost URLs when the
> console itself is opened through a tunnel (SSRF guard). Node
> creation and chat execution happen server-side and work regardless —
> verify with a real chat through the panel instead.

</details>

<details>
<summary><strong>How enrolment actually works</strong> (and what the bridge consumes)</summary>

FreeMilk sign-in is Firebase Authentication on project `milq-mobile`,
but the Firebase identity is only half of the story. The Milq account
itself is created inside the app: on first sign-in the app exchanges
the identity token with `api.milq.ai`, and that exchange includes a
**DeviceCheck token** minted by `DCDevice` on the physical device
(account records are auto-provisioned by that exchange — it is also
where the new-account intro credits are granted). The backend's app
namespaces (`/auth/*`, `/payments/*`, `/runtime/*`) answer every
non-app client with
`403 {"detail":"This client is not supported. Please use the Milq iOS app."}`
before any auth check, and Firebase's own endpoints refuse the app's
app-restricted API key from servers. The gate's exact check is not
statically visible; its behaviour is consistent with Firebase App
Check validation, and the exchange additionally requires the
DeviceCheck token. In practice:

- The Firebase **web** sign-in page (`auth.html`, served by
  `login_fetch.py login` / `serve-auth`) yields a Firebase ID token +
  refresh token. That is all it can yield: it performs no Milq
  exchange, so for an account that has never signed in inside the app
  it enrols nothing and fetches no profile.
- The supported input is **captured session material from an
  enrolled account**: run the real app once on a genuine iPhone,
  capture one full app session (e.g. with an MITM proxy on your own
  device/network), and extract the provider profile (`baseURL`,
  `apiKey`, `refreshCredentials`, …) plus the Milq access/refresh
  tokens. Store them in `credentials.json` (see *Credentials
  format*). Captured tokens can also be fed via `set-tokens`;
  `login_fetch.py fetch` then makes a best-effort attempt to resolve
  or refresh a profile from the stored material — expect it to fail
  against the client gate for accounts without captured Milq
  material.
- The portal at https://account.milq.ai is **not** a credential
  source: its API covers Apple signing, builds, certificates and
  install devices — no model catalog, no provider profile.

**Known unknowns.** Profile issuance is IP-aware: the prepare
response carries a `clientIP`, and whether an issued credential is
bound to that IP is unverified. Whether Milq token refresh works from
a server (rather than only in-app) is likewise per-account and
unverifiable without a real capture. The bridge serves and refreshes
captured material for as long as the upstream accepts it; it cannot
promise a lifetime.

**Economics & ToS.** Opus 5.5 draws from a small per-account daily
allowance ("Sips"), and FreeMilk's terms prohibit extracting provider
information / misusing the service. A bridge yields each account's
daily ration and carries account-termination risk; treat traffic
through it as non-private. Pooling accounts multiplies the ration —
and the risk. You are responsible for your own accounts.

</details>

<details>
<summary><strong>Credentials format</strong></summary>

See `credentials.example.json`. Two shapes, freely combinable — the
bridge serves every profile it finds:

- **Legacy single account:** provider profile fields at top level
  (`baseURL`, `apiKey` required; `protocol`, `model`, `models`,
  `sessionId`, `extraHeaders`, `maxApiTokens`, `refreshCredentials`
  optional), login material under a top-level `milq` object.
- **Pool:** an `accounts` array; each entry carries the same profile
  fields plus `id`, its own `bridgeKey`, and its own `milq` object.
  An entry may also carry `dead: true` (set automatically when the
  account's credential dies).

The file hot-reloads whenever it changes; the bridge starts cleanly
unconfigured (`503 {"error": "freemilk-bridge not configured"}`) when
no profile exists yet.

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Symptom | Likely cause | Fix |
|---|---|---|
| `sync-9router` exits 2, "panel login failed" | Wrong console password, or no 9Router at `PANEL_BASE_URL` | Re-check password; verify the panel URL (default `http://127.0.0.1:2013`) |
| Chat → 503 "not configured" | No account has a profile yet | Feed captured session material for an enrolled account (see Reality check); `fetch` retries resolution from stored material |
| Chat → 401 from the bridge | Presented key matches neither master key nor any `bridgeKey` | Use the connection's bridgeKey, or set `BRIDGE_API_KEY` |
| `accountsHealthy` dropped, an account shows `dead` in `status` | That account's credential died (refresh failed too) | Re-capture that account's app session and re-feed the material (`set-tokens` / `bulk-add`), then re-run `sync-9router` |
| Every fetch candidate 403 "This client is not supported" | The backend's app-only client gate — issuance is not reachable from a server | Feed captured session material from an enrolled account (see Reality check); web sign-in can neither enrol an account nor fetch its profile |
| Console *validate* fails but chats work | SSRF guard on localhost base URLs via tunneled console | Ignore validate; verify with a panel chat |

</details>

## 🧪 Tests

All offline — mock upstreams, a fake Firebase/Milq backend, and a mock
9Router panel implementing the real console contract:

```bash
python3 tests/selftest.py        # bridge: protocols, refresh, 9Router shapes, pool (37 checks)
python3 tests/login_selftest.py  # set-tokens / fetch / login / bulk-add (20 checks)
python3 tests/sync_selftest.py   # sync-9router idempotency + lifecycle (11 checks)
```

## 📁 Files

| File | Purpose |
|---|---|
| `bridge.py` | the bridge server (stdlib only) |
| `login_fetch.py` | captured-material intake, bulk-add, profile fetch, `sync-9router` |
| `auth.html` | Firebase sign-in page (yields Firebase tokens only — cannot enrol accounts; see Reality check) |
| `install.sh` / `run.sh` | one-command install (systemd) / no-systemd runner |
| `freemilk-bridge.service` | systemd unit template (rendered by `install.sh`) |
| `credentials.example.json` | credentials contract example (no real values) |
| `AGENTS.md` | unattended operator runbook for AI agents |
| `tests/` | mock upstream, fake Milq, mock panel + self-tests |
| `docs/assets/` | logo + feature icons (self-contained SVGs) |

## License

MIT — see `LICENSE`.
