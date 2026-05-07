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

- Viewer tokens are generated server-side via `_generate_viewer_token()` using `AccessToken` + `ServiceRtc` from `tokens.py` (v007 format)
- Viewer tokens grant only `kPrivilegeJoinChannel` — viewers cannot publish
- Go publisher tokens use `build_token_with_rtm()` with full RTC+RTM privileges
- Token expiry: 3600s (1 hour) for both viewer and publisher tokens
- The `APP_CERT` is used for HMAC-SHA256 signing — never expose it to clients
- If `APP_CERT` is empty, `build_token_with_rtm()` returns the `APP_ID` as the token (testing mode only)

### Token generation flow

```
POST /api/session
  → _generate_viewer_token(channel, uid, expire_s=3600)
  → AccessToken + ServiceRtc(kPrivilegeJoinChannel only)
  → HMAC-SHA256 sign + zlib compress + base64 encode
  → return token, channel, uid, appid in JSON response
  → viewer uses token to join Agora channel
```

### Viewer token privileges

| Privilege | Value | Viewer | Go Publisher |
|---|---|---|---|
| `kPrivilegeJoinChannel` | 1 | Yes | Yes |
| `kPrivilegePublishAudioStream` | 2 | No | Yes |
| `kPrivilegePublishVideoStream` | 3 | No | Yes |
| `kPrivilegePublishDataStream` | 4 | No | Yes |
| RTM `kPrivilegeLogin` | 1 | No | Yes |

## Viewer Security

- `viewer.html` accepts only `ctl` (control server URL) and `lang` (initial language) as URL query parameters
- On load, the viewer creates a session via `POST /api/session` which returns `appid`, `channel`, `token`, `uid`
- Viewer UID is randomly generated per session (1000–9999 range)
- The viewer connects as audience — its token only permits joining, not publishing

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
