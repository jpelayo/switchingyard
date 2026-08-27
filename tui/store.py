"""The one place state lives.

Everything the operator configures — profiles, the key roster, gateway settings
and the cached model catalogue — sits in a single SQLite database alongside the
cost history. Nothing else on the volume is state.

That is not tidiness for its own sake. The previous arrangement was five files
in three formats, each with a hand-rolled load and save, and each with the same
failure mode: a file that would not parse silently became an empty config, and
an empty config is indistinguishable from one nobody has filled in yet. A table
that is empty is empty; a table that will not open raises.

`routes.toml` and the `Caddyfile` stay files because other processes read them —
the router and the gateway container. They are generated output, rewritten from
these tables on every change and every start, and deleting either is harmless.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_NAME = "switchyard.db"

SCHEMA = """
-- Operational bookkeeping: schema version, ingest checkpoint, last rotation.
-- Kept apart from `setting`, which is what the operator chose.
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- Operator configuration. Values are JSON-encoded scalars so an int stays an
-- int: the settings are a mix of str, int, float and bool.
CREATE TABLE IF NOT EXISTS setting(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS profile(
    name TEXT PRIMARY KEY,
    prompt TEXT,
    judge_kind TEXT NOT NULL DEFAULT 'full');

-- One row per (profile, slot). A slot is a model, how hard it may think, and
-- the adapter it came from.
CREATE TABLE IF NOT EXISTS slot(
    profile TEXT NOT NULL REFERENCES profile(name) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    adapter TEXT NOT NULL,
    PRIMARY KEY (profile, slot));

-- Membership without custody: the digest is stored, never the key.
CREATE TABLE IF NOT EXISTS api_key(
    sha256 TEXT PRIMARY KEY,
    tag TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 1,
    revoked INTEGER NOT NULL DEFAULT 0,
    added TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS api_key_live ON api_key(revoked);

-- Cached catalogue, one row per model per adapter. Prices live here too, so
-- accounting reads them from the same place the picker does.
CREATE TABLE IF NOT EXISTS model(
    adapter TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    price_prompt REAL NOT NULL DEFAULT 0,
    price_completion REAL NOT NULL DEFAULT 0,
    price_cache_read REAL NOT NULL DEFAULT 0,
    price_cache_write REAL NOT NULL DEFAULT 0,
    price_image REAL NOT NULL DEFAULT 0,
    ctx INTEGER NOT NULL DEFAULT 0,
    max_out INTEGER NOT NULL DEFAULT 0,
    structured INTEGER NOT NULL DEFAULT 0,
    tools INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,
    efforts TEXT NOT NULL DEFAULT '[]',
    default_effort TEXT NOT NULL DEFAULT '',
    reasoning_mandatory INTEGER NOT NULL DEFAULT 0,
    image_in INTEGER NOT NULL DEFAULT 0,
    image_out INTEGER NOT NULL DEFAULT 0,
    agentic REAL,
    coding REAL,
    PRIMARY KEY (adapter, id));

CREATE TABLE IF NOT EXISTS catalogue(
    adapter TEXT PRIMARY KEY,
    fetched REAL NOT NULL);

CREATE TABLE IF NOT EXISTS usage(
    hour_utc TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_nano INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, model, tier, tag));
CREATE INDEX IF NOT EXISTS usage_hour ON usage(hour_utc);
CREATE INDEX IF NOT EXISTS usage_tag ON usage(tag);

-- What we actually charged a model at, the first time we saw it. Write-only
-- audit trail: nothing queries it, and that is deliberate — it is there for the
-- day a figure is disputed.
CREATE TABLE IF NOT EXISTS price_used(
    model TEXT PRIMARY KEY, first_seen TEXT,
    prompt REAL, completion REAL, cache_read REAL, cache_write REAL);

-- Failures the router counted, sampled by the accounting sidecar so a history
-- survives its counters resetting on restart. See tui/health.py.
CREATE TABLE IF NOT EXISTS failure(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    reason TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    detail TEXT);
CREATE INDEX IF NOT EXISTS failure_ts ON failure(ts);

-- Routing-log lines that would not parse, capped. Keeps a bad line visible
-- instead of silently dropping it.
CREATE TABLE IF NOT EXISTS rejected(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reason TEXT, line TEXT);
"""

# Columns of `model`, in the order the catalogue row supplies them. Kept as data
# so the insert and the read cannot disagree about the shape.
MODEL_COLUMNS = (
    "id", "name", "price_prompt", "price_completion", "price_cache_read",
    "price_cache_write", "price_image", "ctx", "max_out", "structured", "tools",
    "reasoning", "efforts", "default_effort", "reasoning_mandatory",
    "image_in", "image_out", "agentic", "coding",
)


def path(data: Path) -> Path:
    return data / DB_NAME


def connect(db_path: Path, query_only: bool = False) -> sqlite3.Connection:
    """Open the database. `query_only` blocks writes at the SQL layer.

    Deliberately NOT `mode=ro` for readers: a read-only handle cannot create the
    WAL index file, so on a database no writer has touched since boot it fails
    to open at all. Opening read-write and setting `query_only` gives a reader
    that can participate in WAL and still cannot write a byte.
    """
    if query_only:
        # sqlite3.connect would CREATE an empty database here. A reader must not
        # conjure the thing it is reading — an absent database means "nothing is
        # configured", and for authcheck that has to raise, not return no rows.
        if not db_path.exists():
            raise FileNotFoundError(db_path)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # readers never block the writer
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    if not query_only:
        con.executescript(SCHEMA)
        _add_missing_columns(con)
        con.commit()
    else:
        con.execute("PRAGMA query_only=ON")
    return con


# Columns added after a deployment already existed. CREATE TABLE IF NOT EXISTS
# leaves an existing table alone, so a new column has to be added explicitly.
# This is forward schema evolution, not a migration path from the old file
# format — there is still no way back to that, and none wanted.
LATER_COLUMNS = (
    ("profile", "judge_kind", "TEXT NOT NULL DEFAULT 'full'"),
)


def _add_missing_columns(con) -> None:
    for table, column, decl in LATER_COLUMNS:
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# -- settings --------------------------------------------------------------

def settings(con) -> dict:
    return {r["key"]: json.loads(r["value"])
            for r in con.execute("SELECT key, value FROM setting")}


def save_settings(con, values: dict) -> None:
    con.executemany("INSERT INTO setting(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [(k, json.dumps(v)) for k, v in values.items()])
    con.commit()


# -- roster ----------------------------------------------------------------

def roster(con) -> list[dict]:
    """Every entry, revoked included: past spend keeps its name in accounting."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM api_key ORDER BY revoked, tag")]


