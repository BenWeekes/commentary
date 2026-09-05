"""Results page: Eros EN/zh vs our v7 AI vs the human broadcaster, on the 5-min clip.
Usage: python3 build_page.py [--mock]"""
import json, html, pathlib, statistics, sys
HERE=pathlib.Path(__file__).parent
MOCK='--mock' in sys.argv
AIC='/home/ubuntu/commentary/experiments/ai_commentator'
def jl(p): return [json.loads(l) for l in open(p) if l.strip()]
gold=jl(f'{AIC}/gold_soniox_5min.jsonl')
ours=jl(f'{AIC}/commentary_blend_live_eager_v7.jsonl')
if MOCK:
    en=[{'sequence':i,'source_pts_ms':int(t*1000),'priority':p,'text':x,'latency_ms':l}
        for i,(t,p,x,l) in enumerate([(9.1,2,'[MOCK] Bodies around the referee.',5100),
            (14.9,3,'[MOCK] Neither side can afford to lose.',6900),(188.4,0,'[MOCK] Yellow card for Kohn.',3900)])]
    zh=[e|{'text':'[MOCK] 中文解说样例'} for e in en if e['sequence']!=1]
else:
    en=jl(HERE/'subs_en.jsonl'); zh=jl(HERE/'subs_zh_CN.jsonl')

rows=[]
for g in gold: rows.append((g['start_s'],'human',g['text'],None,None))
for o in ours: rows.append((o['video_time_s'],'ours',o['text'],None,None))
for s in en: rows.append((s['source_pts_ms']/1000,'eros_en',s['text'],s.get('priority'),s.get('latency_ms')))
for s in zh: rows.append((s['source_pts_ms']/1000,'eros_zh',s['text'],s.get('priority'),s.get('latency_ms')))
rows.sort(key=lambda r:r[0])
seqs=sorted(s['sequence'] for s in en); zseqs={s['sequence'] for s in zh}
gaps=[q for q in (sorted({s['sequence'] for s in zh})) if q not in set(seqs)]
en_gaps=[q for q in zseqs if q not in set(seqs)]
lats=sorted(s.get('latency_ms',0) for s in en if s.get('latency_ms'))
def pct(v,p): return v[min(len(v)-1,int(len(v)*p))] if v else '—'
prio={p:sum(1 for s in en if s.get('priority')==p) for p in (0,1,2,3)}
stats=(f"Eros EN {len(en)} lines · zh-CN {len(zh)} lines · EN gaps (failed translations) {len(en_gaps)}"
       f" · latency p50 {pct(lats,.5)}ms / p95 {pct(lats,.95)}ms (claimed 5100/7000)"
       f" · priorities 0/1/2/3 = {prio[0]}/{prio[1]}/{prio[2]}/{prio[3]}"
       f" · ours {len(ours)} lines · human {len(gold)} turns")
def mmss(t): return f"{int(t//60)}:{int(t%60):02d}"
def esc(x): return html.escape(x or '')
body=''
for t,src,txt,pr,lat in rows:
    cells={'human':'','ours':'','eros_en':'','eros_zh':''}
    extra=f" <span class=m>p{pr} · {lat}ms</span>" if src=='eros_en' and pr is not None else ''
    cells[src]=esc(txt)+extra
    body+=(f"<tr><td><a href='#' onclick=\"v.currentTime={t:.1f};return false\">{mmss(t)}</a></td>"
           f"<td>{cells['human']}</td><td>{cells['ours']}</td>"
           f"<td class={'p'+str(pr) if pr is not None else ''}>{cells['eros_en']}</td><td>{cells['eros_zh']}</td></tr>\n")
banner=("<div class=warn>MOCK DATA — layout preview only; awaiting vendor tokens</div>" if MOCK else '')
page=f"""<meta charset=utf-8><title>Eros vendor test — Mainz vs Union 5-min clip</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13px system-ui;margin:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #2a2a2a;padding:4px 7px;vertical-align:top;font-size:12.5px}}
th{{background:#151515;position:sticky;top:0}}a{{color:#7dd3fc}}
video{{width:640px;display:block;margin:8px 0}}.m{{color:#64748b;font-size:10.5px}}
.warn{{background:#452a03;border:1px solid #b45309;padding:8px 12px;border-radius:6px;margin-bottom:10px}}
.p0{{background:#3b0d0d}}.p1{{background:#332008}}
#st{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:10px}}</style>
<h2>Eros (nextmoment.ai) subtitle test — Mainz vs Union Berlin, 76:50–81:50, 1-1</h2>
{banner}<div id=st>{stats}</div>
<video id=v src="/experiments/ai_commentator/blend_v7_10s/original.mp4" controls preload=metadata></video>
<p>Click a time to seek. Priorities: p0 official event (dark red) · p1 corroborated shot/save/goal (amber) · p2 on-ball · p3 colour.
Eros times are <code>source_pts_ms</code> (the moment described, not arrival). Human = broadcast STT (Soniox); Ours = v7 eager 10s pipeline.</p>
<table><tr><th style="width:46px">t</th><th>Human broadcaster</th><th>Our v7 AI</th><th>Eros EN</th><th>Eros zh-CN</th></tr>
{body}</table>"""
out=pathlib.Path('/var/www/html/experiments/ai_commentator/eros_test/index.html')
out.parent.mkdir(exist_ok=True)
out.write_text(page)
print(('MOCK ' if MOCK else '')+'page ->', out, f'| {len(rows)} rows')
