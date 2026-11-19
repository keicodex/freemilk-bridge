# AGENTS.md — freemilk-bridge operator runbook

Instructions for an AI agent (or a human in a hurry) to install,
configure and verify freemilk-bridge **unattended**, end to end.
Every step lists its success criterion; do not proceed past a failed
criterion — jump to Troubleshooting.

## Safety rules (non-negotiable)

1. The bridge binds **127.0.0.1 only**. Never expose it on a LAN/WAN
   interface; it is consumed by a local router (9Router) on the same
   box.
2. `credentials.json` is **mode 0600** and holds live credentials.
   Never print its values, never paste them into logs, tickets or
   chat, never commit it (it is gitignored). Tools here print field
   *names* and account ids only — keep it that way.
3. Token JSONs (Firebase ID/refresh tokens, session cookies) travel
   **via stdin or files the operator hands you**, never via argv
   (visible in `ps`), never via environment dumps.
4. The 9Router console password is supplied **once** via the
   `PANEL_PASSWORD` env var or the interactive prompt of
   `sync-9router`. Never write it to any file, never echo it.
5. In the 9Router panel, only ever touch: the node with prefix
   `freemilk`, the connections belonging to that node, and the custom
   model `freemilk/opus-5.5`. `sync-9router` already enforces this —
   do not hand-edit other panel state "while you're in there".
6. Temporary panel registrations created for testing must be deleted
   completely afterwards, and panel counts (nodes / connections /
   custom models) verified back to their pre-test values.

## Step 0 — prerequisites

- Linux box with Python 3.8+ (`python3 --version`), no pip installs.
- A local 9Router panel (for step 3). Default panel URL is
  `http://127.0.0.1:2013` (the router's `PORT` default); override
  with `PANEL_BASE_URL`.
- One or more FreeMilk accounts the operator can sign in to.

## Step 1 — install and start

```bash
bash install.sh
```

Success: the command prints `install: bridge healthy at
http://127.0.0.1:18611/health`. No systemd? Run `./run.sh` in a
screen/tmux session instead (same health check applies).

Verify directly:

```bash
curl -s http://127.0.0.1:18611/health
# {"ok": true, "service": "freemilk-bridge", "version": "0.3.0",
#  "configured": false, "accounts": 0, "accountsHealthy": 0}
```

`configured: false` is expected until step 2 completes.

## Step 2 — log accounts in

### One account (guided)

```bash
python3 login_fetch.py login
```

The command serves the sign-in page on `http://localhost:18620/auth.html`.
The operator opens it in a browser **on this machine**, signs in with
Google, and the page shows a JSON object with
`idToken` + `refreshToken`. The operator hands that JSON to you
**once**; feed it to the waiting command on stdin. It stores the
tokens, fetches the provider profile, and prints a verdict.

Success: exit code 0 and a line starting `LOGIN OK`.

> ⚠️ **The sign-in page ONLY works from the literal `localhost`
> hostname.** `http://127.0.0.1:18620/auth.html` and any tunnel or
> public URL (ngrok, cloudflared, localhost.run, GitHub Pages) fail
> with `auth/unauthorized-domain` — Firebase's allowlist for the
> FreeMilk project (milq-mobile) covers `localhost` and Milq's own
> domains only. Never expose the auth page through a tunnel for
> login; if the operator cannot open a browser on this machine, use
> the account.milq.ai cookie alternative below instead.

Alternative token source: the operator signs in at
https://account.milq.ai and hands you the session cookie value
instead; store it with
`printf '%s' '<paste>' | python3 login_fetch.py set-tokens`
(JSON: `{"sessionCookie": "..."}`), then `python3 login_fetch.py fetch`.

### Many accounts (bulk)

Collect one token JSON per account (same shape as above) into a
file — a JSON array, JSONL, or blank-line-separated objects all
parse. Then:

```bash
python3 login_fetch.py bulk-add accounts.json
```

Success: exit code 0 and a final line `bulk-add: N/N account(s)
ready`. Per-account failures are reported individually and do not
block the others; re-running the same file is safe (accounts dedupe
by id, derived from their own refresh token).

Check the pool at any time (names and states only, no secrets):

```bash
python3 login_fetch.py status
curl -s http://127.0.0.1:18611/health   # accountsHealthy >= 1
```

## Step 3 — register in 9Router (automatic)

```bash
PANEL_PASSWORD='<console password>' python3 login_fetch.py sync-9router
```

(Or omit the env var and answer the one-time prompt.) The command
logs in to the panel, then idempotently ensures:

- node `FreeMilk`, prefix `freemilk`, type openai-compatible,
  baseUrl `http://127.0.0.1:18611/v1`;
- one connection per ready account, named `freemilk-<accountId>`,
  whose apiKey is that account's `bridgeKey` (issued by the sync
  itself and persisted in `credentials.json`);
