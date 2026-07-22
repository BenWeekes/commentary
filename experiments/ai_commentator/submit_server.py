#!/usr/bin/env python3
"""Reviewer feedback backend for the blend pages (and the legacy tick-score UI).

Runs on 127.0.0.1:8091 behind nginx (same-origin; no CORS games, no open ports).

Endpoints (nginx maps /vte_submit, /blend_feedback, /blend_rounds, /blend_trigger):
  POST /submit          - legacy tick-score payloads (one file per reviewer)
  POST /blend_feedback  - cell-level comments {reviewer, version, items:[...]}
                          accepted only while that version's round is OPEN;
                          late submissions -> 409 + stored under late/ (rejected,
                          not destroyed - a good late comment can be promoted)
  GET  /blend_rounds    - round state machine (which version is open)
  POST /blend_trigger   - {version, pin, triggered_by}: PIN-guarded round close;
                          writes trigger_<version>.json as the work order for the
                          next tuning cycle. Exactly one person presses this.

Run (persistent):
  nohup /home/ubuntu/commentary/.venv/bin/python submit_server.py &
"""
import json, os, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
SCORES = BASE / 'vte_scores'
FEEDBACK = BASE / 'feedback'
ROUNDS = FEEDBACK / 'rounds.json'
PIN_FILE = FEEDBACK / 'pin.txt'
SCORES.mkdir(parents=True, exist_ok=True)
FEEDBACK.mkdir(parents=True, exist_ok=True)
PORT = 8091


import hmac, threading
_LOCK = threading.Lock()          # MEDIUM: serialize round state + appends
MAX_BODY = 256 * 1024             # HIGH: 256KB request cap

def safe(name):
    v = re.sub(r'[^A-Za-z0-9_-]', '_', str(name or ''))[:64]   # no '.', so no '..'
    return v or 'anon'

def _under(path, root):           # HIGH: destination must stay under its root
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))


def load_rounds():
    try:
        return json.loads(ROUNDS.read_text())
    except Exception:
        return {"current": None, "rounds": {}}


# Column -> where feedback on it routes in the build. Mirrors the on-page "How to review":
# EN = the commentary; FR/PT = the translation of it; STT/Vision/Tracker = the input signals.
ROUTING = {
    'English':    'commentary content — chooser/writer prompt + content rules (R1-R6, R9, R10)',
    'French':     'FR translation — football-French localizer + glossary (R7)',
    'Portuguese': 'PT translation — Brazilian localizer + glossary',
    'STT':        'input signal — ASR sanity/veto (R8); rarely a rule target',
    'Vision':     'input signal — detector/vision prompt; perception limits are roadmap, not rules',
    'Tracker':    'input signal — tracker corroboration (R10); positions are objective',
}


