# L1 — Ubuntu Server Deployment

> End-to-end guide for deploying the commentary server on a public Ubuntu instance.

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-pip python3-venv \
  ffmpeg libavformat-dev libavcodec-dev libavutil-dev libx264-dev pkg-config \
  build-essential git curl
```

**Go 1.21+** — Ubuntu's default `golang-go` package may be too old. Install manually if needed:

```bash
curl -LO https://go.dev/dl/go1.21.13.linux-amd64.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go1.21.13.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
source ~/.bashrc
go version
```

## 2. Application install

```bash
cd /home/ubuntu
git clone <repo-url> commentary
cd commentary

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in API keys — see 01_setup.md "Environment Variables" for the full table
```

## 3. Agora SDK on Linux

The Go publisher depends on the Agora Go Server SDK via a `replace` directive in `go.mod`.

### Download and place the SDK

```bash
# Clone or download the Agora Go Server SDK into a known path
# Example: /home/ubuntu/agora-sdk
mkdir -p /home/ubuntu/agora-sdk
# Place SDK contents here (follow Agora's server SDK download instructions)
```

### Update go.mod replace directive

Edit `go-audio-video-publisher/go.mod` line 10 — change the `replace` directive to point to your SDK path:

```go
replace github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2 => /home/ubuntu/agora-sdk
```

### Set LD_LIBRARY_PATH

On Linux, use `LD_LIBRARY_PATH` (not macOS's `DYLD_LIBRARY_PATH`):

```bash
export LD_LIBRARY_PATH=/home/ubuntu/agora-sdk/agora_sdk_linux:$LD_LIBRARY_PATH
```

### Build Go binaries

```bash
cd /home/ubuntu/commentary/go-audio-video-publisher
make build-all
```

This produces three binaries in `bin/`:
- `go-audio-video-publisher` — main publisher
- `subscribe_audio` — audio subscriber
- `relay_publish` — relay publisher

### Verify native library linkage

```bash
ldd bin/go-audio-video-publisher
```

All Agora `.so` references should resolve. If any show "not found", verify `LD_LIBRARY_PATH`.

## 4. Media assets

Demo matches reference clip files in `matches.yaml`:

```yaml
audio: clips/bmg_fch_demo_5min/audio.mp3
video_h264: clips/bmg_fch_demo_5min/video.h264
events: clips/bmg_fch_demo_5min/events.txt
atmosphere: clips/bmg_fch_demo_5min/atmosphere.wav
```

**Path resolution rule:** all file paths are resolved relative to the config file's directory. If `matches.yaml` is at `/home/ubuntu/commentary/matches.yaml`, then `clips/bmg_fch_demo_5min/audio.mp3` resolves to `/home/ubuntu/commentary/clips/bmg_fch_demo_5min/audio.mp3`.

Ensure clip directories are present before starting:

```bash
ls /home/ubuntu/commentary/clips/
```

## 5. systemd service

Create `/etc/systemd/system/commentary.service`:

```ini
[Unit]
Description=Commentary Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/commentary
ExecStart=/home/ubuntu/commentary/.venv/bin/python3 -m server.main --config matches.yaml
EnvironmentFile=/home/ubuntu/commentary/.env
Environment=LD_LIBRARY_PATH=/home/ubuntu/agora-sdk/agora_sdk_linux
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable commentary
sudo systemctl start commentary
sudo systemctl status commentary
```

View logs:

```bash
sudo journalctl -u commentary -f
```

## 6. Reverse proxy (nginx + TLS)

### Install nginx and certbot

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

### nginx site config

Create `/etc/nginx/sites-available/commentary`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/commentary /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### TLS with Let's Encrypt

```bash
sudo certbot --nginx -d your-domain.com
```

Certbot modifies the nginx config to add TLS and sets up auto-renewal.

No WebSocket upgrade headers are needed — the HTTP API is plain REST.

## 7. Firewall

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

- Port 8080 stays localhost-only (nginx proxies public traffic to it).
- The Agora SDK uses **outbound UDP** for media streaming — no inbound ports needed for Agora.

## 8. Log management

### Server logs

```bash
sudo journalctl -u commentary -f          # live tail
sudo journalctl -u commentary --since today  # today's logs
```

systemd/journald handles rotation automatically.

### Runtime JSONL logs

The server writes per-match runtime logs under `match_data/`. These JSONL files grow over time and have **no built-in rotation**.

Monitor disk usage:

```bash
du -sh /home/ubuntu/commentary/match_data/
```

Optional logrotate config — create `/etc/logrotate.d/commentary`:

```
/home/ubuntu/commentary/match_data/**/*.jsonl {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

## 9. Verification checklist

1. **Validate config** (dry run):
   ```bash
   cd /home/ubuntu/commentary
   source .venv/bin/activate
   python3 -m server.main --config matches.yaml --dry-run
   ```

2. **Start the service**:
   ```bash
   sudo systemctl start commentary
   sudo systemctl status commentary
   ```

3. **Test API endpoint**:
   ```bash
   curl http://localhost:8080/api/matches
   ```
   Should return JSON with configured matches.

4. **Start a demo match** via API and confirm status changes:
   ```bash
   curl -X POST http://localhost:8080/api/matches/bmg_fch_demo/start
   curl http://localhost:8080/api/matches/bmg_fch_demo
   ```

5. **Confirm viewer** loads at `https://your-domain.com/viewer_live.html`.

## Related Deep Dives

- [01_setup.md](01_setup.md) — prerequisites, env vars, Go publisher setup (macOS-focused)
- [07_gotchas.md](07_gotchas.md) — `go.mod` replace directive pitfalls, YAML path gotchas
