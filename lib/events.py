def load_events_file(filepath):
    """Parse a Sportradar events file.

    Format: ``offset|PRIORITY|message`` where offset is seconds or ``mm:ss``.
    Returns list of ``(offset_seconds, priority, message)`` tuples.
    """
    events = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            ts = parts[0]
            if ':' in ts:
                mm, ss = ts.split(':')
                offset = int(mm) * 60 + int(ss)
            else:
                offset = int(ts)
            events.append((offset, parts[1], parts[2]))
    return events
