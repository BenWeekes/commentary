#!/usr/bin/env python3
"""Build the six-column tennis review page with per-cell feedback."""
from __future__ import annotations

import html
import json
import errno
import os
import shutil
from pathlib import Path

from tennis_common import (
    ARTIFACTS,
    CLIP,
    CONFIG,
    DELAY_S,
    PROFILE,
    SHARED_ARTIFACTS,
    read_jsonl,
)

BASE = Path(__file__).resolve().parent
VERSION = CONFIG["version"]
PAGE_VERSION = f"{VERSION}_{PROFILE}"
ROOT = Path("/var/www/html/experiments/tennis_commentator") / PAGE_VERSION
COLS = ("STT", "Vision", "Tracker", "English", "French", "Portuguese")
MERGE = 1.2


def fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def pre_match() -> str:
    return """PRE-MATCH DATA — cutoff: start of June 29, 2026
=====================================================
Event: Cary Tennis Classic, ATP Challenger 75
Dates: June 29–July 5, 2026
Surface: outdoor hard court
Round: round of 32

Daniil Glinka (blue shirt; far end at clip start)
  Estonia · left-handed · age 26
  ATP No. 171 on June 22, 2026 · career high No. 167
  2026 record: 18–19 overall; 10–9 hard
  No. 3 seed in Cary
  2025 Drummondville Challenger champion

Aidan Mayo (grey shirt; near end at clip start)
  United States · right-handed · age 23 · prefers hard
  ATP No. 547 on June 22, 2026 · career high No. 269
  2026 record: 9–8 overall; 5–5 hard
  Cary entry by protected ranking
  2024 Drummondville Challenger champion

CLIP INITIAL STATE (verified from scoreboard)
  0–0 sets, 0–0 games, 0–0 points; Mayo serving.
  Players change ends after the opening game.

Sources are linked below. Match result and later information are excluded."""


def add(items: list, t: float, col: int, text: str, detail: str = "", badge: str = "") -> None:
    body = (f"<span class='badge'>{html.escape(badge)}</span>" if badge else "")
    body += html.escape(text)
    if detail:
        body += f"<span class='detail'>{html.escape(detail)}</span>"
    items.append((float(t), col, body))


