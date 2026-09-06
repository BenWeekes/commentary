**RESOLVED 2026-09-06** — ingest allowlisted, full test ran successfully.
Results: https://sa-dev.agora.io/experiments/ai_commentator/eros_test/
Measured: 35 lines/language, 0 translation gaps, latency p50 4.34 s / p95 4.99 s (beats the published p50 5.1 / p95 7.0). Report below kept for the record.

---

# Eros Live integration test — findings report

**From:** Agora integration test (Bundesliga 5-min clip, subtitle mode)
**Date:** 2026-09-05
**Environment:** production `https://live.nextmoment.ai`, docs v2026-09-04/05
**Status: blocked on video ingest — no commentary could be produced yet.**

## What works

- Both tokens authenticate correctly on their documented routes.
- `POST /v1/matches` (subtitle mode, `["en","zh-CN"]`, full match_package with squads,
  numbers, kit colours, formation, referee, mid-match kickoff state) → `201 READY`.
- `POST /v1/matches/{id}/arm` → `ARMED`, `stream_epoch` + ingest URL issued as documented.
- `GET /v1/matches` (list) reflects our matches and state transitions correctly.
- Subtitle polling authenticates and returns well-formed empty envelopes pre-live.
- `POST /v1/matches/{id}/end` works.

## Blocker — SRT ingest is unreachable (no handshake response)

Every publish attempt fails after ~3.1 s (libsrt's connection timeout): the SRT
handshake gets **no response of any kind**.

Evidence gathered from our side (egress IP **3.9.234.40**, London/AWS):

1. `ffmpeg -re … -f mpegts 'srt://34.85.178.237:8890?streamid=<issued>&pkt_size=1316'`
   (the `ffmpeg_url` from `arm`, used verbatim) → `Input/output error` after ~3.1 s.
2. Identical failure timing with the issued streamid, a deliberately wrong streamid,
   and no streamid at all → the streamid is never evaluated; the handshake itself
   receives no reply.
3. Packet capture during an attempt: **12 UDP handshake-induction packets sent to
   34.85.178.237:8890, 0 packets received back.**
4. Same-host control: the HTTPS API on the very same IP (34.85.178.237:443) responds
   normally throughout — the machine is up; UDP 8890 specifically is dark.
5. Our-side elimination: outbound UDP with ephemeral-port return traffic works from
   this host (verified via UDP DNS), so our NAT/firewall path is not the cause.
6. Reproduced across multiple freshly created+armed matches, immediately after arm
   and 20+ s after arm, over ~30 minutes.
7. The `ingest.ffmpeg_url` was used **verbatim** (its only parameters are the issued
   `streamid` and `pkt_size=1316`, exactly matching the doc's template), from libsrt
   1.5.4; a second, independent SRT client (`srt-live-transmit`) fails identically.
8. **TCP port 8890 is also filtered** on the same host while 443 answers — port 8890
   appears entirely unexposed, consistent with a missing/mis-scoped firewall rule for
   `udp:8890` (or an allowlist that does not include our IP) rather than anything
   protocol-level.

**Most likely causes, in order:** (a) an ingest-side firewall/allowlist dropping
unknown source IPs — if so, please allowlist `3.9.234.40`; (b) the SRT listener on
8890 not running/bound on the host that serves the API.

## Bug — `GET /v1/matches/{id}` returns 404 for every id

Documented under the match token's routes, but returns `{"error":"not found"}` (404)
for ids that `GET /v1/matches` lists in the same second — including matches created
before ours on this account. List works; direct fetch is broken for all ids we tried.
Low severity for us (the list suffices), but the doc and API disagree.

## Two questions

1. **Re-arm/recovery:** after a failed publish, `arm` on the same match returns
   `"only a READY match can be armed"`, and nothing moves a match back to `READY`.
   We recovered by `end` + creating a new match. Is that the intended flow when a
   publisher never connected?
2. **Pre-live polling:** polling an `ARMED` (not yet live) match returns `200` with an
   empty list rather than the documented `409 match is not live`. We prefer the
   current behaviour — just confirming it is intentional so we can rely on it.

## Ready on our side

The moment ingest accepts our handshake, the full test runs unattended: real-time
publish of a 720p Bundesliga clip, dual-language read-out, latency measurement
against your published p50 5.1 s / p95 7 s, and a results page comparing your lines
with the human broadcaster and our own pipeline. Happy to run it live on a call.
