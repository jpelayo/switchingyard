"""Cost accounting derived from Switchyard's routing log.

Switchyard records tokens, never cost. This prices each routing-log record
against the cached OpenRouter catalogue and keeps hour buckets in SQLite, so
the raw log can be rotated without losing history.

The checkpoint and the aggregates commit in one transaction: an interrupted
ingest can never double-count.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store
import upstream

NANO = 1_000_000_000          # cost is stored as integer nanodollars
DEFAULT_MAX_LOG_BYTES = 1024 ** 3     # 1 GiB
REJECT_CAP = 500

TOKEN_COLUMNS = ("prompt_tokens", "cached_tokens", "cache_creation_tokens",
                 "completion_tokens", "reasoning_tokens")


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
# The tables live in tui/store.py with everything else: one database holds the
# whole deployment, so there is one place that knows its shape.


def connect(db_path: Path) -> sqlite3.Connection:
    return store.connect(db_path)


def _meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def load_prices(con) -> dict[str, dict]:
    """Per-token prices by model id, from the cached catalogue in this database.

    Used to be a glob over `models.*.json` beside the database — which the
    sidecar was pointing at the wrong name, so it priced nothing at all. Reading
    the table it already has open cannot go stale in that way.
    """
    return store.prices(con)


def cost_nano(record_tokens: dict, price: dict) -> int:
    """Cost in integer nanodollars.

    Token-based only. Image-generating models also charge per image, and the
    routing log carries no image count, so image-output spend is undercounted
    here and is visible only on the provider's own dashboard.

    prompt_tokens is already cache-exclusive upstream (codecs subtract the cache
    detail fields), so the terms do not overlap.

    reasoning_tokens are NOT added: they are a subset of completion_tokens, not a
    sibling of it. The openai_chat codec reads output_tokens from
    `completion_tokens` and reasoning_tokens from
    `completion_tokens_details.reasoning_tokens` (`buffered.rs:1232-1240`), which
    is OpenAI's nesting and the one OpenRouter follows. Adding them charged
    reasoning twice — invisible while nothing reasoned much, and close to double
    for a model running at a high effort. Rows aggregated before this was fixed
    keep the old arithmetic; only a re-ingest restates them.
    """
    total = (record_tokens["prompt_tokens"] * price["prompt"]
             + record_tokens["cached_tokens"] * price["cache_read"]
             + record_tokens["cache_creation_tokens"] * price["cache_write"]
             + record_tokens["completion_tokens"] * price["completion"])
    return round(total * NANO)


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------

def _hour(ts: str) -> str:
    """RFC3339 -> 'YYYY-MM-DDTHH' in UTC."""
    t = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        dt = datetime.fromisoformat(t[:26] + "+00:00")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def ingest(con: sqlite3.Connection, log_path: Path, prices: dict[str, dict]) -> dict:
    """Aggregate new bytes from the routing log. Idempotent, resumable."""
    stats = {"records": 0, "rejected": 0, "bytes": 0, "reset": False, "unpriced": set()}
    if not log_path.exists():
        return stats

    st = log_path.stat()
    saved_inode = _meta(con, "log_inode")
    offset = int(_meta(con, "log_offset", "0"))

    # Rotated or truncated underneath us: start over from the current file.
    if saved_inode != str(st.st_ino) or st.st_size < offset:
        offset, stats["reset"] = 0, saved_inode is not None

    if st.st_size == offset:
        return stats

    buckets: dict[tuple[str, str, str], dict] = {}
    rejects: list[tuple[str, str, str]] = []
    seen_prices: dict[str, dict] = {}

    with log_path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):          # partial trailing write; stop here
                break
            offset += len(raw)
            stats["bytes"] += len(raw)
            try:
                rec = json.loads(raw)
                key = (_hour(upstream.field(rec, "ts", "")),
                       str(upstream.field(rec, "model", "")),
                       str(upstream.field(rec, "tier", "")),
                       str(upstream.field(rec, "tag", "") or ""))
            except Exception as exc:
                if len(rejects) < REJECT_CAP:
                    rejects.append((time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__,
                                    raw[:400].decode("utf-8", "replace")))
                stats["rejected"] += 1
                continue

            tokens = {c: int(upstream.field(rec, c, 0) or 0) for c in TOKEN_COLUMNS}
            model = key[1]
            price = prices.get(model)
            if price is None:
                price = {"prompt": 0.0, "completion": 0.0, "cache_read": 0.0, "cache_write": 0.0}
                stats["unpriced"].add(model)
            else:
                seen_prices[model] = price

            b = buckets.setdefault(key, {c: 0 for c in TOKEN_COLUMNS} | {"requests": 0, "cost_nano": 0})
            b["requests"] += 1
            for c in TOKEN_COLUMNS:
                b[c] += tokens[c]
            b["cost_nano"] += cost_nano(tokens, price)
            stats["records"] += 1

    # One transaction: buckets, prices, rejects and the checkpoint together.
    cols = ", ".join(TOKEN_COLUMNS)
    inc = ", ".join(f"{c}=usage.{c}+excluded.{c}" for c in TOKEN_COLUMNS)
    with con:
        for (hour, model, tier, tag), b in buckets.items():
            con.execute(
                f"""INSERT INTO usage(hour_utc, model, tier, tag, requests, {cols}, cost_nano)
                    VALUES(?,?,?,?,?,{','.join('?' * len(TOKEN_COLUMNS))},?)
                    ON CONFLICT(hour_utc, model, tier, tag) DO UPDATE SET
                      requests=usage.requests+excluded.requests, {inc},
                      cost_nano=usage.cost_nano+excluded.cost_nano""",
                (hour, model, tier, tag, b["requests"], *[b[c] for c in TOKEN_COLUMNS], b["cost_nano"]))
        for model, p in seen_prices.items():
            con.execute("""INSERT INTO price_used(model, first_seen, prompt, completion, cache_read, cache_write)
                           VALUES(?,?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET
                           prompt=excluded.prompt, completion=excluded.completion,
                           cache_read=excluded.cache_read, cache_write=excluded.cache_write""",
                        (model, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         p["prompt"], p["completion"], p["cache_read"], p["cache_write"]))
        for r in rejects:
            con.execute("INSERT INTO rejected(ts, reason, line) VALUES(?,?,?)", r)
        con.execute("DELETE FROM rejected WHERE id NOT IN (SELECT id FROM rejected ORDER BY id DESC LIMIT ?)",
                    (REJECT_CAP,))
        con.execute("INSERT OR REPLACE INTO meta VALUES('log_inode', ?)", (str(st.st_ino),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('log_offset', ?)", (str(offset),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_ingest', ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    stats["unpriced"] = sorted(stats["unpriced"])
    return stats


def rotate(con: sqlite3.Connection, log_path: Path, max_bytes: int = DEFAULT_MAX_LOG_BYTES) -> bool:
    """Rotate the routing log, but only once everything in it is aggregated."""
    if not log_path.exists() or log_path.stat().st_size < max_bytes:
        return False
    if int(_meta(con, "log_offset", "0")) < log_path.stat().st_size:
        return False                              # unaggregated tail; leave it
    previous = log_path.with_suffix(log_path.suffix + ".1")
    previous.unlink(missing_ok=True)
    log_path.rename(previous)
    log_path.touch()
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES('log_inode', ?)", (str(log_path.stat().st_ino),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('log_offset', '0')")
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_rotation', ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    return True


# --------------------------------------------------------------------------
# rollups
# --------------------------------------------------------------------------

PERIODS = ("last hour", "today", "this week", "this month", "this year", "total")


def _since(period: str, now: datetime) -> str | None:
    if period == "last hour":  return (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    if period == "today":      return now.strftime("%Y-%m-%dT00")
    if period == "this week":  return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%dT00")
    if period == "this month": return now.strftime("%Y-%m-01T00")
    if period == "this year":  return now.strftime("%Y-01-01T00")
    return None


def totals(con, now: datetime | None = None, tag: str | None = None) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    out = {}
    for period in PERIODS:
        since = _since(period, now)
        clauses = ([f"hour_utc >= ?"] if since else []) + (["tag = ?"] if tag else [])
        args = tuple(x for x in (since, tag) if x)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        row = con.execute(f"""
            SELECT COALESCE(SUM(requests),0) requests,
                   COALESCE(SUM(CASE WHEN tier=? THEN cost_nano END),0) judge,
                   COALESCE(SUM(CASE WHEN tier<>? THEN cost_nano END),0) served,
                   COALESCE(SUM(cost_nano),0) total
            FROM usage {where}""", (upstream.TIER_JUDGE, upstream.TIER_JUDGE, *args)).fetchone()
        out[period] = {"requests": row["requests"], "judge": row["judge"] / NANO,
                       "served": row["served"] / NANO, "total": row["total"] / NANO}
    return out


# Rolling windows, counted back from now — not calendar boundaries. "last day"
# means the past 24 hours, not since midnight.
CLEAR_WINDOWS = {
    "last hour": timedelta(hours=1),
    "last day": timedelta(days=1),
    "last week": timedelta(weeks=1),
    "last month": timedelta(days=30),
    "last year": timedelta(days=365),
    "all": None,
}


def clear_since(period: str, now: datetime) -> str | None:
    """Bucket cutoff for a rolling clear window, or None to mean everything."""
    delta = CLEAR_WINDOWS[period]
    if delta is None:
        return None
    # Buckets are whole hours, so the cutoff hour is included in full.
    return (now - delta).strftime("%Y-%m-%dT%H")


def clear_preview(con: sqlite3.Connection, period: str, now: datetime) -> tuple[int, float]:
    """What `clear` would remove, without removing it."""
    since = clear_since(period, now)
    where, args = ("WHERE hour_utc >= ?", (since,)) if since else ("", ())
    row = con.execute(f"SELECT COALESCE(SUM(requests),0) q, COALESCE(SUM(cost_nano),0) c "
                      f"FROM usage {where}", args).fetchone()
    return row["q"], row["c"] / NANO


def clear(con: sqlite3.Connection, period: str, now: datetime | None = None) -> dict:
    """Delete aggregated usage for a period. Returns what was removed.

    Deletion does not rewind the ingest checkpoint, so cleared data is gone for
    good rather than reappearing on the next pass — which is what you want when
    zeroing counters between test runs. `reset_ingest` is the separate lever for
    re-reading the log from the beginning.
    """
    now = now or datetime.now(timezone.utc)
    since = clear_since(period, now)
    where, args = ("WHERE hour_utc >= ?", (since,)) if since else ("", ())
    requests, cost = clear_preview(con, period, now)
    removed = {"requests": requests, "cost": cost, "period": period, "since": since}
    with con:
        con.execute(f"DELETE FROM usage {where}", args)
        if period == "all":
            con.execute("DELETE FROM rejected")
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_clear', ?)",
                    (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} ({period})",))
    return removed


def reset_ingest(con: sqlite3.Connection) -> None:
    """Forget where we were in the routing log, so it is re-read from the start.

    Pair with clear('everything') to rebuild history from the log at current
    prices; on its own it would double-count everything still in the file.
    """
    with con:
        con.execute("DELETE FROM meta WHERE key IN ('log_inode','log_offset')")


def by_model(con, since: str | None = None, judge_only: bool = False,
             tag: str | None = None) -> list[tuple[str, int, float]]:
    clauses, args = [], []
    if since:
        clauses.append("hour_utc >= ?"); args.append(since)
    if tag:
        clauses.append("tag = ?"); args.append(tag)
    clauses.append("tier = ?" if judge_only else "tier <> ?"); args.append(upstream.TIER_JUDGE)
    rows = con.execute(f"""SELECT model, SUM(requests) r, SUM(cost_nano) c FROM usage
                           WHERE {' AND '.join(clauses)} GROUP BY model ORDER BY c DESC""", args).fetchall()
    return [(r["model"], r["r"], r["c"] / NANO) for r in rows]


def by_tag(con, since: str | None = None) -> list[tuple[str, int, float]]:
    """Spend per person. Untagged traffic keeps its own row rather than hiding."""
    where, args = ("AND hour_utc >= ?", (since,)) if since else ("", ())
    rows = con.execute(f"""SELECT tag, SUM(requests) r, SUM(cost_nano) c FROM usage
                           WHERE 1=1 {where} GROUP BY tag ORDER BY c DESC""", args).fetchall()
    return [(r["tag"] or "—", r["r"], r["c"] / NANO) for r in rows]


def tags(con) -> list[str]:
    return [r["tag"] for r in con.execute(
        "SELECT DISTINCT tag FROM usage WHERE tag <> '' ORDER BY tag")]


def counterfactual(con, dearest_price: dict, since: str | None = None) -> float:
    """What the served traffic would have cost on the most expensive mode model."""
    where, args = ("AND hour_utc >= ?", (since,)) if since else ("", ())
    row = con.execute(f"""SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(cached_tokens),0) cr,
                                 COALESCE(SUM(cache_creation_tokens),0) cw,
                                 COALESCE(SUM(completion_tokens),0) o, COALESCE(SUM(reasoning_tokens),0) rt
                          FROM usage WHERE tier <> ? {where}""",
                      (upstream.TIER_JUDGE, *args)).fetchone()
    return cost_nano({"prompt_tokens": row["p"], "cached_tokens": row["cr"],
                      "cache_creation_tokens": row["cw"], "completion_tokens": row["o"],
                      "reasoning_tokens": row["rt"]}, dearest_price) / NANO