def install_media(source: Path, destination: Path) -> None:
    """Atomically deploy by hard link when possible, copy across filesystems."""
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        try:
            os.link(source, temporary)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    gate_path = ARTIFACTS / "gate.json"
    if not gate_path.exists() or json.loads(gate_path.read_text()).get("status") != "PASS":
        raise SystemExit("review page requires a passing gate.json")
    ROOT.mkdir(parents=True, exist_ok=True)
    media = {
        "Original": CLIP,
        "AI · English": ARTIFACTS / "review_en.mp4",
        "AI · French": ARTIFACTS / "review_fr.mp4",
        "AI · Portuguese": ARTIFACTS / "review_pt.mp4",
    }
    for label, source in media.items():
        if not source.exists():
            raise SystemExit(f"missing review media: {source}")
        install_media(
            source,
            ROOT / (
                "original.mp4"
                if label == "Original"
                else "ai_" + label.split()[-1].lower() + ".mp4"
            ),
        )
    items = []
    stt_path = SHARED_ARTIFACTS / "stt_merged.jsonl"
    for row in read_jsonl(
        stt_path if stt_path.exists() else SHARED_ARTIFACTS / "stt.jsonl"
    ):
        add(items, row["video_time_s"], 0, row["text"],
            f"provider={row.get('provider', 'unknown')}",
            badge=f"{row['conf']:.2f}")
    for row in read_jsonl(SHARED_ARTIFACTS / "detections.jsonl"):
        det = row["detection"]
        add(items, row["video_time_s"], 1, det.get("observation") or "(no literal read)",
            f"phase={det.get('phase')}; live confidence={det.get('live_play_confidence')}")
    for row in read_jsonl(ARTIFACTS / "tracker.jsonl"):
        add(items, row["video_time_s"], 2, row["text"], row["detail"],
            "accepted" if row["accepted"] else "held")
    for row in read_jsonl(ARTIFACTS / "commentary_attempt_1.jsonl"):
        if row.get("dropped"):
            continue
        intent = row.get("intent") or {}
        policy = row.get("policy") or {}
        placements = row.get("placements") or {}
        english_placement = placements.get("en") or {}
        detail = (
            f"intent={intent.get('code', 'unknown')}; "
            f"phase={intent.get('state_phase', 'unknown')}; "
            f"policy={policy.get('reason', 'unknown')}; "
            f"audio shift={english_placement.get('shift_s', 'n/a')}s"
        )
        add(items, row["video_time_s"], 3, row["text"], detail)
        add(items, row["video_time_s"], 4, row["fr"])
        add(items, row["video_time_s"], 5, row["pt"])
    items.sort(key=lambda item: item[0])
    rows = []
    for t, col, text in items:
        if not rows or t - rows[-1]["anchor"] > MERGE:
            rows.append({"t": t, "anchor": t, "cells": [[] for _ in COLS]})
        rows[-1]["cells"][col].append(text)
    body = ""
    for row in rows:
        cells = ""
        for values in row["cells"]:
            content = "".join(f"<div class='line'>{value}</div>" for value in values)
            cells += f"<div class='cell'>{content or '<div class=hole></div>'}</div>"
        body += f"<div class='row' data-t='{row['t']:.2f}'><div class='time'>{fmt(row['t'])}</div>{cells}</div>"
    sources = "".join(
        f"<li><a href='{html.escape(item['url'])}' target='_blank'>{html.escape(item['label'])}</a></li>"
        for item in CONFIG["sources"]
    )
    merged_stt = read_jsonl(SHARED_ARTIFACTS / "stt_merged.jsonl")
    rejected_stt = read_jsonl(SHARED_ARTIFACTS / "stt_rejected.jsonl")
    stt_note = (
        f"STT audit: {len(merged_stt)} merged utterance(s); "
        f"{len(rejected_stt)} comparison segments rejected by sanity checks."
    )
    page = TEMPLATE.replace("__ROWS__", body).replace("__PREMATCH__", html.escape(pre_match()))
    page = (
        page.replace("__SOURCES__", sources)
        .replace("__VERSION__", PAGE_VERSION)
        .replace("__ROUND__", VERSION)
        .replace("__PROFILE__", PROFILE)
        .replace("__DELAY__", f"{DELAY_S:g}")
        .replace("__STT_NOTE__", html.escape(stt_note))
    )
    (ROOT / "index.html").write_text(page)
    print(f"wrote {ROOT / 'index.html'} ({len(rows)} timeline rows)")


TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Tennis commentator — __ROUND__ ready for review</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#090b0f;color:#e5e7eb;font:13px system-ui,sans-serif;padding:14px 14px 62px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#9ca3af;margin:0 0 10px}.top{background:#090b0f;position:sticky;top:0;z-index:5;border-bottom:1px solid #252a33;padding-bottom:10px}
.hero{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}video{width:min(640px,95vw);background:#000;border-radius:7px}
.side{width:340px;max-width:95vw}.box{border:1px solid #293241;border-radius:7px;background:#0e141d;padding:9px;margin-bottom:8px;line-height:1.45}
.box h2{font-size:11px;color:#7dd3fc;text-transform:uppercase;margin:0 0 5px}.prematch{height:248px;overflow:auto;white-space:pre-wrap;font:11px ui-monospace,monospace;color:#cbd5e1}
.sources{font-size:11px;margin:5px 0 0;padding-left:18px}.sources a{color:#7dd3fc}.tabs{display:flex;gap:7px;justify-content:center;margin-top:8px}
.tabs button{border:1px solid #334155;border-radius:16px;background:#111827;color:#cbd5e1;padding:6px 12px;cursor:pointer}.tabs .active{border-color:#16a34a;color:#86efac}
.wrap{margin-top:12px;border:1px solid #20242c;border-radius:7px;overflow-x:auto}.grid{min-width:1240px}.head,.row{display:grid;grid-template-columns:48px repeat(6,1fr)}
.head{background:#151922;position:sticky;top:0;z-index:2}.head>div{padding:8px;font-size:11px;text-transform:uppercase;font-weight:700}.head>div:nth-child(2){color:#fbbf24}.head>div:nth-child(3){color:#c4b5fd}.head>div:nth-child(4){color:#38bdf8}.head>div:nth-child(n+5){color:#6ee7b7}
.scroll{max-height:72vh;overflow-y:auto}.row{border-top:1px solid #171a20;cursor:pointer}.row:hover,.row.active{background:#122036}.time,.cell{padding:6px 7px;border-right:1px solid #171a20}.time{font:10px ui-monospace,monospace;color:#64748b}.cell{position:relative;line-height:1.4}.line+.line{border-top:1px dashed #252a33;margin-top:4px;padding-top:4px}
.detail{display:block;color:#6b7280;font-size:10px}.badge{color:#86efac;background:#12351f;border-radius:8px;padding:0 5px;margin-right:5px;font:9px ui-monospace,monospace}.hole{height:6px}
.cell:hover{outline:1px dashed #22c55e}.cell.pending{box-shadow:inset 3px 0 #f59e0b}.cell.sent{box-shadow:inset 3px 0 #22c55e}
.editor{background:#101827;border:1px solid #245181;border-radius:6px;padding:7px;margin-top:5px}.editor textarea{width:100%;min-height:45px;background:#080d15;color:#e5e7eb;border:1px solid #334155}.tags{display:flex;gap:4px;flex-wrap:wrap;margin:5px 0}.tags span{border:1px solid #475569;border-radius:9px;padding:1px 6px;font-size:10px}.tags .on{background:#1e3a5f}.actions{text-align:right}.actions button{margin-left:5px}
.bar{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #245181;z-index:10;display:flex;gap:9px;align-items:center;justify-content:center;padding:8px}.bar input{background:#080d15;color:white;border:1px solid #334155;padding:5px}.bar button{border:0;border-radius:4px;padding:6px 12px}.submit{background:#14532d;color:#bbf7d0}.trigger{background:#450a0a;color:#fecaca}.status{color:#94a3b8}
</style></head><body>
<div class="top"><h1>AI Tennis commentator — __ROUND__ ready for review</h1>
<p class="sub">Exact five-minute clip from 02:00:15 · __DELAY__-second fixed-delay profile · same six review columns as football. Click any cell to review it. __STT_NOTE__</p>
<div class="hero"><div class="side"><div class="box"><h2>How to review</h2>English is the grounded commentary. French and Portuguese review only their localization. STT, Vision, and Tracker review the inputs. All points require a disposition before another version can publish.</div>
<div class="box prematch">__PREMATCH__</div><div class="box"><h2>Official sources</h2><ul class="sources">__SOURCES__</ul></div></div>
<video id="video" controls preload="metadata" src="./ai_english.mp4"></video></div>
<div class="tabs"><button data-src="./original.mp4">Original broadcast</button><button class="active" data-src="./ai_english.mp4">AI · English</button><button data-src="./ai_french.mp4">AI · French</button><button data-src="./ai_portuguese.mp4">AI · Portuguese</button></div></div>
<div class="wrap"><div class="grid"><div class="head"><div>time</div><div>STT</div><div>Vision</div><div>Tracker</div><div>English</div><div>French</div><div>Portuguese</div></div><div class="scroll" id="scroll">__ROWS__</div></div></div>
<div class="bar"><span>Reviewer</span><input id="reviewer" placeholder="your name"><span id="count">0 unsent</span><button class="submit" id="submit">Submit feedback</button><button class="trigger" id="trigger">Close round & trigger next version</button><span class="status" id="status"></span></div>
<script>
const VERSION="__ROUND__",PROFILE="__PROFILE__",CLIP="glinka_mayo_cary_2026_12015_5m",COLS=["STT","Vision","Tracker","English","French","Portuguese"];
const video=document.getElementById("video"),scroll=document.getElementById("scroll"),pending=new Map(),tags=["wrong fact","score","identity","timing","repetition","language","👍 good"];
const reviewer=document.getElementById("reviewer"),status=document.getElementById("status");reviewer.value=localStorage.tennis_reviewer||"";reviewer.onchange=()=>localStorage.tennis_reviewer=reviewer.value;
fetch("/tennis_rounds").then(r=>r.json()).then(j=>{const state=j.rounds&&j.rounds[VERSION];if(!state||state.status!=="open")status.textContent=`round ${VERSION} is closed; current: ${j.current||"none"}`}).catch(()=>status.textContent="feedback status unavailable");
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tabs button").forEach(x=>x.classList.remove("active"));b.classList.add("active");const t=video.currentTime,p=!video.paused;video.src=b.dataset.src;video.onloadedmetadata=()=>{video.currentTime=t;if(p)video.play()}});
const rows=[...document.querySelectorAll(".row")];rows.forEach(r=>r.onclick=e=>{if(e.target.closest(".editor"))return;video.currentTime=Math.max(0,+r.dataset.t-.3);video.play()});
let active=null;video.ontimeupdate=()=>{let next=null;for(const row of rows){if(+row.dataset.t<=video.currentTime+.05)next=row;else break}if(next!==active){if(active)active.classList.remove("active");if(next)next.classList.add("active");active=next}};
function count(){document.getElementById("count").textContent=pending.size+" unsent"}function close(){document.querySelectorAll(".editor").forEach(x=>x.remove())}
document.querySelectorAll(".row .cell").forEach(cell=>cell.onclick=e=>{e.stopPropagation();if(e.target.closest(".editor"))return;close();const row=cell.closest(".row"),col=[...row.querySelectorAll(".cell")].indexOf(cell),old=pending.get(cell),snapshot=old?old.cell_text:cell.innerText.slice(0,400),box=document.createElement("div");box.className="editor";box.innerHTML=`<textarea placeholder="comment on this cell"></textarea><div class="tags">${tags.map(x=>`<span>${x}</span>`).join("")}</div><div class="actions">${old?'<button class="remove">Remove</button>':""}<button class="add">${old?"Update":"Add"}</button></div>`;if(old){box.querySelector("textarea").value=old.comment;box.querySelectorAll(".tags span").forEach(x=>x.classList.toggle("on",old.tags.includes(x.textContent)))}box.querySelectorAll(".tags span").forEach(x=>x.onclick=()=>x.classList.toggle("on"));box.querySelector(".add").onclick=()=>{const comment=box.querySelector("textarea").value.trim(),selected=[...box.querySelectorAll(".tags .on")].map(x=>x.textContent);if(!comment&&!selected.length)return;pending.set(cell,{t:+row.dataset.t,column:COLS[col],profile:PROFILE,clip:CLIP,cell_text:snapshot,tags:selected,comment});cell.classList.add("pending");count();close()};if(old)box.querySelector(".remove").onclick=()=>{pending.delete(cell);cell.classList.remove("pending");count();close()};cell.appendChild(box)});
document.getElementById("submit").onclick=()=>{if(!reviewer.value.trim()){status.textContent="enter your name";return}const snap=new Map(pending),items=[...snap.values()];if(!items.length){status.textContent="nothing to submit";return}fetch("/tennis_feedback",{method:"POST",body:JSON.stringify({reviewer:reviewer.value,version:VERSION,items})}).then(async r=>[r.status,await r.json()]).then(([s,j])=>{if(s===200&&j.stored===items.length){snap.forEach((v,c)=>{if(pending.get(c)===v){pending.delete(c);c.classList.remove("pending");c.classList.add("sent")}});count();status.textContent=`${j.stored} submitted`}else status.textContent=j.error||"server did not confirm every item"}).catch(()=>status.textContent="network error")};
document.getElementById("trigger").onclick=()=>{if(pending.size){status.textContent="submit comments first";return}if(!reviewer.value.trim()){status.textContent="enter your name";return}if(!confirm("Close this tennis review round?"))return;const pin=prompt("Trigger PIN");if(!pin)return;fetch("/tennis_trigger",{method:"POST",body:JSON.stringify({version:VERSION,pin,triggered_by:reviewer.value})}).then(async r=>[r.status,await r.json()]).then(([s,j])=>status.textContent=s===200?`round closed; ${j.items} items queued`:j.error||"failed")};
</script></body></html>"""


if __name__ == "__main__":
    main()
