#!/usr/bin/env python3
"""Headless accounting ingest and routing-log rotation.

Runs as the `accounting` sidecar. Touches no request traffic: it reads the
routing log, aggregates it into SQLite, and rotates the log only once every byte
in it has been aggregated, so rotation is never lossy.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tui"))
import accounting  # noqa: E402
import health      # noqa: E402
import store       # noqa: E402
import upstream    # noqa: E402

DATA = Path(os.environ.get("SWITCHYARD_DATA", "/data"))
ROUTING_LOG = Path(os.environ.get("SWITCHYARD_ROUTING_LOG", "/var/log/switchyard/routing.jsonl"))


def configured_cap(con, fallback: int) -> int:
    """Routing-log cap from the TUI's gateway settings, so both live together."""
    try:
        gib = store.settings(con).get("routing_log_max_gib")
        if gib:
            return int(float(gib) * 1024 ** 3)
    except Exception:
        pass
    return fallback


def once(max_bytes: int) -> None:
    con = accounting.connect(store.path(DATA))
    try:
        max_bytes = configured_cap(con, max_bytes)
        prices = accounting.load_prices(con)
        stats = accounting.ingest(con, ROUTING_LOG, prices)
        note = []
        if stats["reset"]:
            note.append("log replaced underneath us, restarted from offset 0")
        if stats["rejected"]:
            note.append(f"{stats['rejected']} unparseable")
        if stats["unpriced"]:
            note.append(f"unpriced: {', '.join(stats['unpriced'][:3])}")
        # Sample the router's fault counters while we are here: they reset when
        # it restarts, and nothing else keeps a history of them.
        try:
            if health.record(con):
                note.append("new failures recorded")
            health.prune(con)
        except Exception as exc:
            note.append(f"failure sampling: {type(exc).__name__}")
        if accounting.rotate(con, ROUTING_LOG, max_bytes):
            note.append("rotated")
        if stats["records"] or note:
            print(f"ingested {stats['records']} records"
                  f"{' (' + '; '.join(note) + ')' if note else ''}", flush=True)
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Switchyard accounting ingest")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="run forever, ingesting every SECONDS")
    ap.add_argument("--max-log-bytes", type=int, default=accounting.DEFAULT_MAX_LOG_BYTES,
                    help="rotate the routing log above this size (default 1 GiB)")
    ap.add_argument("--check-upstream", action="store_true",
                    help="verify the vendored Switchyard still honours our assumptions")
    args = ap.parse_args()

    if args.check_upstream:
        ok, results = upstream.check(DATA / "routes.toml")
        for passed, name, detail in results:
            print(f"{'PASS' if passed else 'FAIL'}  {name:<44} {detail}")
        return 0 if ok else 1

    if args.loop:
        print(f"accounting: every {args.loop}s, rotating above "
              f"{args.max_log_bytes / 1024**3:.2f} GiB", flush=True)
        while True:
            try:
                once(args.max_log_bytes)
            except Exception as exc:                 # never let the sidecar die
                print(f"ingest failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(args.loop)
    else:
        once(args.max_log_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
