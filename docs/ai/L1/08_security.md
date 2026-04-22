# L1 — Security

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
