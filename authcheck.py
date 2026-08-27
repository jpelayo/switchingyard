#!/usr/bin/env python3
"""Membership check for caller-pays mode.

Caddy delegates the yes/no here via `forward_auth`, which sends a GET carrying
the original request's headers and no body. This answers 200 or 401 and nothing
else — it never proxies, never reads a payload, never writes a credential
anywhere.

The roster holds only SHA-256 digests. A leak of the database exposes no
spendable secret, which is the entire point: membership is verifiable without
custody.

Every failure denies. A missing database, a locked one, a corrupt one and an
unknown digest are all the same answer here — 401 — because the alternative is a
checker that fails open on the one path that decides who gets in.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tui"))
import store  # noqa: E402

DATA = Path(os.environ.get("SWITCHYARD_DATA", "/data"))
DB = store.path(DATA)
PORT = int(os.environ.get("SWITCHYARD_AUTHCHECK_PORT", "9000"))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def presented(headers) -> str | None:
    auth = headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return (headers.get("x-api-key") or "").strip() or None


def authorised(headers) -> str | None:
    """Tag of the matching roster entry, or None.

    A fresh connection per request: this server is threaded, sqlite3 connections
    are not shareable across threads, and revocation must take effect without a
    restart. The connection is query-only, so this process cannot write to the
    database even by accident.

    The comparison stays `hmac.compare_digest` against the rows rather than a
    `WHERE sha256 = ?`, so matching does not become a timing signal. The roster
    is a handful of rows; the scan costs nothing.
    """
    secret = presented(headers)
    if not secret:
        return None
    candidate = digest(secret)
    con = None
    try:
        con = store.connect(DB, query_only=True)
        rows = con.execute(
            "SELECT tag, sha256 FROM api_key WHERE revoked=0").fetchall()
    except Exception:
        # Missing, locked, corrupt, permission-denied: all deny.
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    for row in rows:
        if hmac.compare_digest(candidate, row["sha256"] or ""):
            return row["tag"]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "switchyard-authcheck"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass        # the decision is logged below; never log headers

    def _answer(self):
        label = authorised(self.headers)
        body = b'{"ok":true}' if label else b'{"ok":false}'
        self.send_response(200 if label else 401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        if label:
            # Caddy can copy this onto the upstream request for attribution.
            self.send_header("x-switchyard-client", label)
        else:
            self.send_header("www-authenticate", 'Bearer realm="switchyard"')
        self.end_headers()
        self.wfile.write(body)
        print(f"{'allow' if label else 'deny '} {label or '-'}", flush=True)

    do_GET = do_HEAD = do_POST = _answer


def main() -> int:
    n = 0
    try:
        con = store.connect(DB, query_only=True)
        n = con.execute("SELECT COUNT(*) c FROM api_key WHERE revoked=0").fetchone()["c"]
        con.close()
    except Exception:
        pass         # no database yet: every request denies, which is correct
    if n == 0:
        print("WARNING: no authorised keys — every request will be rejected. "
              "Run 'manage' and add one.", file=sys.stderr, flush=True)
    print(f"authcheck on :{PORT}, {n} authorised key hash(es)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
