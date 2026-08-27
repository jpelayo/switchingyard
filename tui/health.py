"""Failures the router counts, sampled and kept so they can be looked at later.

The router reports faults as OpenTelemetry counters and stats fields. Two
problems with that as the only record: the counters reset when it restarts, and
nothing reads them unless someone is looking at the right screen at the right
moment. A judge that has been failing on every request since this morning shows
up as a number that says nothing about when it started.

So the accounting sidecar samples them on its loop and stores the *increases*
with a timestamp. What you get is a history — "this began at 16:02 and has
happened 340 times since" — rather than a gauge.

Nothing here is derived from the request log: these are exactly the failures the
request log cannot see, because they happen after the response headers or before
a call is ever billed.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

# Prometheus lines look like:
#   switchyard_classifier_fail_open_total{judge_model="x",reason="timeout"} 3
_METRIC = re.compile(r'^(?P<name>[a-z_]+)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.]+)$')

# What a counter's `reason` label means, in the operator's terms rather than the
# transport's. A 404 from OpenRouter is by far the most common and the least
# self-explanatory, so it says where to go.
REASON_HELP = {
    "upstream_non_5xx": "the provider refused the call — commonly HTTP 404, "
                        "'no endpoints matching your data policy' "
                        "(openrouter.ai/settings/privacy)",
    "upstream_5xx": "the provider failed on its side",
    "timeout": "the judge did not answer in time",
    "transport": "the call never reached the provider",
    "invalid_response": "the judge answered with something unusable",
    "parse_error": "the verdict was not valid JSON — often an empty reply",
    "client_error": "the call failed before the provider saw it",
}


def base_url(candidates=("http://localhost:4000", "http://switchingyard:4000",
                         "http://127.0.0.1:4000")) -> str | None:
    for base in candidates:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=3):
                return base
        except Exception:
            continue
    return None


def sample(base: str | None = None) -> dict[tuple[str, str, str], int]:
    """Current counter values, keyed by (kind, subject, reason)."""
    base = base or base_url()
    out: dict[tuple[str, str, str], int] = {}
    if not base:
        return out
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as r:
            for line in r.read().decode("utf-8", "replace").splitlines():
                if line.startswith("#"):
                    continue
                m = _METRIC.match(line.strip())
                if not m or "classifier_fail_open" not in m.group("name"):
                    continue
                labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group("labels")))
                out[("judge fail-open", labels.get("judge_model", "?"),
                     labels.get("reason", "?"))] = int(float(m.group("value")))
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"{base}/v1/stats", timeout=5) as r:
            d = json.load(r)
        for model, st in (d.get("models") or {}).items():
            if st.get("errors"):
                out[("broken stream", model, "stream ended early")] = int(st["errors"])
        for kind, n in (d.get("routing_fallbacks") or {}).items():
            if n:
                out[("routing fallback", "-", kind)] = int(n)
    except Exception:
        pass
    return out


def record(con, now: datetime | None = None, base: str | None = None) -> int:
    """Store what has happened since the last sample. Returns rows written.

    Counters only go up, and reset to zero when the router restarts. A value
    lower than last time is therefore a restart, not a decrease — the new value
    is taken whole rather than treated as a negative delta.
    """
    now = now or datetime.now(timezone.utc)
    current = sample(base)
    if not current:
        return 0
    row = con.execute("SELECT value FROM meta WHERE key='failure_counters'").fetchone()
    previous = {}
    if row:
        try:
            previous = {tuple(k.split("\x1f")): v for k, v in json.loads(row["value"]).items()}
        except Exception:
            previous = {}
    written = 0
    with con:
        for key, value in current.items():
            was = previous.get(key, 0)
            delta = value if value < was else value - was
            if delta <= 0:
                continue
            kind, subject, reason = key
            con.execute("INSERT INTO failure(ts, kind, subject, reason, count) "
                        "VALUES(?,?,?,?,?)",
                        (now.isoformat(timespec="seconds"), kind, subject, reason, delta))
            written += 1
        con.execute("INSERT INTO meta(key, value) VALUES('failure_counters', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({"\x1f".join(k): v for k, v in current.items()}),))
    return written


def recent(con, hours: int = 24) -> list[dict]:
    """Failures in the last `hours`, most recent first, grouped by what they are."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return [dict(r) for r in con.execute(
        "SELECT kind, subject, reason, SUM(count) total, MIN(ts) first_seen, "
        "       MAX(ts) last_seen "
        "FROM failure WHERE ts >= ? "
        "GROUP BY kind, subject, reason ORDER BY last_seen DESC", (since,))]


def prune(con, keep_days: int = 7) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    con.execute("DELETE FROM failure WHERE ts < ?", (cutoff,))
    con.commit()
