# Sign-Language Signer Overlay — transparent burn-in pipeline

> Added 2026-09-05. How we composite a transparent ASL signer (Signapse "JAY") over any
> match/interview video and ship a single self-contained MP4. Three published examples under
> `https://sa-dev.agora.io/experiments/ai_commentator/sign/`. Build dirs (resumable scripts +
> cached clips): `experiments/ai_commentator/sign_build/` (football, AI commentary + silence
> cutting), `sign_build_al/` (interview, Soniox STT), `sign_build_aic/` (football clip via STT).

## Why chroma-key (the transparency trick)

Signapse cannot return browser/ffmpeg-friendly alpha video (transparency only ships as
ProRes 4444). So we request each signing clip on a **solid green background** and key it out
ourselves. (The live web overlay in `github.com/BenWeekes/sign-video-client` uses the same idea in
WebGL; that repo is NOT needed here — the pipeline is self-contained in `sign_build*/`.)

1. **Generate clips green**: `POST https://ai.api.production.signapsesolutions.com/v2/generate`
   with `X-API-KEY` and body
   `{"content":{"type":"text","data":<text>},"output":{"format":"mp4","delivery":{"method":"download",
   "config":{"digitalSigner":"JAY","language":"ASL","backgroundColor":"#00FF00"}}},"context":{"application":"media"}}`.
   ⚠️ `config` MUST be nested inside `output.delivery` — as a sibling it is silently ignored.
   The 303 redirect leads to the MP4 (Python `urllib` follows it: the response body IS the file
   — check `data[4:8]==b'ftyp'`, don't parse JSON).
2. **Key it in ffmpeg** (per clip, before overlay):
   `crop=iw*0.62:ih:iw*0.19:0,chromakey=0x00FF00:0.13:0.06,despill=type=green`
   — crop tightens the signer (he occupies the middle ~55% of the 1920×1080 frame), chromakey
   removes green (similarity .13, blend .06 → no fringe, eye-whites safe), despill de-greens
   edge pixels. Then `scale=W:-2` (football 1280×720 used W=380–460; 640×360 videos W=190).
3. **Overlay bottom-right**: `overlay=x=W-w+22:y=H-h` (small positive x-offset eats the
   transparent margin so he hugs the edge).

## Signapse gotchas (hard-won)

- **~45 s server gateway timeout**: generation time scales with text length AND their load; a
  22-word sentence 408s under load while 4 words takes ~30 s. There is NO client-side control.
  **Fix: chunk every request to ≤8 words** (split at sentence ends, then commas nearest the
  middle), generate per-chunk, then `ffmpeg -f concat -c copy` the chunks into one clip per
  line. This is also what the live web client does implicitly (it sends sentences).
- Transient 408/503 waves: retry each chunk up to ~10× with 8–15 s sleeps, sequentially (a
  parallel burst makes their rate limiting worse).
- Replacing `—` with `,` avoids odd renders; identical text is served from their cache (fast).
- Key lives in `.env` as `SIGNAPSE_API_KEY`.

## The idle signer + the ghost-arms bug

A pre-baked green-background idle loop (`sign_build/assets/idle-jay-asl-green.mp4`) keeps the signer on screen between lines. **Never leave the idle
layer enabled underneath an active signing clip** — the keyed clip is transparent, so the
idle's static arms show through behind the moving ones. The idle overlay gets
`enable='if(<sum of between(t,start,end) for every signing window>,0,1)'`.

## Sync & realism

- Each line's signing clip is placed with `setpts=PTS-STARTPTS+<t>/TB` and
  `overlay ... enable='between(t,start,end)'`; `end` is capped at the next line's start
  (stay-in-step beats completeness — same drop-late philosophy as the commentary pipeline).
- **Interpreter lag**: real signers trail the audio; we add **+4 s** to every start time
  (capped near video end). Reviewed as markedly more realistic than exact sync.
- Line timings come from either the commentary pipeline's own jsonl (`video_time_s`) or, for
  arbitrary videos, **Soniox stt-rt-v5** on the extracted 16 kHz mono PCM: pace uploads at
  ≤2× real time (4× starves their keepalive), end with an empty-string message, treat socket
  close OR `finished` as end, drop `<end>/<fin>/<endpoint>` marker tokens, group subword
  tokens into words (tokens starting with a space begin a new word), split lines at sentence
  punctuation or >1.5 s gaps, merge fragments <4 words.

## Encoding without killing the box

43 clips = 45 ffmpeg inputs in one graph → OOM (rc -9) on a 4-core/15G box. Batch the
overlays: ≤6–9 clips per pass, near-lossless intermediates (crf 15), final pass crf 20, then
mux audio (`-c:v copy`). Run everything `nice -n 15` with `-threads 2`, strictly one process
at a time. Trim/concat of one input inside the same graph needs `split` — simpler to do the
silence-cut as its own first pass (`select='between(t,a,b)+...',setpts=N/25/TB`).

## Ops lessons

- Keep build dirs OUT of /tmp — a session scratchpad wipe cost a full regeneration. The
  `sign_build*/` dirs are durable and every phase is cached/resumable (rerun continues).
- `pkill -f <pattern>` from an agent shell can match its own wrapper command line and
  self-kill (exit 144): kill by PID, or use a `[b]racketed` pattern.
- Killing a python parent does not kill its ffmpeg child — sweep both.
