## Live Ops Notes

### Stale `lineups_fetch_failed` on running matches

The status UI can keep showing `lineups_fetch_failed` even after a later
Sportradar refresh succeeds and writes full keyterms to disk. The scheduler
stores `last_error` but does not clear it on a successful refresh, so the UI
can show a stale failure beside a healthy running match.

The live worker loads keyterms only at startup. To verify what a running match
is actually using, inspect the run header in:

```text
match_data/{match_id}/runs/{run_id}/stt.jsonl
```

The header contains the loaded `keyterms` array. If the header has only the
fallback/team terms and the disk keyterms later contain the full lineup,
restart that match before kickoff if possible. Do not restart a healthy match
for a stale `last_error` alone.

Follow-up fix: clear `MatchSchedule.last_error` on successful scheduler
refresh, and consider surfacing `loaded_keyterm_count` from the active worker
separately from the latest on-disk keyterm count.