- the custom model `freemilk/opus-5.5`.

Success: exit code 0, final line `sync-9router: OK — ...`.
Re-running after adding accounts adds only the new connections;
accounts that died or were removed get their connections disabled.
Adding the model to a combo is the operator's choice in the panel
(Combos → pick combo → add `freemilk/opus-5.5`).

## Step 4 — verify end to end

```bash
curl -s http://127.0.0.1:18611/health
# "configured": true, "accountsHealthy": >= 1

curl -s http://127.0.0.1:18611/v1/models \
  -H "Authorization: Bearer <any account bridgeKey from the panel connection>"
# data[0].id == "opus-5.5"
```

Then one chat **through the panel** (use the panel's own test key /
Default Key from inside the box, never a member key):

```bash
curl -s http://127.0.0.1:2013/v1/chat/completions \
  -H "Authorization: Bearer <panel test key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "freemilk/opus-5.5", "messages": [{"role": "user", "content": "ping"}]}'
# HTTP 200 with an assistant message
```

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `login`/`fetch`: every candidate 401/403, errors mention App Check / attestation | api.milq.ai gated the issuance path with Firebase App Check (App Attest). A pure server-side login cannot mint those tokens. | **Stop.** Report it. The only way forward is an `appCheckToken` captured from a real signed-in app session, stored via `set-tokens`. Do not attempt to forge attestation. |
| `sync-9router` exits 2 with "panel login failed" | Wrong console password, or no 9Router at `PANEL_BASE_URL`. | Re-check the password with the operator; verify the panel URL (`curl -s http://127.0.0.1:2013/api/provider-nodes` should 401, not refuse). |
| Health: `accountsHealthy` dropped; `status` shows an account `dead` | That account's upstream credential was rejected even after refresh (rotated/revoked upstream, allowance account closed). The bridge marked it dead and fails over to the rest. | Re-run `login`/`bulk-add` for that account with a fresh sign-in; then `sync-9router` (its connection already exists). |
| Chat returns 503 `freemilk-bridge not configured` | No account carries a profile yet. | Complete step 2; `login_fetch.py fetch` retries profile issuance for accounts that have material but no profile. |
| Chat returns 401 from the bridge | Key material is configured and the presented key matches neither the master `BRIDGE_API_KEY` nor any account `bridgeKey`. | Use the connection's bridgeKey (see the panel connection), or set the master key env on the bridge. |
| Panel console "validate" button fails for the node while chats work | The console's SSRF guard refuses localhost base URLs when the console itself is reached through a tunnel/public URL. Node creation and chat execution are server-side and unaffected. | Ignore the validate result; verify with the step-4 panel chat instead. |
| Bridge port already in use | Another bridge instance (or an old `run.sh`) is running. | `systemctl status freemilk-bridge`, or find the listener: `ss -tlnp \| grep 18611`. |

## Development

Run the full test suite (all offline, mock upstreams + mock panel):

```bash
python3 tests/selftest.py        # bridge: protocols, 9Router shapes, pool
python3 tests/login_selftest.py  # login / fetch / bulk-add vs fake Milq
python3 tests/sync_selftest.py   # sync-9router vs mock panel
```
