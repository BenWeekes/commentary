"""Sportradar data fetch, keyterms derivation, and per-match refresh.

Used by the refresh-data API and (later) the scheduler to keep
per-match metadata fresh before kickoff.
"""

import json
import os
import threading
import time
import urllib.request

SPORTRADAR_BASE_URL = "https://api.sportradar.com/soccer-extended/trial/v4/en"


def _sr_get(path: str, api_key: str, timeout: int = 15):
    """GET a Sportradar endpoint. Returns parsed JSON or None on error."""
    url = f"{SPORTRADAR_BASE_URL}/{path}"
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[SR] GET {path} failed: {e}")
        return None


def fetch_lineups(sport_event_id: str, api_key: str) -> dict | None:
    """Fetch lineups for a sport event. Returns raw API response or None."""
    return _sr_get(f"sport_events/{sport_event_id}/lineups.json", api_key)


def fetch_summary(sport_event_id: str, api_key: str) -> dict | None:
    """Fetch match summary. Returns raw API response or None."""
    return _sr_get(f"sport_events/{sport_event_id}/summary.json", api_key)


def derive_roster(lineups: dict) -> str | None:
    """Build a roster string for the translation prompt.

    Format matches generate_demo_transcript.py — includes venue, referees,
    managers, starting XI with numbers, substitutes.
    Returns None if lineups data is unusable.
    """
    if not lineups:
        return None

    lines = []
    se = lineups.get("sport_event", {})

    # Venue
    venue = se.get("venue", {})
    if venue.get("name"):
        city = venue.get("city_name", "")
        lines.append(f"Venue: {venue['name']}" + (f", {city}" if city else ""))

    # Referees (SR uses "Last, First" format)
    refs = se.get("sport_event_conditions", {}).get("referees", [])
    if refs:
        ref_names = [_flip_sr_name(r["name"]) for r in refs if r.get("name")]
        if ref_names:
            lines.append(f"Referees: {', '.join(ref_names)}")

    # Teams — from lineups.competitors (more detailed than sport_event.competitors)
    lu = lineups.get("lineups", {})
    for team in lu.get("competitors", []):
        tname = team.get("name", "?")
        abbr = team.get("abbreviation", "?")
        qualifier = team.get("qualifier", "?")
        lines.append(f"\n{tname} ({abbr}) — {qualifier}:")

        mgr = team.get("manager", {})
        if mgr.get("name"):
            # SR format is "Last, First" — flip it
            parts = mgr["name"].split(", ", 1)
            mgr_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else mgr["name"]
            lines.append(f"  Manager: {mgr_name}")

        players = team.get("players", [])
        starting = [p for p in players if p.get("starter")]
        subs = [p for p in players if not p.get("starter")]

        if starting:
            lines.append("  Starting XI:")
            for p in sorted(starting, key=lambda x: x.get("jersey_number", 99)):
                lines.append(f"    #{p.get('jersey_number', '?')} {p.get('name', '?')}")
        if subs:
            lines.append("  Substitutes:")
            for p in sorted(subs, key=lambda x: x.get("jersey_number", 99)):
                lines.append(f"    #{p.get('jersey_number', '?')} {p.get('name', '?')}")

    roster = "\n".join(lines)
    return roster if roster.strip() else None


def derive_keyterms(lineups: dict, summary: dict | None = None) -> list[str]:
    """Derive STT keyterms from SR data.

    Extracts:
    - Team names (full + abbreviation)
    - Player names (full name + surname only)
    - Manager names (full + surname)
    - Venue name + city
    - Competition name
    - Referee names
    """
    terms = set()

    if not lineups:
        return []

    se = lineups.get("sport_event", {})

    # Competition
    comp = se.get("sport_event_context", {}).get("competition", {})
    if comp.get("name"):
        terms.add(comp["name"])

    # Venue
    venue = se.get("venue", {})
    if venue.get("name"):
        terms.add(venue["name"])
    if venue.get("city_name"):
        terms.add(venue["city_name"])

    # Referees (SR uses "Last, First" format)
    refs = se.get("sport_event_conditions", {}).get("referees", [])
    for ref in refs:
        raw = ref.get("name", "")
        if raw:
            name = _flip_sr_name(raw)
            terms.add(name)
            surname = _surname(name)
            if surname and surname != name:
                terms.add(surname)

    # Teams — use both sport_event.competitors and lineups.competitors
    for source in [se.get("competitors", []),
                   lineups.get("lineups", {}).get("competitors", [])]:
        for team in source:
            if team.get("name"):
                terms.add(team["name"])
            if team.get("abbreviation"):
                terms.add(team["abbreviation"])

    # Players and managers from lineups
    for team in lineups.get("lineups", {}).get("competitors", []):
        mgr = team.get("manager", {})
        if mgr.get("name"):
            mgr_name = _flip_sr_name(mgr["name"])
            terms.add(mgr_name)
            surname = _surname(mgr_name)
            if surname and surname != mgr_name:
                terms.add(surname)

        for player in team.get("players", []):
            name = player.get("name", "")
            if name:
                terms.add(name)
                surname = _surname(name)
                if surname and surname != name:
                    terms.add(surname)

    # Summary can add season/round info
    if summary:
        ss = summary.get("sport_event", {})
        ctx = ss.get("sport_event_context", {})
        season = ctx.get("season", {})
        if season.get("name"):
            terms.add(season["name"])
        rnd = ctx.get("round", {})
        if rnd.get("name"):
            terms.add(rnd["name"])

    # Sort for deterministic output
    return sorted(terms)


