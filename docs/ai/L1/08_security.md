# L1 — Security

> API key management, token generation, CORS policy, and network exposure considerations.

## API Keys

| Key | Risk if leaked | Storage |
|---|---|---|
| `OPENAI_API_KEY` | Billing abuse | `.env` only |
| `DEEPGRAM_API_KEY` | Billing abuse | `.env` only |
| `ELEVENLABS_API_KEY` | Billing abuse, voice cloning | `.env` only |
| `AGORA_APP_ID` | Channel access (low risk alone) | `.env`, viewer URL params |
| `AGORA_APP_CERT` | Token forgery (high risk) | `.env` only, never client-side |
| `SPORTRADAR_API_KEY` | API quota abuse | `.env` only |

- `.env` is in `.gitignore` — never committed
- `.env.example` contains placeholder values only
- `live_match.py` loads `.env` via `_load_dotenv()` using `os.environ.setdefault()` — won't override existing env vars
- The ElevenLabs API key is sent over the WebSocket connection in the initial handshake message (`xi_api_key` field)

## Agora Tokens

- Tokens are generated server-side via `tokens.py` (v007 format)
- Token privileges: join channel, publish audio/video/data, RTM login
- Tokens have configurable expiry (default 900s for `AccessToken`, 3600s for `build_token_with_rtm`)
- The `APP_CERT` is used for HMAC-SHA256 signing — never expose it to clients
- If `APP_CERT` is empty, `build_token_with_rtm()` returns the `APP_ID` as the token (testing mode only)

### Token generation flow

```
POST /api/session
  → tokens.build_token_with_rtm(app_id, app_cert, channel, uid, expire=3600)
  → HMAC-SHA256 sign + zlib compress + base64 encode
  → return token in JSON response
  → viewer uses token to join Agora channel
```

### Token privileges granted

| Privilege | Value | Purpose |
|---|---|---|
| `kPrivilegeJoinChannel` | 1 | Join the RTC channel |
| `kPrivilegePublishAudioStream` | 2 | Publish audio (Go publisher only) |
| `kPrivilegePublishVideoStream` | 3 | Publish video (Go publisher only) |
| `kPrivilegePublishDataStream` | 4 | Publish data stream |
| RTM `kPrivilegeLogin` | 1 | RTM login for signalling |

## Viewer Security

- `viewer.html` accepts `appid`, `channel`, `token` as URL query parameters
- The token must be pre-generated server-side and passed to the viewer
- The viewer connects as audience (UID 101) — it cannot publish
- Default App ID is hardcoded in `viewer.html` — override with `?appid=` param

## CORS

- `ControlHandler` sets `Access-Control-Allow-Origin: *` on all responses
- This allows the viewer (served from any origin) to call the control API
- For production, restrict the CORS origin to the viewer's domain

## Network Exposure

- Control server listens on `0.0.0.0:8090` by default — exposed to local network
- No authentication on control endpoints (`/start`, `/stop`, `/set-lang`)
- For production, add authentication or bind to `127.0.0.1`

## WebSocket Security

- ElevenLabs: API key sent in the initial WebSocket handshake (`xi_api_key` field in JSON)
- Deepgram: API key sent as a query parameter or header on WebSocket connect
- Both connections use `wss://` (TLS)

## Subprocess Isolation

- Each session's Go publisher runs in its own process group (`preexec_fn=os.setsid`)
- `kill_publisher()` sends `SIGKILL` to the entire process group via `os.killpg()`
- If the Python process crashes without cleanup, Go publishers become orphaned (see gotchas)
- No shell injection risk: `subprocess.Popen` uses argument lists, not shell strings

## Related Deep Dives

None — security considerations are self-contained.