def add_key(con, entry: dict) -> None:
    con.execute(
        "INSERT INTO api_key(sha256, tag, provider, hint, profile, verified, "
        "                    revoked, added) VALUES(?,?,?,?,?,?,0,?)",
        (entry["sha256"], entry["tag"], entry["provider"], entry.get("hint", ""),
         entry["profile"], int(entry.get("verified", True)), entry.get("added", "")))
    con.commit()


def revoke_key(con, tag: str) -> None:
    con.execute("UPDATE api_key SET revoked=1 WHERE tag=?", (tag,))
    con.commit()


def set_key_profile(con, tag: str, profile: str) -> None:
    con.execute("UPDATE api_key SET profile=? WHERE tag=?", (profile, tag))
    con.commit()


def live_tags(con) -> list[str]:
    return [r["tag"] for r in con.execute(
        "SELECT tag FROM api_key WHERE revoked=0 ORDER BY tag")]


# -- catalogue -------------------------------------------------------------

def save_catalogue(con, adapter: str, rows: list[dict], fetched: float) -> None:
    """Replace one adapter's catalogue wholesale, in one transaction.

    Wholesale because a model withdrawn upstream must disappear here too; in one
    transaction so a reader never sees a half-replaced catalogue.
    """
    with con:
        con.execute("DELETE FROM model WHERE adapter=?", (adapter,))
        con.executemany(
            f"INSERT INTO model(adapter, {', '.join(MODEL_COLUMNS)}) "
            f"VALUES(?{', ?' * len(MODEL_COLUMNS)})",
            [tuple([adapter] + [_encode(r.get(c)) for c in MODEL_COLUMNS])
             for r in rows])
        con.execute("INSERT INTO catalogue(adapter, fetched) VALUES(?, ?) "
                    "ON CONFLICT(adapter) DO UPDATE SET fetched=excluded.fetched",
                    (adapter, fetched))


def _encode(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


def catalogue(con, adapter: str) -> list[dict]:
    return [_row_to_model(r) for r in con.execute(
        "SELECT * FROM model WHERE adapter=? ORDER BY id", (adapter,))]


def model_record(con, adapter: str, model: str) -> dict:
    r = con.execute("SELECT * FROM model WHERE adapter=? AND id=?",
                    (adapter, model)).fetchone()
    return _row_to_model(r) if r else {}


def _row_to_model(r) -> dict:
    row = dict(r)
    row.pop("adapter", None)
    for flag in ("structured", "tools", "reasoning", "reasoning_mandatory",
                 "image_in", "image_out"):
        row[flag] = bool(row[flag])
    row["efforts"] = json.loads(row["efforts"] or "[]")
    # Per-million, for display. Derived rather than stored so the two cannot drift.
    row["in"] = row["price_prompt"] * 1e6
    row["out"] = row["price_completion"] * 1e6
    return row


def catalogue_fetched(con, adapter: str) -> float | None:
    r = con.execute("SELECT fetched FROM catalogue WHERE adapter=?",
                    (adapter,)).fetchone()
    return r["fetched"] if r else None


def adapters_cached(con) -> list[str]:
    return [r["adapter"] for r in con.execute(
        "SELECT adapter FROM catalogue ORDER BY adapter")]


def prices(con) -> dict[str, dict]:
    """Per-token prices by model id, across every cached adapter.

    The routing log records a model id and no adapter, so two adapters listing
    the same slug at different prices cannot be told apart after the fact. The
    first adapter alphabetically wins; exact while one is registered.
    """
    out: dict[str, dict] = {}
    for r in con.execute("SELECT id, price_prompt, price_completion, "
                         "price_cache_read, price_cache_write FROM model "
                         "ORDER BY adapter"):
        out.setdefault(r["id"], {"prompt": r["price_prompt"],
                                 "completion": r["price_completion"],
                                 "cache_read": r["price_cache_read"],
                                 "cache_write": r["price_cache_write"]})
    return out