def _surname(name: str) -> str:
    """Extract surname from a full name. Returns empty string if single word."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return parts[-1]
    return ""


def _flip_sr_name(name: str) -> str:
    """Convert SR 'Last, First' format to 'First Last'."""
    parts = name.split(", ", 1)
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return name


# ── Refresh service ──────────────────────────────────────────────────

_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_lock = threading.Lock()


def _get_refresh_lock(match_id: str) -> threading.Lock:
    with _refresh_locks_lock:
        if match_id not in _refresh_locks:
            _refresh_locks[match_id] = threading.Lock()
        return _refresh_locks[match_id]


def refresh_match_data(match_id, match_cfg, match_store, api_key: str) -> dict:
    """Refresh SR data for a match and persist to match_store.

    Returns a summary dict with status, counts, and timestamps.
    Concurrent refresh for the same match is blocked (returns immediately).
    """
    lock = _get_refresh_lock(match_id)
    if not lock.acquire(blocking=False):
        return {"status": "already_refreshing", "match_id": match_id}

    try:
        result = {"status": "ok", "match_id": match_id}
        seid = match_cfg.sport_event_id
        if not seid:
            return {"status": "no_sport_event_id", "match_id": match_id}

        # Fetch lineups (primary source for roster + keyterms)
        lineups = fetch_lineups(seid, api_key)
        if not lineups:
            return {"status": "lineups_fetch_failed", "match_id": match_id}

        # Fetch summary (optional, adds season/round info)
        summary = fetch_summary(seid, api_key)

        # Derive and persist keyterms
        keyterms = derive_keyterms(lineups, summary)
        if keyterms:
            match_store.write_keyterms(match_id, keyterms)
        result["keyterm_count"] = len(keyterms)

        # Derive and persist roster
        roster = derive_roster(lineups)
        if roster:
            match_store.write_roster(match_id, {"roster_text": roster})
        result["roster_player_count"] = _count_players(lineups)

        # Cache raw SR responses
        cache = {"lineups": lineups}
        if summary:
            cache["summary"] = summary
        cache["fetched_at"] = time.time()
        match_store.write_sr_cache(match_id, cache)

        # Extract kickoff if available
        kickoff_utc = _extract_kickoff(lineups, summary)
        result["kickoff_utc"] = kickoff_utc

        # Update match metadata
        now = time.time()
        meta = match_store.read_match_meta(match_id) or {}
        meta.update({
            "last_refresh_at": now,
            "last_refresh_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "keyterm_count": len(keyterms),
            "roster_player_count": result["roster_player_count"],
        })
        if kickoff_utc:
            meta["kickoff_utc"] = kickoff_utc
        match_store.write_match_meta(match_id, meta)

        result["last_refresh_at"] = now
        result["last_refresh_iso"] = meta["last_refresh_iso"]
        return result

    except Exception as e:
        print(f"[SR] refresh_match_data({match_id}) error: {e}")
        return {"status": "error", "match_id": match_id, "error": str(e)}
    finally:
        lock.release()


def _count_players(lineups: dict) -> int:
    count = 0
    for team in lineups.get("lineups", {}).get("competitors", []):
        count += len(team.get("players", []))
    return count


def _extract_kickoff(lineups: dict, summary: dict | None) -> str | None:
    """Extract kickoff time from SR data. Returns ISO string or None."""
    # Try summary first (has match status)
    for source in [summary, lineups]:
        if not source:
            continue
        se = source.get("sport_event", {})
        start_time = se.get("start_time")
        if start_time:
            return start_time
    return None
