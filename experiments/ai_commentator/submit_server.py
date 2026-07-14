#!/usr/bin/env python3
"""Tiny submission backend for the vision/tracker eval scoring page.

Receives POSTed reviewer scores and stores ONE file per reviewer (so several
people can score the same clip and we aggregate). nginx proxies /vte_submit ->
127.0.0.1:8091 so the page stays same-origin (no CORS / no extra open ports).

Run (persistent):
  /home/ubuntu/commentary/.venv/bin/python submit_server.py
Aggregate:
  ls experiments/ai_commentator/vte_scores/*.json
"""
import json, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCORES = Path('/home/ubuntu/commentary/experiments/ai_commentator/vte_scores')
SCORES.mkdir(parents=True, exist_ok=True)
PORT = 8091


def safe(name):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', (name or 'anon'))[:40] or 'anon'


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            return self._send(400, {'ok': False, 'error': f'bad json: {e}'})
        rev = safe(data.get('reviewer'))
        test = safe(data.get('test', 'default'))
        data['_received_at'] = time.time()
        # one file per (test, reviewer) — latest wins — + append-only audit log
        tdir = SCORES / test; tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f'{rev}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
        with (SCORES / 'submissions.jsonl').open('a') as f:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')
        ok = sum(1 for _ in (data.get('ticks') or {}))
        print(f"[submit] reviewer={rev} ticks={ok}")
        return self._send(200, {'ok': True, 'reviewer': rev, 'ticks': ok})

    def do_GET(self):
        # simple health / listing
        revs = sorted(p.stem for p in SCORES.glob('*.json'))
        return self._send(200, {'ok': True, 'reviewers': revs})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print(f"submit_server on 127.0.0.1:{PORT} -> {SCORES}")
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