def digest_round(version):
    """Group a round's comments by (profile, column) with the build-side routing target,
    so the trigger work order is actionable along the same axes reviewers used."""
    cf = FEEDBACK / version / 'comments.jsonl'
    groups, total = {}, 0
    if cf.exists():
        for line in cf.open():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for it in (rec.get('items') or []):
                total += 1
                prof = it.get('profile') or '?'
                coln = it.get('column') or ('col' + str(it.get('col')))
                key = f'{prof}|{coln}'
                g = groups.setdefault(key, {
                    'profile': prof, 'column': coln,
                    'routes_to': ROUTING.get(coln, 'unclassified — operator to route'),
                    'count': 0, 'positive': 0, 'comments': []})
                g['count'] += 1
                if '👍 good' in (it.get('tags') or []):
                    g['positive'] += 1
                g['comments'].append({'t': it.get('t'), 'clip': it.get('clip'),
                                      'by': rec.get('reviewer'), 'tags': it.get('tags'),
                                      'comment': it.get('comment')})
    return {'total_items': total,
            'groups': sorted(groups.values(), key=lambda g: (g['profile'], g['column']))}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        if n > MAX_BODY:
            raise ValueError('body too large')
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        if self.path.startswith('/blend_rounds'):
            return self._send(200, load_rounds())
        revs = sorted(p.stem for p in SCORES.glob('*.json'))
        return self._send(200, {'ok': True, 'reviewers': revs})

    def do_POST(self):
        try:
            data = self._body()
        except Exception as e:
            return self._send(400, {'ok': False, 'error': f'bad json: {e}'})

        if self.path.startswith('/submit'):            # legacy tick-score UI
            rev = safe(data.get('reviewer'))
            test = safe(data.get('test', 'default'))
            data['_received_at'] = time.time()
            tdir = SCORES / test
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / f'{rev}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
            with (SCORES / 'submissions.jsonl').open('a') as f:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
            return self._send(200, {'ok': True, 'reviewer': rev})

        if self.path.startswith('/blend_feedback'):
            version = safe(data.get('version', ''))
            reviewer = safe(data.get('reviewer'))
            items = data.get('items', [])
            if not version or not isinstance(items, list) or not items:
                return self._send(400, {'ok': False, 'error': 'version and items[] required'})
            def _str(v, n):                       # only real strings; missing/objects -> ''
                return v[:n] if isinstance(v, str) else ''
            def _clean(it):
                if not isinstance(it, dict): return None
                return {'t': float(it.get('t', 0) or 0), 'col': int(it.get('col', -1) or -1),
                        'column': _str(it.get('column'), 32),
                        'profile': _str(it.get('profile'), 8),
                        'clip': _str(it.get('clip'), 48),
                        'cell_text': str(it.get('cell_text', ''))[:400],
                        'tags': [str(x)[:24] for x in (it.get('tags') or [])][:8],
                        'comment': str(it.get('comment', ''))[:1000]}
            items = [c for c in (_clean(i) for i in items[:60]) if c]
            if not items:
                return self._send(400, {'ok': False, 'error': 'no valid items'})
            rec = {'reviewer': reviewer, 'version': version,
                   'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                   'items': items}
            with _LOCK:
                rounds = load_rounds()
                status = (rounds.get('rounds', {}).get(version) or {}).get('status')
                if status == 'open':
                    d = FEEDBACK / version; d.mkdir(parents=True, exist_ok=True)
                    if not _under(d, FEEDBACK):
                        return self._send(400, {'ok': False, 'error': 'bad version'})
                    with (d / 'comments.jsonl').open('a') as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    print(f"[feedback] {reviewer} -> {version}: {len(items)} items")
                    return self._send(200, {'ok': True, 'stored': len(items)})
            d = FEEDBACK / version / 'late'
            d.mkdir(parents=True, exist_ok=True)
            with (d / 'comments.jsonl').open('a') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            return self._send(409, {'ok': False, 'error': f'round {version} is closed',
                                    'hint': f"the open round is {load_rounds().get('current')}",
                                    'stored': 'late/ (rejected, kept for the record)'})

        if self.path.startswith('/blend_trigger'):
            raw_version = data.get('version', '')          # validate BEFORE sanitizing
            version = safe(raw_version)
            pin = str(data.get('pin', '')).strip()
            who = safe(data.get('triggered_by'))
            try:
                real_pin = PIN_FILE.read_text().strip()
            except Exception:
                return self._send(500, {'ok': False, 'error': 'pin not configured on server'})
            if not pin or not hmac.compare_digest(pin, real_pin):   # constant-time
                time.sleep(0.5)                                     # crude throttle
                return self._send(403, {'ok': False, 'error': 'wrong PIN'})
            with _LOCK:
                rounds = load_rounds()
                if raw_version != rounds.get('current'):
                    return self._send(409, {'ok': False, 'error': 'can only close the current open round'})
                rr = rounds.setdefault('rounds', {}).setdefault(version, {})
                if rr.get('status') != 'open':
                    return self._send(409, {'ok': False, 'error': f'round {version} is not open'})
                rr['status'] = 'closed'
                rr['closed'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                rr['triggered_by'] = who
                tmp = ROUNDS.with_suffix('.tmp')
                tmp.write_text(json.dumps(rounds, indent=1)); os.replace(tmp, ROUNDS)
            cf = FEEDBACK / version / 'comments.jsonl'
            n = sum(1 for _ in open(cf)) if cf.exists() else 0
            dg = digest_round(version)
            (FEEDBACK / f'trigger_{version}.json').write_text(json.dumps(
                {'version': version, 'triggered_by': who, 'closed': rr['closed'],
                 'submissions': n, 'items': dg['total_items'],
                 'by_profile_and_column': dg['groups'],
                 'routing_note': 'each group lists routes_to = the pipeline target/rule family '
                                 'per column (matches the on-page How to review); profile scopes '
                                 'the change to the 6s or 10s pipeline.',
                 'work_order': 'distill feedback -> ledger candidates -> implement -> '
                               'gate (worst-of-3 + fixtures, all clips) -> dispositions '
                               '-> publish next version'}, indent=1, ensure_ascii=False))
            print(f"[trigger] {who} closed {version} ({n} submissions)")
            return self._send(200, {'ok': True, 'closed': version, 'submissions': n,
                                    'next': 'work order written; the next version will be built and announced'})

        return self._send(404, {'ok': False, 'error': 'not found'})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print(f'feedback server on 127.0.0.1:{PORT}')
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
