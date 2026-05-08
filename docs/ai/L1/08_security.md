# L1 — Security

> API key management, token generation, CORS policy, network exposure, and security gaps.

## API Keys

| Key | Risk if leaked | Storage |
|---|---|---|
| `OPENAI_API_KEY` | Billing abuse | `.env` only |
| `DEEPGRAM_API_KEY` | Billing abuse | `.env` only |
| `ELEVENLABS_API_KEY` | Billing abuse, voice cloning | `.env` only |
| `AGORA_APP_ID` | Channel access (low risk alone) | `.env`, returned to viewer via API |
| `AGORA_APP_CERT` | Token forgery (high risk) | `.env` only, never client-side |
| `SPORTRADAR_API_KEY` | API quota abuse | `.env` only — used by `commentary_feeder.py`, `generate_demo_transcript.py`, and server mode roster fetch |

- `.env` is in `.gitignore` — never committed
- `.env.example` contains placeholder values only
- Both `live_match.py` and `server/main.py` load `.env` via `_load_dotenv()` using `os.environ.setdefault()` — won't override existing env vars
- The ElevenLabs API key is sent over the WebSocket connection in the initial handshake message (`xi_api_key` field) — used in `lib/tts_engine.py` and `lib/sr_prefetcher.py`

## Agora Tokens

- Viewer tokens are generated server-side via `generate_viewer_token()` in `server/token_api.py` (production) or `_generate_viewer_token()` in `live_match.py` (dev mode)
- Viewer tokens grant only `kPrivilegeJoinChannel` — viewers cannot publish
- Go publisher generates its own token internally via `buildToken()` → `rtctokenbuilder.BuildTokenWithUserAccount()` with `RolePublisher`
- Both modes pass `AGORA_APP_CERT` to the Go publisher via the `AGORA_APP_CERTIFICATE` env var
- Both modes refuse to start without `AGORA_APP_CERT` — there is no empty-cert fallback
- Token expiry: 86400s (24 hours) for viewer tokens, 3600s (1 hour) for publisher tokens
- The `APP_CERT` is used for HMAC-SHA256 signing — never expose it to clients

### Token generation flow (production server)

```
GET /api/matches/{id}/channels
  → generate_viewer_token(app_id, app_cert, channel, uid, expire_s=86400)
  → AccessToken + ServiceRtc(kPrivilegeJoinChannel only)
  → HMAC-SHA256 sign + zlib compress + base64 encode
  → return {appid, channels: {lang: {channel, token, uid}}} in JSON

POST /api/token
  → body: {match_id, lang}
  → generate_viewer_token() for single channel
  → return {token, channel, uid, appid}
```

### Token generation flow (dev mode)

```
POST /api/session
  → _generate_viewer_token(channel, uid, expire_s=86400)
  → AccessToken + ServiceRtc(kPrivilegeJoinChannel only)
  → return token, channel, uid, appid in JSON response
```

### Viewer token privileges

| Privilege | Value | Viewer | Go Publisher |
|---|---|---|---|
| `kPrivilegeJoinChannel` | 1 | Yes | Yes |
| `kPrivilegePublishAudioStream` | 2 | No | Yes |
| `kPrivilegePublishVideoStream` | 3 | No | Yes |
| `kPrivilegePublishDataStream` | 4 | No | Yes |

## Production Server Security

### Network exposure

- Production server listens on `0.0.0.0:8080` by default — exposed to local network
- Dev-mode server listens on `0.0.0.0:8090` — same exposure

### Static pages

- `status.html` — intended as a public read-only dashboard
- `control.html` — admin control page with start/stop buttons
- `viewer_live.html` — production viewer, requires match_id parameter

### Token endpoint

- `POST /api/token` generates viewer-only tokens on demand
- Requires `match_id` and `lang` in request body
- Returns audience-only token (join only, no publish)

## CORS

- Both `StatusHandler` (production) and `ControlHandler` (dev mode) set `Access-Control-Allow-Origin: *` on all responses
- This allows the viewer (served from any origin) to call the control API
- For production, restrict the CORS origin to the viewer's domain

## Current Gaps

The following are known security gaps in the current implementation:

- **No authentication on control endpoints**: `POST /api/matches/{id}/start` and `POST /api/matches/{id}/stop` are open to anyone who can reach the server. Production deployment should add auth middleware or restrict to internal network.
- **CORS `*` on all endpoints**: all API responses allow any origin. Production should restrict to known viewer domains.
- **status.html has start/stop buttons**: the status page was intended as read-only but currently includes control functionality. Control should be restricted to `control.html` with auth.
- **No rate limiting on token generation**: `POST /api/token` and `GET /api/matches/{id}/channels` generate tokens without rate limits. A flood of requests could exhaust UID space or create excessive Agora connections.
- **No HTTPS**: both servers serve over plain HTTP. Production should terminate TLS at a reverse proxy.

## Viewer Security

- `viewer_live.html` accepts `match` and `lang` as URL query parameters
- On language switch, the viewer requests a fresh token via `POST /api/token` (on-demand, not prefetched)
- Viewer UIDs are incrementing from 100 (globally unique per server lifetime)
- The viewer connects as audience — its token only permits joining, not publishing

### Dev-mode viewer

- `viewer.html` accepts `ctl` (control server URL) and `lang` (initial language) as URL query parameters
- On load, the viewer creates a session via `POST /api/session` which returns `appid`, `channel`, `token`, `uid`
- Viewer UID is randomly generated per session (1000–9999 range)

## WebSocket Security

- ElevenLabs: API key sent in the initial WebSocket handshake (`xi_api_key` field in JSON)
- Deepgram: API key sent as a query parameter or header on WebSocket connect
- Both connections use `wss://` (TLS)

## Subprocess Isolation

- Each Go publisher runs in its own process group (`preexec_fn=os.setsid`)
- `_kill_publisher()` sends `SIGKILL` to the entire process group via `os.killpg()`
- If the Python process crashes without cleanup, Go publishers become orphaned (see gotchas)
- No shell injection risk: `subprocess.Popen` uses argument lists, not shell strings

## Related Deep Dives

None — security considerations are self-contained.
