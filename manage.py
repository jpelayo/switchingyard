#!/usr/bin/env python3
"""Switchyard management TUI.

Stdlib only: curses, sqlite3, urllib. Runs inside the container and is the only
place configuration is edited.

The server holds no provider credential. Each caller presents their own key,
which is relayed upstream, so they are billed on their own account. Membership
is a roster of SHA-256 digests — nothing spendable is stored here.

All state is in one database, /data/switchyard.db: profiles, the roster, gateway
settings, the cached catalogue and the cost history. The only other files are
generated output, rewritten from those tables on every change and every start:

    routes.toml    GENERATED — read by the router
    Caddyfile      GENERATED — read by the gateway container
"""

from __future__ import annotations

import curses
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tui"))
import accounting                                           # noqa: E402
import caddyfile                                            # noqa: E402
import profiles as profiles_store                           # noqa: E402
import health                                               # noqa: E402
import providers                                            # noqa: E402
import store                                                # noqa: E402
import routes as routes_gen                                 # noqa: E402
import upstream                                             # noqa: E402
from profiles import MODES, SLOTS, SLOT_REQUIRES, Profiles, valid_name   # noqa: E402

DATA = Path(os.environ.get("SWITCHYARD_DATA", "/data"))
ROUTES = DATA / "routes.toml"
CADDYFILE = DATA / "Caddyfile"
DB = store.path(DATA)
ROUTING_LOG = Path(os.environ.get("SWITCHYARD_ROUTING_LOG", "/var/log/switchyard/routing.jsonl"))
GATEWAY_LOG = Path("/var/log/switchyard/gateway.log")

TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SELECTED_HEADER = upstream.SELECTED_MODEL_HEADER

# Widest slot name, derived rather than fixed: `user-interface` is 14 and a
# hardcoded 12 left it touching the model column with no gutter at all. Adding a
# mode must not silently break alignment, so this follows SLOTS.
SLOT_W = max(len(s) for s in SLOTS)


_db = None


def db():
    """The one connection this process uses.

    Opened lazily so importing the module does not create a database — the
    entrypoint imports it to regenerate config, and a fresh install has nothing
    yet. Everything else reads through it live, so there is no cache to
    invalidate and no two views of the same state.
    """
    global _db
    if _db is None:
        _db = store.connect(DB)
    return _db


def profiles() -> Profiles:
    return Profiles(db(), gateway_settings().get("provider", providers.DEFAULT))


def provider():
    return providers.get(gateway_settings().get("provider", providers.DEFAULT))


def adapters_in_use() -> list[str]:
    """Every adapter any profile names, plus the default for new slots."""
    profs = profiles()
    seen = {provider().NAME}
    for name in profs.names():
        seen.update(profs.adapters_in_use(name))
    return sorted(seen)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def gateway_settings() -> dict:
    """Defaults, overlaid with whatever the operator has changed."""
    settings = dict(caddyfile.DEFAULTS)
    settings.setdefault("provider", providers.DEFAULT)
    settings.update(store.settings(db()))
    return settings


def save_gateway_settings(settings: dict) -> None:
    # Store only what differs from the defaults, so a changed default reaches
    # deployments that never touched that field.
    changed = {k: v for k, v in settings.items()
               if k not in caddyfile.DEFAULTS or caddyfile.DEFAULTS[k] != v}
    store.save_settings(db(), changed)
    write_config()


# --------------------------------------------------------------------------
# roster — tags and key digests, never keys
# --------------------------------------------------------------------------

def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def load_roster() -> list[dict]:
    """Every entry, revoked included — past spend keeps its name in accounting."""
    return store.roster(db())


def active_tags() -> list[str]:
    return store.live_tags(db())


def profile_of(tag: str) -> str:
    for e in load_roster():
        if e["tag"] == tag:
            return e.get("profile", "default")
    return "default"


def users_of(profile: str) -> list[str]:
    return [e["tag"] for e in load_roster()
            if e.get("profile", "default") == profile and not e.get("revoked")]


# --------------------------------------------------------------------------
# generated configuration
# --------------------------------------------------------------------------

def write_config() -> tuple[list[str], tuple[bool, str]]:
    """Regenerate routes.toml and the Caddyfile, then validate.

    Both are derived from the database, which is the source of truth for both
    routing and admission. Neither is hand-editable, which is what allows one
    route set per profile.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    settings = gateway_settings()
    # Slots written before per-slot adapters existed inherit the old global
    # setting rather than a hardcoded name.
    profiles_store.DEFAULT_ADAPTER = settings.get("provider", providers.DEFAULT)
    profs = profiles()

    toml, skipped = routes_gen.render(
        profs,
        recent_turn_window=int(settings.get("recent_turn_window", 4)),
        max_output_tokens=int(settings.get("max_output_tokens", 1024)),
        default_target=settings.get("fallback_mode", FALLBACK_DEFAULT))
    ROUTES.write_text(toml)

    CADDYFILE.write_text(caddyfile.render(**{k: v for k, v in settings.items()
                                             if k in caddyfile.DEFAULTS}))
    CADDYFILE.chmod(0o600)
    return skipped, validate_caddy()


def _run_caddy(path: Path) -> tuple[bool, str]:
    r = subprocess.run(["caddy", "validate", "--config", str(path), "--adapter", "caddyfile"],
                       capture_output=True, text=True, timeout=60)
    out = [l for l in (r.stderr + r.stdout).strip().splitlines()
           if l.strip() and not l.lstrip().startswith("{")]
    return r.returncode == 0, (out[-1] if out else "no output")


def validate_caddy() -> tuple[bool, str]:
    """`caddy validate` opens the log writer, so a missing log directory fails
    for a reason unrelated to the config. Create it, and fall back to checking a
    stderr-logging copy if it still cannot be written."""
    if not CADDYFILE.exists():
        return False, "not generated yet"
    try:
        GATEWAY_LOG.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        ok, detail = _run_caddy(CADDYFILE)
        if ok or "log writer" not in detail:
            return ok, detail
        stripped = re.sub(r"output file [^\n]*\{[^}]*\}", "output stderr", CADDYFILE.read_text())
        # In a temp dir, not beside the real one: /data holds the database and
        # the two generated files, and nothing transient should appear there.
        with tempfile.TemporaryDirectory() as tmp:
            alt = Path(tmp) / "Caddyfile"
            alt.write_text(stripped)
            ok2, detail2 = _run_caddy(alt)
        return (True, "rules valid; log destination unchecked here") if ok2 else (False, detail2)
    except FileNotFoundError:
        return True, "caddy not in this image — the gateway validates at startup"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def validate_routes() -> tuple[bool, str]:
    """Ask the real server whether the generated routing config loads."""
    try:
        r = subprocess.run(["switchyard-server", "--config", str(ROUTES), "--dry-run"],
                           capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})
        out = (r.stdout + r.stderr).strip().splitlines()
        return r.returncode == 0, (out[-1] if out else f"exit {r.returncode}")
    except FileNotFoundError:
        return True, "switchyard-server not on PATH here"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# curses primitives
# --------------------------------------------------------------------------

def prompt(stdscr, label: str, hidden: bool = False) -> str | None:
    """One-line input at the bottom of the screen. Returns None on escape."""
    h, w = stdscr.getmaxyx()
    curses.noecho()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(h - 1, 0, label[: w - 1], curses.A_BOLD)
    stdscr.refresh()
    buf = ""
    while True:
        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        if ch in ("\n", "\r"):
            break
        if ch == "\x1b":
            return None
        if ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
            buf = buf[:-1]
        elif isinstance(ch, str) and ch.isprintable():
            buf += ch
        shown = "*" * len(buf) if hidden else buf
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        stdscr.addstr(h - 1, 0, (label + shown)[: w - 1], curses.A_BOLD)
        stdscr.refresh()
    return buf.strip()


def pager(stdscr, title: str, body: str, keys: tuple[str, ...] = ()) -> str | None:
    """Scrollable text view. Returns a key from `keys` if the user pressed one."""
    lines = body.splitlines() or ["(no output)"]
    top = 0
    while True:
        h, w = stdscr.getmaxyx()
        view = h - 2
        stdscr.erase()
        stdscr.addstr(0, 0, title[: w - 1], curses.A_REVERSE)
        for i, line in enumerate(lines[top : top + view]):
            stdscr.addstr(i + 1, 0, line[: w - 1])
        extra = "".join(f"   {c} {n}" for c, n in
                        (("c", "clear"), ("t", "by tag")) if c in keys)
        stdscr.addstr(h - 1, 0, ("↑↓ PgUp/PgDn scroll   q back" + extra)[: w - 1], curses.A_DIM)
        stdscr.refresh()
        k = stdscr.getch()
        if 0 < k < 256 and chr(k) in keys:
            return chr(k)
        if k in (ord("q"), 27, curses.KEY_ENTER, 10, 13):
            return None
        if k == curses.KEY_DOWN and top + view < len(lines):
            top += 1
        elif k == curses.KEY_UP and top > 0:
            top -= 1
        elif k == curses.KEY_NPAGE:
            top = min(top + view, max(0, len(lines) - view))
        elif k == curses.KEY_PPAGE:
            top = max(top - view, 0)


SEPARATOR = "\u2500" * 52          # a row the cursor refuses to land on


def _selectable(rows: list[str], i: int) -> bool:
    return 0 <= i < len(rows) and rows[i].strip() != SEPARATOR


def _nearest(rows: list[str], i: int, step: int = 1) -> int:
    """Move off a separator in the direction of travel, then back if need be."""
    j = i
    while 0 <= j < len(rows) and not _selectable(rows, j):
        j += step
    if not _selectable(rows, j):
        j = i
        while 0 <= j < len(rows) and not _selectable(rows, j):
            j -= step
    return max(0, min(j, len(rows) - 1))


def choose(stdscr, title: str, rows: list[str], hint: str = "", start: int = 0,
           keys: tuple[str, ...] = ()):
    """Scrolling single-select list. Returns the index, or None on escape.

    `start` opens the list on a given row, so a caller looping over the same
    list keeps the cursor where the user left it. Rows equal to SEPARATOR are
    drawn but never selected.

    `keys` are extra keystrokes that act on the highlighted row; pressing one
    returns (index, key) instead of a bare index. They must be control codes —
    a plain letter would be indistinguishable from a shortcut.

    A row whose label begins with a single character and a space — "d duplicate",
    "+ new", "? how users connect" — advertises that character as a shortcut, so
    pressing it selects that row. The labels promised this long before anything
    implemented it, which made those actions look broken.
    """
    sel, top = _nearest(rows, max(0, min(start, len(rows) - 1))), 0
    while True:
        h, w = stdscr.getmaxyx()
        view = h - 3
        if sel < top:
            top = sel
        if sel >= top + view:
            top = sel - view + 1
        stdscr.erase()
        stdscr.addstr(0, 0, title[: w - 1], curses.A_REVERSE)
        for i, row in enumerate(rows[top : top + view]):
            style = curses.A_STANDOUT if top + i == sel else curses.A_NORMAL
            stdscr.addstr(i + 1, 0, row[: w - 1].ljust(min(w - 1, max(len(row) + 1, 40))), style)
        stdscr.addstr(h - 1, 0, (hint or "↑↓ move   enter select   esc back")[: w - 1], curses.A_DIM)
        stdscr.refresh()
        k = stdscr.getch()
        if 32 <= k < 127:
            want = chr(k).lower()
            for i, row in enumerate(rows):
                label = row.strip()
                if (len(label) > 1 and label[1] == " " and label[0].lower() == want
                        and _selectable(rows, i)):
                    return i
        if k in (27, ord("q")):
            return None
        if k in (curses.KEY_ENTER, 10, 13):
            return sel if _selectable(rows, sel) else None
        if 0 < k < 32 and chr(k) in keys:
            if _selectable(rows, sel):
                return sel, chr(k)
            continue
        if k == curses.KEY_DOWN:
            sel = _nearest(rows, min(sel + 1, len(rows) - 1), 1)
        elif k == curses.KEY_UP:
            sel = _nearest(rows, max(sel - 1, 0), -1)
        elif k == curses.KEY_NPAGE:
            sel = _nearest(rows, min(sel + view, len(rows) - 1), 1)
        elif k == curses.KEY_PPAGE:
            sel = _nearest(rows, max(sel - view, 0), -1)


# --------------------------------------------------------------------------
# catalogue (via the provider adapter)
# --------------------------------------------------------------------------

def fetch_catalogue(adapter: str = "") -> list[dict]:
    adapter = adapter or provider().NAME
    rows = providers.get(adapter).catalogue()
    store.save_catalogue(db(), adapter, rows, time.time())
    return rows


def catalogue(adapter: str = "") -> list[dict]:
    """The cached catalogue, fetched on first use.

    No process-local memo any more: the table IS the cache, shared by every
    process, and a reload in one is visible in the next read everywhere.
    """
    adapter = adapter or provider().NAME
    rows = store.catalogue(db(), adapter)
    return rows if rows else fetch_catalogue(adapter)


def model_record(adapter: str, model: str) -> dict:
    """One catalogue row, or {} when the catalogue is missing or the slug was
    typed by hand. Callers must cope with {}: it means "capabilities unknown",
    not "unsupported"."""
    if not model:
        return {}
    try:
        return store.model_record(db(), adapter, model)
    except Exception:
        return {}


def catalogue_age(adapter: str = "") -> str:
    adapter = adapter or provider().NAME
    fetched = store.catalogue_fetched(db(), adapter)
    if fetched is None:
        return "never downloaded"
    mins = (time.time() - fetched) / 60
    if mins < 60:
        return f"{int(mins)} min ago"
    if mins < 60 * 24:
        return f"{int(mins / 60)} h ago"
    return f"{int(mins / 1440)} days ago"


def action_reload(stdscr, only: str = "") -> None:
    """Refresh the cached catalogue of every adapter in use.

    One failing adapter must not cost the others their refresh, so each is
    reported on its own line and the previously cached copy is left in place.
    """
    targets = [only] if only else adapters_in_use()
    h, w = stdscr.getmaxyx()
    lines, failed = [], []
    for adapter in targets:
        stdscr.erase()
        stdscr.addstr(0, 0, f"Downloading the {providers.get(adapter).DISPLAY} "
                            f"model catalogue…"[: w - 1])
        stdscr.refresh()
        try:
            rows = fetch_catalogue(adapter)
        except Exception as exc:
            failed.append(f"{adapter}: {type(exc).__name__}: {exc}")
            continue
        lines += [f"{adapter} — {len(rows)} models cached",
                  f"    {sum(1 for r in rows if r['structured'])} structured outputs "
                  f"(eligible as judge)",
                  f"    {sum(1 for r in rows if r['tools'])} tool calling",
                  f"    {sum(1 for r in rows if r['reasoning'])} accept a reasoning level",
                  ""]
    if failed:
        lines += ["Not refreshed — the previously cached copy is still in place:"] + \
                 [f"    {f}" for f in failed] + ["", "Outbound HTTPS to the provider is needed."]
    pager(stdscr, "Catalogue reload failed" if failed and not lines else "Catalogue updated",
          "\n".join(lines + [
              "Floating aliases are excluded: pin a dated slug so routing does not",
              "shift underneath you."]))


CAPABILITY_LABEL = {"structured": "structured output", "image_in": "image input",
                    "image_out": "image output", "tools": "tool calling"}


def slot_filter(slot: str, profile: str = "") -> tuple[str, ...]:
    """What a model must do to fill this slot. The judge's requirement depends
    on the profile, because the judge's kind is a per-profile choice."""
    if profile:
        return profiles().requires(profile, slot)
    # No profile named: fall back to the STRICTER judge, never to no filter.
    # A caller that forgets the profile should get too few models offered, not
    # a judge that cannot read the images it will be shown.
    if slot == "judge":
        return profiles_store.JUDGE_KINDS[profiles_store.JUDGE_KIND_DEFAULT]
    return SLOT_REQUIRES.get(slot, ())


def _row_label(r: dict, width: int) -> str:
    flags = ("S" if r["structured"] else "·") + ("T" if r["tools"] else "·") + ("R" if r["reasoning"] else "·") \
            + ("I" if r.get("image_in") else "·") + ("O" if r.get("image_out") else "·")
    agentic = f"{r['agentic']:>5.1f}" if r.get("agentic") is not None else "    ·"
    return (f"{r['id']:<44} {r['in']:>8.3f} {r['out']:>8.3f}  "
            f"{r['ctx'] // 1000:>5}k {agentic}  {flags}")[: width - 1]


def pick_model(stdscr, what: str, slot: str = "", adapter: str = "",
               profile: str = "") -> tuple[str, str] | None:
    """Scroll and type-to-filter one adapter's catalogue, restricted to what the
    slot needs. Returns (adapter, model).

    A model that cannot do the job is never offered rather than being allowed and
    failing at request time — the judge must emit a structured verdict AND accept
    images (it is shown the conversation verbatim), image-in needs vision, and
    image-out needs a model that actually returns pictures.
    """
    adapter = adapter or provider().NAME
    required = slot_filter(slot, profile)
    query, sel, top = "", 0, 0

    def load() -> list[dict] | None:
        try:
            rows = catalogue(adapter)
        except Exception as exc:
            stdscr.erase()
            stdscr.addstr(0, 0, f"No catalogue available: {exc}"[: stdscr.getmaxyx()[1] - 1])
            stdscr.addstr(2, 0, "Enter a slug manually, or esc and use 'Reload catalogue'.")
            stdscr.refresh(); stdscr.getch()
            return None
        for capability in required:
            rows = [r for r in rows if r.get(capability)]
        return rows

    rows_all = load()
    if rows_all is None:
        typed = prompt(stdscr, f"{what} slug: ")
        return (adapter, typed) if typed else None

    while True:
        h, w = stdscr.getmaxyx()
        view = h - 4
        rows = [r for r in rows_all if query in r["id"].lower()]
        sel = max(0, min(sel, len(rows) - 1))
        if sel < top:
            top = sel
        if sel >= top + view:
            top = sel - view + 1

        stdscr.erase()
        head = f"{what}  —  {len(rows)}/{len(rows_all)} models on {providers.get(adapter).DISPLAY}"
        if required:
            head += "   [only: " + " + ".join(CAPABILITY_LABEL[c] for c in required) + "]"
        stdscr.addstr(0, 0, head[: w - 1], curses.A_REVERSE)
        stdscr.addstr(1, 0, f"{'model':<44} {'$in/M':>8} {'$out/M':>8}  {'ctx':>6} {'agent':>5}  "
                            f"S=struct T=tools R=reason I=img-in O=img-out"[: w - 1], curses.A_DIM)
        for i, r in enumerate(rows[top: top + view]):
            style = curses.A_STANDOUT if top + i == sel else curses.A_NORMAL
            stdscr.addstr(i + 2, 0, _row_label(r, w).ljust(min(w - 1, 80)), style)
        if not rows:
            why = ("(no model matches that filter — backspace, or esc to clear)" if query else
                   "(no model in the catalogue can do this — try Reload catalogue)")
            stdscr.addstr(2, 0, why[: w - 1])
        if query:
            stdscr.addstr(h - 1, 0, f"filter: {query}_    esc clear   ⌫ delete   enter select"[: w - 1],
                          curses.A_BOLD)
        else:
            hint = "type to filter   ↑↓ PgUp/PgDn   enter select   ^R reload   tab manual   esc back"
            if len(providers.names()) > 1:
                hint = hint.replace("^R reload", "^A adapter   ^R reload")
            stdscr.addstr(h - 1, 0, hint[: w - 1], curses.A_DIM)
        stdscr.refresh()

        k = stdscr.getch()
        if k == 27:
            if query:
                query, sel, top = "", 0, 0
                continue
            return None
        if k in (curses.KEY_ENTER, 10, 13) and rows:
            return adapter, rows[sel]["id"]
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            query, sel, top = query[:-1], 0, 0
        elif k == curses.KEY_DOWN:
            sel += 1
        elif k == curses.KEY_UP:
            sel -= 1
        elif k == curses.KEY_NPAGE:
            sel += view
        elif k == curses.KEY_PPAGE:
            sel -= view
        elif k == curses.KEY_HOME:
            sel = 0
        elif k == curses.KEY_END:
            sel = len(rows) - 1
        elif k == 1:                        # ^A — switch adapter, re-list
            names = providers.names()
            if len(names) > 1:
                j = choose(stdscr, "Which system serves this slot?",
                           [f"  {providers.get(n).DISPLAY:<14} {providers.get(n).BASE_URL}"
                            for n in names], start=names.index(adapter))
                if j is not None and names[j] != adapter:
                    adapter = names[j]
                    fresh = load()
                    if fresh is not None:
                        rows_all = fresh
                    query, sel, top = "", 0, 0
        elif k == 18:                       # ^R — printable keys are filter input
            action_reload(stdscr, only=adapter)
            fresh = load()
            if fresh is not None:
                rows_all = fresh
            sel, top = 0, 0
        elif k == 9:                        # tab — hand-typed slug
            manual = prompt(stdscr, f"{what} slug (typed): ")
            if manual:
                return adapter, manual
        elif 32 <= k < 127:
            query += chr(k).lower()
            sel, top = 0, 0


def pick_effort(stdscr, adapter: str, model: str, slot: str, current: str = "") -> str | None:
    """Choose how hard a model thinks, from what the model itself allows.

    The catalogue enumerates each model's own levels, so an unsupported one is
    never offered; where it enumerates none, the adapter's ladder is used and the
    provider maps anything it dislikes to the nearest rung it supports.
    """
    mod = providers.get(adapter)
    record = model_record(adapter, model)
    offered = mod.levels(record)
    if not offered:
        pager(stdscr, f"{model} does not reason",
              "This model accepts no reasoning level, so none is sent for it.\n\n"
              "The catalogue reports no reasoning support. If you believe that is\n"
              "stale, reload the catalogue and try again.")
        return None

    own = record.get("default_effort") or ""
    rows = []
    for level in offered:
        note = mod.LEVEL_NOTE.get(level, "")
        marks = " ".join(filter(None, [
            "· model default" if level == own else "",
            "· current" if level == current else ""]))
        rows.append(f"  {level:<10} {note:<38} {marks}")
    title = f"Reasoning level for {slot} — {model}"
    if slot == "judge":
        title += "   (shares its output budget with the verdict)"
    j = choose(stdscr, title, rows,
               hint="enter select   esc keep current",
               start=offered.index(current) if current in offered else 0)
    return offered[j] if j is not None else None


def confirm(stdscr, question: str) -> bool:
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 1, 0, f"{question} y/N"[: w - 1], curses.A_BOLD)
    stdscr.clrtoeol()
    stdscr.refresh()
    return stdscr.getch() in (ord("y"), ord("Y"))


# --------------------------------------------------------------------------
# authorised keys
# --------------------------------------------------------------------------

def action_roster(stdscr) -> None:
    selected = 0
    while True:
        entries = load_roster()
        rows = []
        for e in entries:
            state = ("revoked" if e.get("revoked")
                     else "unverified" if not e.get("verified", True) else "active")
            rows.append(f"  {e['tag']:<18} {providers.get(e.get('provider','')).DISPLAY:<12} "
                        f"{e.get('hint','…'):<10} {e.get('profile','default'):<10} "
                        f"{e.get('added','')[:10]:<12} {state}")
        rows = rows or ["  (nobody yet — every request is refused until a key is added)"]
        rows += ["", "  + add a key", "  - revoke", "  p change profile", "  ? how users connect"]
        idx = choose(stdscr, f"Authorised keys — {len(active_tags())} active", rows,
                     start=selected, hint="enter to choose   + - p ?   esc back")
        if idx is None:
            return
        selected = idx
        label = rows[idx].strip()
        if label.startswith("+"):
            add_key_wizard(stdscr)
        elif label.startswith("-"):
            _revoke(stdscr, entries)
        elif label.startswith("p "):
            _reassign(stdscr, entries)
        elif label.startswith("?"):
            pager(stdscr, "Connecting a client", "\n".join(client_help()))


def _revoke(stdscr, entries) -> None:
    live = [e for e in entries if not e.get("revoked")]
    if not live:
        return
    j = choose(stdscr, "Revoke whose key?", [f"  {e['tag']:<20} {e.get('hint','')}" for e in live])
    if j is None or not confirm(stdscr, f"revoke {live[j]['tag']}?"):
        return
    store.revoke_key(db(), live[j]["tag"])
    write_config()
    pager(stdscr, "Revoked",
          f"{live[j]['tag']} can no longer call the service.\n\n"
          "The entry stays listed so past spend keeps its name in accounting.\n"
          "authcheck reads the roster per request, so this is already live —\n"
          "no restart needed.")


def _reassign(stdscr, entries) -> None:
    live = [e for e in entries if not e.get("revoked")]
    if not live:
        return
    j = choose(stdscr, "Change whose profile?",
               [f"  {e['tag']:<20} currently {e.get('profile','default')}" for e in live])
    if j is None:
        return
    profs = profiles()
    names = profs.names()
    k = choose(stdscr, f"Profile for {live[j]['tag']}",
               [f"  {n:<16} model name \"{profs.route_id(n)}\"" for n in names])
    if k is None:
        return
    store.set_key_profile(db(), live[j]["tag"], names[k])
    write_config()
    primary = profs.primary_adapter(names[k])
    held = live[j].get("provider", "")
    warn = ""
    if held and held != primary:
        # Caddy admits on the hash alone, so a mismatch is only discovered when
        # the provider rejects the forwarded credential — which reads as ours.
        warn = (f"\n! their key is a {providers.get(held).DISPLAY} key, but this profile\n"
                f"  routes to {providers.get(primary).DISPLAY}. The gateway will admit them\n"
                f"  and the provider will then reject the request.\n")
    pager(stdscr, "Reassigned",
          f"{live[j]['tag']} now uses profile {names[k]}.\n\n"
          f"They must send model \"{profs.route_id(names[k])}\".\n" + warn +
          "Restart the router to apply:  docker compose restart switchingyard")


def add_key_wizard(stdscr) -> None:
    """Six steps, nothing written until the last.

    Adding is the only moment the plaintext key exists — after hashing it can
    never be tested again — so it is validated with the provider here.
    """
    prov = provider()

    # 1. system
    names = providers.names()
    i = choose(stdscr, "Which system is this key for?",
               [f"  {providers.get(n).DISPLAY}" for n in names],
               hint="one option today; adapters make the next one cheap")
    if i is None:
        return
    prov = providers.get(names[i])

    # 2. key
    key = prompt(stdscr, f"{prov.DISPLAY} key (hidden): ", hidden=True)
    if not key:
        return
    if not key.startswith(prov.KEY_PREFIX):
        pager(stdscr, "That is not a key for this system",
              f"{prov.DISPLAY} keys start with {prov.KEY_PREFIX!r}.\n\n"
              "Nothing was sent anywhere and nothing was stored.")
        return
    if digest(key) in {e.get("sha256") for e in load_roster()}:
        pager(stdscr, "Already on the roster", "That exact key is already authorised.")
        return

    # 3. validate
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addstr(0, 0, f"Verifying with {prov.DISPLAY}…"[: w - 1])
    stdscr.refresh()
    ok, detail, meta = prov.validate(key)
    verified = bool(ok)
    if ok is False:
        pager(stdscr, "Rejected", f"{detail}\n\nNothing was stored. Check for a typo or a\n"
                                  "truncated paste, or whether the key was revoked upstream.")
        return
    if ok is None:
        lines = [detail, "", "The key could not be checked. It can still be added, and will be",
                 "flagged unverified in the roster — but if it is wrong, the first",
                 "real request is when you find out."]
        pager(stdscr, "Could not verify", "\n".join(lines))
        if not confirm(stdscr, "add it anyway, unverified?"):
            return

    if meta:
        pager(stdscr, "Verified", "\n".join(
            [f"{prov.DISPLAY} accepted the key.", ""] +
            [f"  {k:<12} {v}" for k, v in meta.items()] +
            ["", "Confirm this is the account you expect before continuing."]))

    # 4. tag
    suggested = str(meta.get("label", "")).strip().replace(" ", "-")[:64]
    tag = prompt(stdscr, f"tag — whose key is this? [{suggested or 'e.g. marta'}]: ") or suggested
    if not tag:
        return
    if not TAG_RE.match(tag):
        pager(stdscr, "Unusable tag",
              "Tags may contain letters, digits, dot, underscore and hyphen,\n"
              "up to 64 characters.\n\n"
              "The tag is sent upstream as an HTTP header for attribution, so\n"
              "spaces and control characters are refused rather than escaped.")
        return
    if tag in {e["tag"] for e in load_roster()}:
        pager(stdscr, "Tag already used", f"{tag!r} is already on the roster.")
        return

    # 5. profile — assigned from the newcomer default, not asked
    profs = profiles()
    chosen, intact = newcomer_profile()

    # 6. confirm
    stdscr.erase()
    body = "\n".join([
        f"  tag       {tag}",
        f"  profile   {chosen} → they send model \"{profs.route_id(chosen)}\""
        + ("" if intact else "   (configured newcomer profile is gone)"),
        "" if not routes_gen.unservable(profs, chosen) else
        f"            ! {chosen} serves nothing yet — "
        f"{routes_gen.unservable(profs, chosen)}",
        f"  system    {prov.DISPLAY}",
        "" if prov.NAME == profs.primary_adapter(chosen) else
        f"            ! {chosen} routes to "
        f"{providers.get(profs.primary_adapter(chosen)).DISPLAY}; this key will be\n"
        f"              admitted here and then rejected upstream",
        f"  key       {prov.KEY_PREFIX}…{key[-4:]}",
        f"  digest    {digest(key)[:8]}…",
        "" if verified else "  state     UNVERIFIED",
        "",
        "The key itself is NOT stored. Only the digest is kept, so this key can",
        "be recognised later but never used, read back, or recovered from here.",
    ])
    pager(stdscr, "Confirm", body)
    if not confirm(stdscr, "add this key?"):
        return

    store.add_key(db(), {"tag": tag, "provider": prov.NAME, "sha256": digest(key),
                         "hint": f"…{key[-4:]}", "profile": chosen, "verified": verified,
                         "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    write_config()
    pager(stdscr, "Added", f"{tag} may now call the service, using their own {prov.DISPLAY}\n"
                           f"key and model \"{profs.route_id(chosen)}\".\n\n"
                           "Live immediately — authcheck reads the roster per request.")


def newcomer_profile() -> tuple[str, bool]:
    """Profile a new key is assigned, and whether that was the configured one.

    Falls back to the default profile if the configured one has been deleted,
    rather than refusing to add anyone.
    """
    profs = profiles()
    wanted = gateway_settings().get("newcomer_profile") or profs.default_name
    if wanted in profs.names():
        return wanted, True
    return profs.default_name, False


def action_newcomer(stdscr) -> None:
    """What a newly added key gets, without asking during the wizard."""
    while True:
        profs = profiles()
        current, intact = newcomer_profile()
        rows = [f"  profile for newcomers    {current}"
                + ("" if intact else "   ! configured profile is gone; using the default"),
                "",
                "  New keys are assigned this profile with no prompt. Change anyone",
                f"  afterwards from Authorised keys → p. They send model \"{profs.route_id(current)}\".",
                "",
                "  > choose a profile"]
        idx = choose(stdscr, "Defaults for new users", rows, hint="esc back")
        if idx is None or not rows[idx].strip().startswith(">"):
            return
        names = profs.names()
        j = choose(stdscr, "Newcomers get which profile?",
                   [f"  {n:<16} \"{profs.route_id(n)}\""
                    + ("" if profs.complete(n) else "   ! incomplete") for n in names])
        if j is None:
            continue
        settings = gateway_settings()
        settings["newcomer_profile"] = names[j]
        save_gateway_settings(settings)


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

def action_profiles(stdscr) -> None:
    selected = 0
    while True:
        profs = profiles()
        rows = []
        for n in profs.names():
            missing = profs.missing(n)
            state = (f"{len(SLOTS) - len(missing)}/{len(SLOTS)}"
                     + (f"  ! not serving — missing {', '.join(missing)}" if missing else ""))
            # No user count here: nothing observes who uses a profile — any
            # caller may send any profile's model name — so a number here reads
            # as usage while only counting what the roster was told.
            rows.append(f"  {n:<16} \"{profs.route_id(n):<16}\" {state}"
                        + ("   default" if n == profs.default_name else ""))
        rows += ["", "  + new", "  d duplicate", "  r rename", "  x delete"]
        idx = choose(stdscr, "Profiles — mode→model sets", rows, start=selected,
                     hint="enter to edit models   + d r x   esc back")
        if idx is None:
            return
        selected = idx
        label = rows[idx].strip()
        names = profs.names()
        if idx < len(names):
            edit_profile(stdscr, names[idx])
        elif label.startswith("+") or label.startswith("d "):
            clone = profs.default_name if label.startswith("d ") else None
            if label.startswith("d "):
                j = choose(stdscr, "Duplicate which profile?", [f"  {n}" for n in names])
                if j is None:
                    continue
                clone = names[j]
            new = prompt(stdscr, "new profile name [a-z0-9-]: ")
            if not new:
                continue
            try:
                profs.create(new, clone_from=clone)
                write_config()
            except ValueError as exc:
                pager(stdscr, "Cannot create", str(exc))
        elif label.startswith("r "):
            j = choose(stdscr, "Rename which profile?", [f"  {n}" for n in names])
            if j is None:
                continue
            new = prompt(stdscr, f"new name for {names[j]}: ")
            if not new:
                continue
            assigned = users_of(names[j])
            if assigned and not confirm(stdscr, f"{len(assigned)} key(s) name this profile; "
                                                f"they must send \"{profs.route_id(new)}\" — rename?"):
                continue
            try:
                profs.rename(names[j], new)
            except ValueError as exc:
                pager(stdscr, "Cannot rename", str(exc)); continue
            for e in load_roster():
                if e.get("profile") == names[j]:
                    store.set_key_profile(db(), e["tag"], new)
            write_config()
            pager(stdscr, "Renamed", f"Users of this profile must now send model "
                                     f"\"{profs.route_id(new)}\".")
        elif label.startswith("x "):
            j = choose(stdscr, "Delete which profile?", [f"  {n}" for n in names])
            if j is None:
                continue
            assigned = users_of(names[j])
            if assigned:
                # Not a refusal: profiles are not an isolation boundary and
                # nothing restricts who may use one. The only real consequence
                # is that a model name someone was told to send stops resolving.
                pager(stdscr, "Still named by some keys", "\n".join(
                    [f"These keys record profile '{names[j]}':", ""]
                    + [f"  {t}" for t in assigned]
                    + ["", f"Deleting it means \"{profs.route_id(names[j])}\" stops resolving.",
                       "Anyone sending that model name gets an unknown-model error",
                       "until they are told a new one.", "",
                       "Their keys keep working for every other profile."]))
            if not confirm(stdscr, f"delete profile {names[j]}?"):
                continue
            try:
                profs.delete(names[j]); write_config()
            except ValueError as exc:
                pager(stdscr, "Cannot delete", str(exc))


def _judge_note(profs: Profiles, name: str) -> str:
    """Said wherever the judge is shown: an imageless judge is a deliberate
    trade, and the half that costs something has to travel with it."""
    if profs.judge_kind(name) == "full":
        return ""
    fb = gateway_settings().get("fallback_mode", FALLBACK_DEFAULT)
    return (f"imageless judge — any turn carrying an image is never classified "
            f"and falls open to '{fb}'")


def _slot_row(profs: Profiles, name: str, slot: str, primary: str) -> str:
    """One editor row: model, reasoning level, and the adapter only when it is
    not the profile's own — nine identical adapter labels would be noise, while
    the exception has to be unmissable."""
    model = profs.model_of(name, slot)
    if not model:
        return f"  {slot:<{SLOT_W}}  not set"
    adapter = profs.adapter_of(name, slot)
    mod = providers.get(adapter)
    level = profs.effort_of(name, slot)
    if not mod.levels(model_record(adapter, model)):
        level = "n/a"
    note = ""
    if slot == "judge":
        note = f"   [{profs.judge_kind(name)}]"
    shared = profs.sharing(name, slot)
    if shared:
        note = f"   (shared with {', '.join(shared)})"
    if adapter != primary:
        note = f"   [{mod.DISPLAY} — outside auto]{note}"
    return f"  {slot:<{SLOT_W}}  {model:<44}  {level:<8}{note}"


def edit_profile(stdscr, name: str) -> None:
    """Judge and modes in ONE list, with the judge set apart by a rule.

    The judge is infrastructure, not a task mode — it never serves a request —
    but splitting it into its own menu entry made it feel like a separate thing
    to configure. A separator says the same with less navigation.
    """
    profs = profiles()
    missing = profs.missing(name)
    # open where the work is
    selected = SLOTS.index(missing[0]) + (1 if missing and missing[0] != "judge" else 0) if missing else 0
    while True:
        profs = profiles()
        primary = profs.primary_adapter(name)
        rows = [_slot_row(profs, name, "judge", primary), f"  {SEPARATOR}"]
        rows += [_slot_row(profs, name, slot, primary) for slot in MODES]
        title = f"Profile {name} — clients send \"{profs.route_id(name)}\"  ·  {primary}"
        hint = "enter model   ^E level   esc back"
        if profs.judge_kind(name) != "full":
            hint = "! image turns are not classified   " + hint
        if routes_gen.not_routing(profs, name):
            hint = "! one model everywhere — the judge is never called   " + hint
        away = profs.outside_auto(name)
        if away:
            hint = f"outside auto: {', '.join(away)}   " + hint
        gaps = profs.missing(name)
        if gaps:
            rows += ["", f"  > fill the {len(gaps)} unset slot(s) from another mode"]
        idx = choose(stdscr, title, rows, start=selected, hint=hint,
                     keys=(chr(5),))          # ^E, a control key: letters filter
        if idx is None:
            return
        if isinstance(idx, tuple):
            idx, key = idx
        else:
            key = ""
        selected = idx
        if idx >= len(SLOTS) + 1:
            _fill_missing(stdscr, name)
            continue
        slot = "judge" if idx == 0 else MODES[idx - 2]

        if key == chr(5):                     # ^E — change the level alone
            model = profs.model_of(name, slot)
            if not model:
                continue
            level = pick_effort(stdscr, profs.adapter_of(name, slot), model, slot,
                                profs.effort_of(name, slot))
            if level is not None:
                moved = profs.set_effort(name, slot, level)
                if slot == "judge":
                    _judge_budget_guard(stdscr, level)
                _regenerate(stdscr)
                if moved:
                    pager(stdscr, "Level applies to the whole target",
                          f"{slot} and {', '.join(moved)} share {model}, so they are one\n"
                          f"target and cannot hold two levels — the engine rejects a\n"
                          f"classifier whose targets resolve to the same model twice.\n\n"
                          f"All of them are now {level}.")
            continue

        if slot == "judge":
            kinds = list(profiles_store.JUDGE_KINDS)
            cur = profs.judge_kind(name)
            j = choose(stdscr, "What kind of judge?",
                       [f"  {k:<11} {profiles_store.JUDGE_KIND_NOTE[k]}" for k in kinds],
                       hint="the judge is shown the conversation verbatim, images included",
                       start=kinds.index(cur) if cur in kinds else 0)
            if j is None:
                continue
            if kinds[j] != cur:
                profs.set_judge_kind(name, kinds[j])
        picked = pick_model(stdscr, f"{slot} model for profile {name}", slot=slot,
                            adapter=profs.adapter_of(name, slot), profile=name)
        if not picked:
            continue
        adapter, model = picked
        mod = providers.get(adapter)
        record = model_record(adapter, model)
        level = (mod.judge_level(record) if slot == "judge" else mod.default_level(record))
        profs.set_model(name, slot, model, effort=level, adapter=adapter)
        if slot != "judge":
            # A judge that classified for one adapter cannot be paid for by a
            # caller holding another's key, so it follows the majority.
            new_primary = profs.primary_adapter(name)
            if profs.adapter_of(name, "judge") != new_primary:
                profs.set_adapter(name, "judge", new_primary)
                pager(stdscr, "The judge moved with auto",
                      f"auto now classifies on {new_primary}, and one route carries one\n"
                      f"caller credential — so the judge must sit there too.\n\n"
                      f"Its model was cleared; pick one before this profile serves.")
        if slot != "judge" and mod.levels(record):
            chosen = pick_effort(stdscr, adapter, model, slot, level)
            if chosen is not None:
                profs.set_effort(name, slot, chosen)
        _regenerate(stdscr)
        selected = _nearest(rows, min(idx + 1, len(SLOTS)), 1)


JUDGE_BUDGET_WITH_REASONING = 4096


def _judge_budget_guard(stdscr, level: str) -> None:
    """Give the verdict room once the judge is allowed to think.

    Reasoning and the verdict JSON share one `max_output_tokens`, and the effort
    levels are defined as a share of it — `max` spends about 95% on thinking. A
    1024-token budget leaves roughly 50 tokens for the verdict at that setting,
    and a truncated verdict is unparseable, which fails open to default_target on
    every request while still billing the judge call. Nothing in the response
    says so, which is why this is raised rather than merely warned about.
    """
    if level == profiles_store.JUDGE_LEVEL:
        return
    settings = gateway_settings()
    current = int(settings.get("max_output_tokens", 1024))
    if current >= JUDGE_BUDGET_WITH_REASONING:
        pager(stdscr, "The judge will now think",
              f"Its reasoning and its verdict JSON share max_output_tokens, which\n"
              f"is {current} — enough headroom at {level}.")
        return
    settings["max_output_tokens"] = JUDGE_BUDGET_WITH_REASONING
    save_gateway_settings(settings)
    pager(stdscr, "The judge will now think",
          f"Reasoning and the verdict JSON share one output budget, and the\n"
          f"effort levels are a share of it — at {level} most of the budget goes\n"
          f"on thinking.\n\n"
          f"max_output_tokens raised {current} → {JUDGE_BUDGET_WITH_REASONING} so the verdict still\n"
          f"fits. A truncated verdict is unparseable and routes every request to\n"
          f"the fallback target, silently, while still billing the judge call.\n\n"
          f"Check routing (option 6) after restarting.")


def _regenerate(stdscr) -> None:
    skipped, (ok, detail) = write_config()
    if not ok:
        pager(stdscr, "Generated config is invalid", detail)


def _fill_missing(stdscr, name: str) -> None:
    """Copy one already-set mode's model into every unset slot.

    Adding modes to the taxonomy leaves every existing profile incomplete and
    therefore not serving, so completing it must not mean nine separate picks.
    image-out is excluded: only a handful of models generate images, and
    inheriting a text model there would produce a profile that validates but
    cannot do the job.
    """
    profs = profiles()
    gaps = [s for s in profs.missing(name) if s != "image-out"]
    if not gaps:
        pager(stdscr, "Nothing to fill",
              "Only image-out is unset, and it has no sensible default: pick one\n"
              "of the few models that actually return images.")
        return
    sources = [m for m in MODES if profs.model_of(name, m)]
    if not sources:
        pager(stdscr, "Nothing to copy from", "No mode has a model yet.")
        return
    j = choose(stdscr, f"Fill {', '.join(gaps)} from which mode?",
               [f"  {m:<{SLOT_W}}  {profs.model_of(name, m):<44}  {profs.effort_of(name, m)}"
                for m in sources])
    if j is None:
        return
    source = sources[j]
    model = profs.model_of(name, source)
    adapter = profs.adapter_of(name, source)
    level = profs.effort_of(name, source)
    # A slot's requirements are not waived just because the model is inherited:
    # the picker refuses an incapable model, and so must this. The judge is the
    # one that matters — a judge that cannot emit structured output fails open on
    # every request, and one that cannot read images fails open on every image.
    record = model_record(adapter, model)
    unfit = [s for s in gaps
             if record and not all(record.get(c) for c in profs.requires(name, s))]
    gaps = [s for s in gaps if s not in unfit]
    if not gaps:
        pager(stdscr, "Nothing that model can fill",
              f"{model} does not meet what {', '.join(unfit)} needs:\n"
              + "\n".join(f"    {s:<{SLOT_W}}  {', '.join(CAPABILITY_LABEL[c] for c in profs.requires(name, s))}"
                           for s in unfit)
              + "\n\nPick those slots individually.")
        return
    if not confirm(stdscr, f"set {len(gaps)} slot(s) to {model} at {level}?"):
        return
    for slot in gaps:
        # The judge keeps its own level: its reasoning competes with the verdict
        # JSON for one output budget, so inheriting a mode's level would be the
        # one copy that can break routing outright.
        if slot == "judge":
            record = model_record(adapter, model)
            profs.set_model(name, slot, model, adapter=adapter,
                            effort=providers.get(adapter).judge_level(record))
        else:
            profs.set_model(name, slot, model, effort=level, adapter=adapter)
    skipped, (ok, detail) = write_config()
    still = profs.missing(name)
    pager(stdscr, "Filled",
          f"{len(gaps)} slot(s) now use {model} at {level}.\n\n"
          + ("The judge kept its own reasoning level: its thinking competes with\n"
             "the verdict JSON for one budget.\n\n" if "judge" in gaps else "")
          + (f"Left alone — {model} cannot do what they need: {', '.join(unfit)}\n\n"
             if unfit else "")
          + (f"Still unset: {', '.join(still)}\n\n" if still else "")
          + ("Config regenerated and valid." if ok else f"Config INVALID: {detail}"))


# Where an unparseable verdict lands. `simple` and not `coder`: fail-open is
# silent, so it happens at full traffic before anyone notices, and the mode it
# lands on should be the cheapest that can answer anything — not the dearest.
FALLBACK_DEFAULT = "simple"

# Routing behaviour. None of this was reachable from the TUI, including the
# judge's output budget, which is the setting most likely to break routing.
ROUTING_FIELDS = [
    ("fallback_mode", "fail-open mode",
     "where an unparseable verdict lands — silently, on every failure"),
    ("max_output_tokens", "judge output budget",
     "shared by the judge's reasoning and its verdict; too small truncates it"),
    ("recent_turn_window", "turns shown to the judge",
     "messages after the opening task; the judge never sees the system prompt"),
]
ROUTING_DEFAULTS = {"fallback_mode": FALLBACK_DEFAULT, "max_output_tokens": 1024,
                    "recent_turn_window": 4}


def routing_settings() -> dict:
    s = dict(ROUTING_DEFAULTS)
    s.update({k: v for k, v in gateway_settings().items() if k in ROUTING_DEFAULTS})
    return s


def action_routing(stdscr) -> None:
    """The four settings that decide how classification behaves."""
    selected = 0
    while True:
        cur = routing_settings()
        profs = profiles()
        rows = [f"  {label:<26} {cur[key]}" for key, label, _ in ROUTING_FIELDS]
        fb = cur["fallback_mode"]
        rows += ["", f"  on failure every request goes to '{fb}' —",
                 f"  {profs.model_of(profs.default_name, fb) or 'not set'} "
                 f"in profile '{profs.default_name}'"]
        rows += ["",
                 "  every turn is classified. Session affinity — reusing one",
                 "  decision for a whole conversation — is not offered: it means",
                 "  the judge does not decide, which is the point of this service."]
        idx = choose(stdscr, "Routing behaviour", rows, start=selected,
                     hint="enter to change   esc back   (restart the router to apply)")
        if idx is None:
            return
        if idx >= len(ROUTING_FIELDS):
            continue
        selected = idx
        key, label, hint = ROUTING_FIELDS[idx]
        settings = gateway_settings()
        if key == "fallback_mode":
            j = choose(stdscr, "Fail open to which mode?",
                       [f"  {m:<{SLOT_W}}  {profs.model_of(profs.default_name, m) or 'not set'}"
                        for m in MODES],
                       hint="a failed verdict is invisible — choose something cheap",
                       start=MODES.index(fb) if fb in MODES else 0)
            if j is None:
                continue
            settings[key] = MODES[j]
        else:
            value = prompt(stdscr, f"{label} ({hint}) [{cur[key]}]: ")
            if not value:
                continue
            try:
                settings[key] = int(value)
            except ValueError:
                continue
        save_gateway_settings(settings)


GATEWAY_FIELDS = [
    ("max_body", "max request body", "Caddy rejects larger; Switchyard's own limit is 32MB"),
    ("read_timeout", "upstream timeout", "how long a completion may take, e.g. 900s"),
    ("roll_size", "access log roll at", "Caddy rolls its own log at this size, e.g. 100MiB"),
    ("roll_keep", "access logs kept", "how many rolled files Caddy retains"),
    ("roll_keep_days", "access logs kept for", "days before a rolled file is deleted"),
    ("routing_log_max_gib", "routing log cap (GiB)", "the accounting sidecar rotates above this"),
    ("public_url", "public URL", "what clients connect to, e.g. https://switchyard.example.com"),
]


def endpoint() -> tuple[str, str, str]:
    """Where the gateway listens, and what a reverse proxy should target.

    Comes from compose via SWITCHYARD_PUBLISHED, so editing the ports line there
    updates this too.
    """
    published = os.environ.get("SWITCHYARD_PUBLISHED", "127.0.0.1:4000:4000")
    parts = published.split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "0.0.0.0", parts[0], parts[1]
    return "127.0.0.1", "4000", published


def base_url() -> str:
    settings = gateway_settings()
    public = (settings.get("public_url") or "").rstrip("/")
    if public:
        return public
    host_ip, host_port, _ = endpoint()
    return f"http://{host_ip}:{host_port}"


def client_lines() -> list[str]:
    """How to point a client at this service, per profile."""
    profs = profiles()
    base = base_url()
    roster = [e for e in load_roster() if not e.get("revoked")]
    out = [
        "clients            (full setup under 'Connect a client')",
        "",
        "  all three protocols are served at once — use whichever your tool speaks",
        f"    OpenAI Chat         POST  {base}/v1/chat/completions",
        f"    OpenAI Responses    POST  {base}/v1/responses",
        f"    Anthropic Messages  POST  {base}/v1/messages",
        "",
        f"  api_key     each user's OWN {provider().DISPLAY} key — the server has none",
        "  model       one per profile:",
    ]
    for n in profs.names():
        users = [e["tag"] for e in roster if e.get("profile", "default") == n]
        state = "" if profs.complete(n) else "   ! incomplete, will not serve"
        out.append(f"                {profs.route_id(n):<16} {', '.join(users) or 'nobody'}{state}")
    out += ["              or pin a mode: " + ", ".join(MODES)]
    if not (gateway_settings().get("public_url") or ""):
        out += ["", "  That base URL is this host only. Set 'public URL' in Gateway",
                "  configuration to the address your reverse proxy serves."]
    return out

def _size(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1e6:.1f} MB"
    except OSError:
        return "absent"


def action_gateway(stdscr) -> None:
    """Gateway settings, generated config, and log sizes."""
    while True:
        settings = gateway_settings()
        ntok = len(active_tags())
        ok, detail = validate_caddy()
        rows = [f"  {label:<24} {settings.get(key)}" for key, label, _ in GATEWAY_FIELDS]
        rows += ["",
                 f"  keys                     {ntok} active"
                 + ("" if ntok else "   ! nothing can call the service"),
                 f"  config                   {'valid' if ok else 'INVALID — ' + detail}",
                 f"  gateway access log       {_size(GATEWAY_LOG)}  (Caddy rolls this)",
                 f"  routing log              {_size(ROUTING_LOG)}  (sidecar rotates this)",
                 "",
                 "  > how do clients connect?",
                 "  > view generated Caddyfile",
                 "  > re-validate"]
        idx = choose(stdscr, "Gateway configuration", rows,
                     hint="enter to edit a value   esc back   (restart the gateway to apply)")
        if idx is None:
            return
        if idx < len(GATEWAY_FIELDS):
            key, label, hint = GATEWAY_FIELDS[idx]
            value = prompt(stdscr, f"{label} ({hint}) [{settings.get(key)}]: ")
            if value:
                if isinstance(caddyfile.DEFAULTS.get(key), bool):
                    pass
                elif isinstance(caddyfile.DEFAULTS.get(key), int):
                    try:
                        value = int(value)
                    except ValueError:
                        continue
                elif isinstance(caddyfile.DEFAULTS.get(key), float):
                    try:
                        value = float(value)
                    except ValueError:
                        continue
                settings[key] = value
                save_gateway_settings(settings)
                ok, detail = validate_caddy()
                if not ok:
                    pager(stdscr, "That change produced an invalid config",
                          f"{detail}\n\nThe value was saved anyway so you can correct it.\n"
                          "Do NOT restart the gateway until this validates.")
        elif rows[idx].strip().startswith("> how do clients connect"):
            pager(stdscr, "Connecting a client", "\n".join(client_help()))
        elif rows[idx].strip().startswith("> view"):
            pager(stdscr, str(CADDYFILE), CADDYFILE.read_text() if CADDYFILE.exists()
                  else "not generated yet — add a key")
        elif rows[idx].strip().startswith("> re-validate"):
            ok, detail = validate_caddy()
            pager(stdscr, "Validation", ("Valid configuration." if ok else f"INVALID\n\n{detail}")
                  + "\n\nApply with:  docker compose restart gateway")


def _opencode_config(profs, base: str) -> str:
    """A complete, valid opencode.json for this deployment.

    Built with json.dumps rather than assembled by hand: this text is meant to
    be pasted verbatim, and a config that does not parse fails silently — the
    provider simply never appears in /models.

    ONE model: the profile's `auto`. The per-mode route ids exist for debugging
    and for deliberately skipping the judge, but they are pieces of this profile,
    not alternatives to it — listing them in a client's picker invites hand-
    picking a mode, which is the job this router exists to do.
    """
    d = profs.default_name
    models = {profs.route_id(d): {"name": "Auto (classified)"}}
    ctx = min([c for c in (_spec(profs, m, "ctx") for m in MODES) if c] or [0])
    out_max = min([o for o in (_spec(profs, m, "max_out") for m in MODES) if o] or [0])
    if ctx:
        # The SMALLEST across the modes: "auto" may land on any of them, so the
        # honest bound is the most constrained one.
        models[profs.route_id(d)]["limit"] = ({"context": ctx, "output": out_max}
                                              if out_max else {"context": ctx})
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "switchyard": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Switchyard",
                "options": {"baseURL": f"{base}/v1"},
                "models": models,
            }
        }
    }, indent=2)


def _spec(profs, mode: str, field: str) -> int:
    d = profs.default_name
    return model_record(profs.adapter_of(d, mode), profs.model_of(d, mode)).get(field) or 0


def client_help() -> list[str]:
    """Copy-pasteable setup. Each user supplies their own provider key."""
    profs = profiles()
    base = base_url()
    prov = provider()
    model = profs.route_id(profs.default_name)
    lines = [
        f"Every user authenticates with their OWN {prov.DISPLAY} key and is billed",
        "on their own account. The server holds no credential.",
        "",
        f"  get a key   {prov.CONSOLE_URL}",
        "  then ask the operator to authorise it — they store only a hash",
        "",
        "Both header forms work:  authorization: Bearer <key>   ·   x-api-key: <key>",
        "",
        "Model name selects the profile you were assigned:",
    ]
    for n in profs.names():
        lines.append(f"    {profs.route_id(n):<16} profile {n}")
    lines += ["", "=" * 66, "", "curl", "",
              f"  curl {base}/v1/chat/completions \\",
              f"    -H 'authorization: Bearer {prov.KEY_PREFIX}YOUR-OWN-KEY' \\",
              "    -H 'content-type: application/json' \\",
              '    -d \'{"model":"' + model + '","messages":[{"role":"user","content":"hi"}]}\'',
              "", "=" * 66, "", "OpenCode", "",
              "  1. /connect  ->  Other  ->  id 'switchyard'  ->  paste YOUR key",
              "     Stored in ~/.local/share/opencode/auth.json, not the config.",
              "", "  2. ~/.config/opencode/opencode.json — the WHOLE file.",
              "     The \"provider\" wrapper is required: a block pasted at the top",
              "     level is silently ignored and the provider never appears.", ""]
    lines += ["    " + l for l in _opencode_config(profs, base).splitlines()]
    lines += ["", "  3. /models  ->  switchyard/" + model,
              "     Models listed here appear as soon as the file is saved.",
              "     `opencode models --refresh` refreshes the models.dev catalogue,",
              "     which this provider is not in — it will not help.",
              "", "=" * 66, "", "Claude Code", "",
              f"  export ANTHROPIC_BASE_URL={base}",
              f"  export ANTHROPIC_AUTH_TOKEN={prov.KEY_PREFIX}YOUR-OWN-KEY",
              f"  export ANTHROPIC_MODEL={model}",
              "", "=" * 66, "", "OpenAI SDK", "",
              f"  export OPENAI_BASE_URL={base}/v1",
              f"  export OPENAI_API_KEY={prov.KEY_PREFIX}YOUR-OWN-KEY",
              "",
              "If your key is rotated or revoked upstream, access stops and the",
              "operator must authorise the new one — they cannot detect it here,",
              "because only a hash is kept.",
              ]
    return lines

def action_connect(stdscr) -> None:
    """How to point a client at this service — the most-wanted screen."""
    pager(stdscr, "Connecting a client", "\n".join(client_help()))


def action_summary(stdscr) -> None:
    profs = profiles()
    roster = load_roster()
    host_ip, host_port, container_port = endpoint()
    ok_routes, routes_detail = validate_routes()
    ok_caddy, caddy_detail = validate_caddy()

    lines = [
        f"provider     {provider().DISPLAY} — caller-pays, no key on this server",
    ] + [f"catalogue    {a}: "
         f"{len(store.catalogue(db(), a))} models, {catalogue_age(a)}"
         for a in adapters_in_use()] + [
        f"routes.toml  {'valid' if ok_routes else 'INVALID — ' + routes_detail}",
        f"Caddyfile    {'valid' if ok_caddy else 'INVALID — ' + caddy_detail}",
        "",
        "profiles",
    ]
    for n in profs.names():
        reason = routes_gen.unservable(profs, n)
        users = [e["tag"] for e in roster if e.get("profile", "default") == n and not e.get("revoked")]
        primary = profs.primary_adapter(n)
        lines.append(f"  {n:<14} \"{profs.route_id(n):<14}\" "
                     f"{len(users)} key(s) assigned  {primary}"
                     + (f"   ! {reason}" if reason else ""))
        for slot in SLOTS:
            lines.append("      " + _slot_row(profs, n, slot, primary).strip())
        away = profs.outside_auto(n)
        if away:
            lines.append(f"      (outside auto, reachable only by name: {', '.join(away)})")
        idle = routes_gen.not_routing(profs, n)
        if idle:
            lines.append(f"      ! {idle}")
        jn = _judge_note(profs, n)
        if jn:
            lines.append(f"      ! {jn}")
    lines += ["", "authorised keys"]
    for e in roster:
        state = ("revoked" if e.get("revoked")
                 else "unverified" if not e.get("verified", True) else "active")
        lines.append(f"  {e['tag']:<18} {e.get('hint','')}  {e.get('profile','default'):<12} {state}")
    if not roster:
        lines.append("  (none — nobody can call this service)")

    lines += ["", "endpoint",
              f"  in container       0.0.0.0:{container_port}",
              f"  on the host        {host_ip}:{host_port}",
              f"  from a container   switchyard-gateway:4000   (the 'proxy' bridge)",
              f"  reachable now      {probe_base() or 'not responding — is the stack up?'}",
              ""] + client_lines()
    pager(stdscr, "Current configuration", "\n".join(lines))

PROBES = [
    ("simple", "What is the capital of Peru?"),
    ("toolcall", "Read the file /etc/hosts and tell me what is in it."),
    ("coder", "This Rust function deadlocks under load. Find the bug and fix it."),
    ("planner", "Break the migration of a monolith to services into ordered steps with milestones."),
    ("researcher", "Summarise and reconcile what these twelve papers claim about scaling laws."),
    ("reasoner", "Prove that the square root of two is irrational, showing every step."),
    ("image-in", "Look at the screenshot I attached and tell me which button is misaligned."),
    ("image-out", "Draw me a picture of a lighthouse at dusk, in watercolour."),
    ("narrative", "Write the opening scene of a short story about a lighthouse keeper."),
    ("user-interface", "The settings page feels cramped on mobile — restyle the form so the "
                       "labels and inputs stack cleanly and the tap targets are big enough."),
    ("security", "Review this login handler for vulnerabilities: it builds the SQL query by "
                 "concatenating the username, and compares the password with =="),
    ("debug", "This test started failing after the last deploy with "
              "'KeyError: session_id' at handler.py:212. The traceback is below. Why?"),
]


# --------------------------------------------------------------------------
# is the router serving what we last wrote?
# --------------------------------------------------------------------------

def running_routes(base: str | None = None) -> set[str] | None:
    """Route ids the running router has LOADED, or None if it cannot be asked.

    None means "unknown", never "none" — a caller must not read a failed lookup
    as an empty config.
    """
    base = base or probe_base()
    if not base:
        return None
    try:
        with urllib.request.urlopen(f"{base}{upstream.MODELS_PATH}", timeout=5) as r:
            payload = json.load(r)
        return {m[upstream.MODELS_ID_FIELD] for m in payload.get("data", [])
                if isinstance(m.get(upstream.MODELS_ID_FIELD), str)}
    except Exception:
        return None


def stream_errors() -> tuple[int, dict[str, int]]:
    """Streams that broke after the response headers were already sent.

    This is the ONLY place such a failure is counted. The request log records
    status and error when the Response object is built — for a streamed reply
    that is before a single body byte exists — so a stream that dies two chunks
    in is logged as `status=200 error=""`. The routing log misses it too:
    `observe` returns early on failure without appending a record.

    Client-side it looks like a connection reset, because the router emits an
    error frame and closes WITHOUT the terminating `[DONE]` (`sse.rs:41-48`).
    """
    base = probe_base()
    if not base:
        return 0, {}
    try:
        with urllib.request.urlopen(f"{base}/v1/stats", timeout=5) as r:
            d = json.load(r)
        by_model = {m: st.get("errors", 0) for m, st in (d.get("models") or {}).items()
                    if st.get("errors")}
        return int(d.get("total_errors", 0)), by_model
    except Exception:
        return 0, {}


def judge_coverage() -> dict:
    """How often the judge actually decided, and why it did not.

    "Is the judge working?" has been answered here by inference too many times.
    There are six distinct ways a request reaches a model without the judge
    choosing it, and only counting separates them:

      served      requests the router handled
      judged      judge calls it made          (/v1/stats classifier)
      fail_open   judge calls whose verdict was discarded  (/metrics)
      passthrough profiles that classify nothing by construction
      pinned      clients addressing a mode directly, skipping the judge
      affinity    reuse of an earlier decision within a session

    `served - judged` is the count that matters: every one of those is a
    request the judge never saw.
    """
    out = {"served": 0, "judged": 0, "fail_open": 0, "reasons": []}
    base = probe_base()
    if not base:
        return out
    try:
        with urllib.request.urlopen(f"{base}/v1/stats", timeout=5) as r:
            d = json.load(r)
        out["served"] = int(d.get("total_requests", 0))
        out["judged"] = int((d.get("classifier") or {}).get("total_requests", 0))
    except Exception:
        return out
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as r:
            for line in r.read().decode("utf-8", "replace").splitlines():
                # switchyard_classifier_fail_open_total{judge_model="…",reason="…"} 3
                if line.startswith("switchyard_classifier_fail_open") and not line.startswith("#"):
                    try:
                        out["fail_open"] += int(float(line.rsplit(" ", 1)[1]))
                    except Exception:
                        pass
    except Exception:
        pass

    profs = profiles()
    settings = routing_settings()
    for n in profs.names():
        idle = routes_gen.not_routing(profs, n)
        if idle:
            out["reasons"].append(f"'{n}' classifies nothing — {idle}")
        away = profs.outside_auto(n)
        if away:
            out["reasons"].append(f"'{n}': {', '.join(away)} sit outside auto and "
                                  f"are reachable only by name — the judge cannot pick them")
    if out["served"] > out["judged"] and not out["reasons"]:
        out["reasons"].append("clients are addressing a mode directly (e.g. \"coder\") "
                              "instead of \"auto\" — a pinned route never calls the judge")
    return out


def _router_started() -> float | None:
    """When the router process started, if this TUI shares its container.

    `docker compose exec` runs alongside the server, so PID 1 IS the router and
    its start time is readable. `docker compose run --rm` starts a separate
    container where PID 1 is this TUI — then there is nothing to compare and we
    say so rather than guess.
    """
    try:
        if "switchyard-server" not in Path("/proc/1/cmdline").read_bytes().decode(errors="replace"):
            return None
        return Path("/proc/1").stat().st_mtime
    except Exception:
        return None


CONFIG_FINGERPRINT_KEY = "router_config"


def config_fingerprint(text: str | None = None) -> str:
    if text is None:
        text = ROUTES.read_text() if ROUTES.exists() else ""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def mark_router_started() -> None:
    """Record the config the router is about to load.

    Called from the entrypoint's SERVER branch only, right after the config is
    regenerated and immediately before exec. It must not run on the `manage`
    branch: the TUI regenerates too, and recording there would claim the running
    router had loaded a file it has never seen.
    """
    store.save_settings(db(), {CONFIG_FINGERPRINT_KEY: config_fingerprint()})


def stale_config() -> str:
    """Why the running router is not serving what we last wrote, or "".

    Three signals, in order of exactness, because the obvious one is not enough:

    * the fingerprint recorded when the router last started is exact. It is the
      only signal that catches a change which rewrites targets while leaving the
      route ids alone — which is most of them, including the one that hid a dead
      judge: a passthrough config advertises exactly the same ten ids as the
      classifier config that replaced it.
    * the route-id set, as a fallback when no fingerprint was recorded — a
      router started by hand rather than by our entrypoint.
    * the file being newer than the process, when the TUI shares its container.

    Never reports stale on missing evidence: an unreachable router is unknown.
    """
    if not ROUTES.exists():
        return ""
    if running_routes() is None:
        return ""           # not reachable: unknown, not stale
    loaded = store.settings(db()).get(CONFIG_FINGERPRINT_KEY)
    if loaded:
        if loaded != config_fingerprint():
            return "the router is serving an older config"
        return ""
    generated = routes_gen.route_ids(ROUTES.read_text())
    running = running_routes()
    if running is not None and running != generated:
        return "the router is serving a different route set"
    started = _router_started()
    if started is not None and ROUTES.stat().st_mtime > started:
        return "the router started before the current config was written"
    return ""


def probe_base() -> str | None:
    """Find the running server: inside its container, or across the compose network."""
    for base in ("http://localhost:4000", "http://switchingyard:4000", "http://127.0.0.1:4000"):
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=3):
                return base
        except Exception:
            continue
    return None


def probe_once(base: str, text: str, model_name: str = "auto",
               key: str = "") -> tuple[str | None, str | None]:
    """Send one classified request. Returns (served model id, error)."""
    body = json.dumps({
        "model": model_name,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 1,          # the answer is irrelevant; keep the served call minimal
    }).encode()
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            served = r.headers.get(SELECTED_HEADER)
            if served:
                return served, None
            payload = json.load(r)
            return payload.get("model"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# The judge's response_format, verbatim from classifier_contract.rs:114-133.
# Switchyard always sends strict json_schema and then validates the reply
# against the same schema locally, so a provider that merely ACCEPTS the
# parameter without honouring it fails just as surely as one that rejects it.
JUDGE_SCHEMA_NAME = "switchyard_classifier_response"


# Roughly four characters per token. The judge is shown the conversation
# verbatim, so the size that matters is the real one, not a one-line probe.
FILLER = ("def handle(request):\n    # tool result follows\n"
          "    payload = json.loads(request.body)\n"
          "    return dispatch(payload, timeout=30)\n\n")


def _bulk(tokens: int) -> str:
    return FILLER * max(1, (tokens * 4) // len(FILLER))


def _ask_judge(profile: str, key: str, text: str) -> tuple[str | None, str]:
    """One judge call, exactly as Switchyard makes it. Returns (label, detail).

    label is None when the verdict is unusable; detail always says why, so the
    caller never has to infer a cause from a missing answer.
    """
    profs = profiles()
    adapter = profs.adapter_of(profile, "judge")
    mod = providers.get(adapter)
    grouped = routes_gen.groups(profs, profile, profs.auto_modes(profile))
    schema = json.loads(routes_gen.response_schema(grouped, profile))
    prompt_text = (profs.get(profile).get("prompt")
                   or routes_gen.build_prompt(grouped, profile)).strip()
    labels = schema["properties"]["decision"]["properties"]["mode"]["enum"]
    body = json.dumps({
        "model": profs.model_of(profile, "judge"),
        "messages": [{"role": "system", "content": prompt_text},
                     {"role": "user", "content": text}],
        "max_tokens": int(gateway_settings().get("max_output_tokens", 1024)),
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": JUDGE_SCHEMA_NAME,
                                            "strict": True, "schema": schema}},
    }).encode()
    req = urllib.request.Request(f"{mod.BASE_URL}/chat/completions", data=body,
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:120]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    choice = (payload.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        return None, f"empty verdict (finish={choice.get('finish_reason')})"
    try:
        chosen = (json.loads(content).get("decision") or {}).get("mode")
    except Exception as exc:
        return None, f"not JSON ({exc}): {content[:60]}"
    if chosen not in labels:
        return None, f"{chosen!r} is not one of the targets"
    return chosen, "ok"


def judge_discriminate(profile: str, key: str, on_step=None) -> tuple[list[tuple], list[str]]:
    """Ask the judge every probe prompt DIRECTLY, with no router in between.

    This is the measurement that separates "the judge cannot tell these apart"
    from "the judge is fine and the routing is not using it". Nothing else here
    can distinguish those, because every other signal is observed downstream of
    the very component in question.
    """
    profs = profiles()
    label_of = {}
    for lbl, _model, modes, *_ in routes_gen.groups(profs, profile, profs.auto_modes(profile)):
        label_of[f"{profile}_{lbl}"] = "/".join(modes)
    rows = []
    for i, (expected, text) in enumerate(PROBES):
        if on_step:
            on_step(i, expected)
        chosen, detail = _ask_judge(profile, key, text)
        rows.append((expected, chosen, label_of.get(chosen or "", ""), detail))
    out = [f"{'probe asked for':<16} {'judge chose':<30} {'= mode'}", ""]
    for expected, chosen, modes, detail in rows:
        if chosen is None:
            out.append(f"{expected:<16} FAILED  {detail}")
        else:
            out.append(f"{expected:<16} {chosen:<30} {modes}")
    return rows, out


def judge_check(profile: str, key: str) -> tuple[bool, list[str]]:
    """Ask the judge exactly what Switchyard asks it, and report what came back.

    Every other check here infers the judge's health from where requests were
    routed, which cannot tell "the judge said coder" from "the judge failed and
    coder is the fallback". This calls the judge directly, so the answer is the
    provider's own rather than an inference from routing.
    """
    profs = profiles()
    adapter = profs.adapter_of(profile, "judge")
    mod = providers.get(adapter)
    model = profs.model_of(profile, "judge")
    grouped = routes_gen.groups(profs, profile, profs.auto_modes(profile))
    schema = json.loads(routes_gen.response_schema(grouped, profile))
    prompt_text = (profs.get(profile).get("prompt")
                   or routes_gen.build_prompt(grouped, profile)).strip()
    labels = schema["properties"]["decision"]["properties"]["mode"]["enum"]

    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt_text},
                     {"role": "user", "content": "Write a Python function that reverses a list."}],
        "max_tokens": int(gateway_settings().get("max_output_tokens", 1024)),
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": JUDGE_SCHEMA_NAME,
                                            "strict": True, "schema": schema}},
    }).encode()
    req = urllib.request.Request(f"{mod.BASE_URL}/chat/completions", data=body,
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {key}"})
    out = [f"judge      {model}", f"adapter    {mod.DISPLAY}", ""]
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        out += [f"REJECTED — HTTP {e.code}", "", detail[:600], ""]
        if e.code == 404:
            out += ["A 404 here is not a typo in the model name — it is the provider",
                    "saying it has no endpoint it will serve YOU for this model.",
                    "Usually one of:", "",
                    "  · your account's data policy excludes every provider of it",
                    "    (openrouter.ai/settings/privacy)",
                    "  · the model is listed but has no live provider right now",
                    "  · it needs credit or an account setting you have not enabled",
                    "",
                    "The catalogue says the model exists and is capable; it does not",
                    "know what your account may reach. Pick another judge."]
        else:
            out += ["The provider refused the request Switchyard makes. This model",
                    "cannot serve as a judge here, whatever its catalogue entry says."]
        return False, out
    except Exception as e:
        out += [f"COULD NOT ASK — {type(e).__name__}: {e}"]
        return False, out

    choices = payload.get("choices") or [{}]
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    usage = payload.get("usage") or {}
    out += [f"accepted   HTTP 200   {usage.get('completion_tokens', '?')} completion token(s)",
            f"finish     {choices[0].get('finish_reason')}", "", "verdict returned:", ""]
    out += ["  " + l for l in (content or "(empty)").splitlines()[:12]] + [""]

    if not content:
        out += ["EMPTY VERDICT.",
                "Nothing to parse, so every request falls open to the fallback.",
                "If finish is 'length' the budget was spent before the answer:",
                "raise max_output_tokens, or lower the judge's reasoning level."]
        return False, out
    try:
        verdict = json.loads(content)
    except Exception as exc:
        out += [f"NOT JSON — {exc}",
                "Switchyard parses this reply; anything unparseable fails open.",
                "The provider accepted strict json_schema without honouring it."]
        return False, out
    chosen = (verdict.get("decision") or {}).get("mode")
    if chosen not in labels:
        out += [f"WRONG SHAPE — mode {chosen!r} is not one of the targets.",
                "", "expected one of:"] + [f"  {l}" for l in labels]
        out += ["", "Switchyard matches this string against the target table names,",
                "so anything else falls open to the fallback."]
        return False, out
    out += [f"VALID at ~40 tokens — the judge chose {chosen!r}.", ""]

    # A one-line prompt proves the contract, not the judge. Real turns carry a
    # whole transcript, and an empty completion under load is the failure the
    # router reports as `judge verdict unavailable ... EOF while parsing`.
    out += ["Now at a realistic size, which is where it actually runs:", ""]
    sizes = [4_000, 16_000, 48_000]
    worked_big = True
    for want in sizes:
        big = ("Here is the current file, then my question at the end.\n\n"
               + _bulk(want) + "\nNow write the next function.")
        chosen_big, detail_big = _ask_judge(profile, key, big)
        if chosen_big:
            out.append(f"  ~{want:>6,} tokens   ok — chose {chosen_big}")
        else:
            worked_big = False
            out.append(f"  ~{want:>6,} tokens   FAILED — {detail_big}")
    out.append("")
    if worked_big:
        out += ["The judge holds up at size. If routing still misbehaves, the",
                "cause is downstream of the judge, not the judge."]
        return True, out
    out += ["THE JUDGE FAILS AT SIZE.", "",
            "It answers a short prompt and returns nothing on a real one. Every",
            "such turn falls open to the fallback, after paying for the judge",
            "call and waiting for it.", "",
            "  · lower 'turns shown to the judge' (Routing behaviour) so it",
            "    reads less of the transcript — the opening task always goes,",
            "    so this helps only if the tail is what is too big",
            "  · otherwise the judge model or its provider cannot take the",
            "    input: choose another and run this again"]
    return False, out


def action_probe(stdscr) -> None:
    """Actively classify one known prompt per mode and report where each went.

    Self-contained: it generates its own traffic rather than waiting for someone
    to have used the server first.
    """
    # Nothing measured here can mean anything if the process is serving a
    # different config. Check BEFORE spending money on probes: this exact case
    # once produced a confident "the judge is failing" about a judge that was
    # never called.
    stale = stale_config()
    if stale:
        pager(stdscr, "Not testing — the config is not applied", "\n".join([
            f"! {stale}.", "",
            "The router reads its config once, at startup. Probing now would",
            "measure the OLD routing and report it as this profile's behaviour.",
            "", "Apply first:", "",
            "    docker compose restart switchingyard", "",
            "then run this test again.",
        ]))
        return

    profs = profiles()
    ready = [n for n in profs.names() if not routes_gen.unservable(profs, n)]
    if not ready:
        pager(stdscr, "Not configured yet",
              f"No profile is servable, so nothing can be probed.")
        return
    i = choose(stdscr, "Test which profile?",
               [f"  {n:<14} \"{profs.route_id(n)}\"" for n in ready],
               hint="each profile is a separate mapping and needs its own check")
    if i is None:
        return
    prof = ready[i]
    model_name = profs.route_id(prof)

    base = probe_base()
    if base is None:
        pager(stdscr, "Server not reachable", "\n".join([
            "Could not reach the router on :4000.",
            "",
            "Start it first:   docker compose up -d",
            "",
            "If you launched this TUI with 'docker compose run', the server runs in a",
            "separate container; 'docker compose exec switchingyard manage' attaches to the",
            "running one instead.",
        ]))
        return

    h, w = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addstr(0, 0, "Routing self-test"[: w - 1], curses.A_REVERSE)
    stdscr.addstr(2, 0, f"Sending {len(PROBES)} classified requests to {base} "
                        f"as model \"{model_name}\"."[: w - 1])
    # `max_tokens: 1` bounds the ANSWER, not the thinking. A mode on a reasoning
    # model spends its whole reasoning budget before emitting that one token, so
    # a probe there can cost far more than the judge call beside it — measured at
    # 1548 output tokens on one run against a mode set to high.
    stdscr.addstr(3, 0, "Each costs a judge call plus a 1-token completion — but a mode"[: w - 1])
    stdscr.addstr(4, 0, "on a reasoning model still pays for its reasoning first."[: w - 1])
    stdscr.addstr(6, 0, "The gateway requires a key, and the server holds none — paste"[: w - 1])
    stdscr.addstr(7, 0, "one authorised key to run the test with."[: w - 1])
    stdscr.refresh()
    probe_key = prompt(stdscr, "key for this test (hidden, not stored): ", hidden=True)
    if not probe_key:
        return

    # Which mode(s) does a served model id correspond to? Modes may share one.
    by_model: dict[str, list[str]] = {}
    for mode in MODES:
        by_model.setdefault(profs.model_of(prof, mode), []).append(mode)

    # The routing log records every call the ROUTER made, tagged judge or
    # served. Reading it across the probe run turns "did it consult the judge?"
    # from an inference into a count. Caveat: a judge call that FAILS is never
    # logged upstream, so zero means "no judge call succeeded", not "none was
    # attempted" — which is why the direct judge test below still matters.
    log_start = ROUTING_LOG.stat().st_size if ROUTING_LOG.exists() else 0

    results: list[tuple[str, str | None, str | None]] = []
    for i, (expected, text) in enumerate(PROBES):
        stdscr.erase()
        stdscr.addstr(0, 0, "Routing self-test"[: w - 1], curses.A_REVERSE)
        for j, (mode, _) in enumerate(PROBES):
            if j < len(results):
                served, err = results[j][1], results[j][2]
                got = "/".join(by_model.get(served, [])) or (served or "?")
                line = f"  {mode:<12} -> {'ERROR  ' + err if err else got}"
            elif j == i:
                line = f"  {mode:<12} -> ..."
            else:
                line = f"  {mode:<12}"
            stdscr.addstr(j + 2, 0, line[: w - 1])
        stdscr.addstr(len(PROBES) + 3, 0, f"{i + 1}/{len(PROBES)}"[: w - 1], curses.A_DIM)
        stdscr.refresh()
        served, err = probe_once(base, text, model_name, probe_key)
        results.append((expected, served, err))

    # Report
    served_models = {r[1] for r in results if r[1]}
    errors = [r for r in results if r[2]]
    judged = served_calls = 0
    try:
        with ROUTING_LOG.open() as fh:
            fh.seek(log_start)
            for line in fh:
                try:
                    tier = str(upstream.field(json.loads(line), "tier", ""))
                except Exception:
                    continue
                if tier == upstream.TIER_JUDGE:
                    judged += 1
                else:
                    served_calls += 1
    except Exception:
        judged = served_calls = -1

    # What the judge DECIDED is the useful column. The served model is how we
    # know it — the router names it in a response header — but the operator is
    # reading this to see the judge's choices, so lead with those. A mismatch
    # against the probe's own label is visible by reading across; it is not a
    # failure and does not need announcing.
    # Column names must not claim more than was measured. This is the model the
    # router served and the mode it corresponds to; whether a JUDGE chose it is
    # a separate fact, settled by the call count below. Labelling this column
    # "judge chose" would assert exactly what is in question.
    lines = [f"{'probe asked for':<16} {'resolved to':<26} {'served by'}", ""]
    for expected, served, err in results:
        if err:
            lines.append(f"{expected:<16} ERROR  {err}")
            continue
        modes = by_model.get(served, [])
        chose = "/".join(modes) if modes else "? (not a model in this profile)"
        lines.append(f"{expected:<16} {chose:<26} {served}")

    lines += ["", "-" * 60, ""]
    if judged >= 0:
        lines += [f"The router logged {judged} judge call(s) and {served_calls} served "
                  f"call(s) for these {len(PROBES)} requests.", ""]
        if judged >= len(PROBES):
            lines += ["One judge call per request, so the middle column above IS the",
                      "judge's decision.", ""]
        elif judged:
            lines += [f"Fewer judge calls than requests: {len(PROBES) - judged} request(s)",
                      "reused an earlier decision. Session affinity does that on purpose —",
                      "turn it off in Routing behaviour to make every turn classify.", ""]
        if judged == 0 and served_calls:
            lines += ["ZERO judge calls. The router did not consult the judge at all,",
                      "so nothing here is a verdict — it is whatever the route does",
                      "without one.", "",
                      "  · if 'auto' is a passthrough, that is expected: every mode",
                      "    resolves to one model and there is nothing to classify",
                      "  · otherwise the judge call is failing before it is logged —",
                      "    failed judge calls are never written to the routing log",
                      ""]
    if errors:
        lines += ["Some probes failed outright. Check the API key and that the",
                  "chosen models exist upstream.", ""]
    if len(served_models) >= 2:
        lines += ["JUDGE IS WORKING.",
                  f"{len(served_models)} distinct targets were selected, so classification",
                  "is discriminating between prompts.",
                  "",
                  "A '(judge chose differently)' line is not a failure on its own —",
                  "it means the judge disagreed with the probe's label."]
    elif len(served_models) == 1:
        only = served_models.pop()
        fallback = profs.model_of(prof, gateway_settings().get("fallback_mode", FALLBACK_DEFAULT))
        distinct = len({profs.model_of(prof, m) for m in profs.auto_modes(prof)})
        lines += [f"EVERY PROBE WENT TO ONE MODEL: {only}.", ""]
        if distinct == 1:
            # Not a judge failure at all: there is nothing to choose between.
            lines += ["That is not a judge failure — this profile points every mode",
                      "at that one model, so \"" + model_name + "\" is a passthrough and no",
                      "judge call is made at all.", "",
                      "Give at least two modes different models."]
        elif only == fallback:
            # Do not guess at the cause: ask the judge the same question
            # Switchyard asks it and report the provider's own answer.
            lines += ["", "That is the fail-open target, so the judge is suspect.",
                      "Asking it directly, exactly as Switchyard does:", "",
                      "-" * 60, ""]
            healthy, detail = judge_check(prof, probe_key)
            lines += detail
            if healthy:
                # The contract works. Now the only question left is whether the
                # judge can TELL THESE PROMPTS APART — asked directly, with no
                # router in between, so the answer cannot be an artefact of it.
                def step(i, mode):
                    stdscr.erase()
                    stdscr.addstr(0, 0, "Asking the judge directly"[: w - 1], curses.A_REVERSE)
                    stdscr.addstr(2, 0, f"{i + 1}/{len(PROBES)}  {mode}"[: w - 1])
                    stdscr.refresh()
                rows, table = judge_discriminate(prof, probe_key, on_step=step)
                chosen = {c for _e, c, _m, _d in rows if c}
                lines += ["", "-" * 60, "",
                          "Asked directly, with no router in between:", ""] + table + [""]
                if len(chosen) >= 2:
                    lines += [f"The judge DOES discriminate — {len(chosen)} distinct verdicts.",
                              "",
                              "So the judge works and the routing is not using its answer.",
                              "Check that the running config is the one you edited (Validate),",
                              "and that session affinity is not latching one decision across",
                              "the whole test."]
                elif len(chosen) == 1:
                    only = chosen.pop()
                    lines += [f"The judge answers {only!r} to EVERY prompt.", "",
                              "Nothing is wrong with the routing: the judge genuinely cannot",
                              "tell these apart. It is a classification problem, not a",
                              "plumbing one.", "",
                              "  · try a stronger judge — option 2 filters to capable models",
                              "  · a 9-way choice is a lot for a small model",
                              "  · merged modes make the labels less distinct: modes sharing",
                              "    a model become one target, so give them different models",
                              "    or accept the merge"]
                else:
                    lines += ["Every direct call failed — see the reasons above."]
        else:
            lines += ["", "Not the fail-open target, so the judge is answering — it is just",
                      "classifying everything the same way. Check the mode descriptions",
                      "in the prompt, or that the modes point at different models."]
    else:
        lines += ["No probe succeeded. Nothing can be concluded."]

    pager(stdscr, "Routing self-test", "\n".join(lines))


def _money(v: float) -> str:
    return f"{v:>12.6f}" if 0 < abs(v) < 0.01 else f"{v:>12.2f}"


def action_accounting(stdscr) -> None:
    """Spend by period, split judge vs served."""
    con = db()
    prices = accounting.load_prices(con)
    stats = accounting.ingest(con, ROUTING_LOG, prices)   # pick up whatever the sidecar has not

    t = accounting.totals(con)
    lines = [f"{'period':<12}{'requests':>10}{'judge $':>13}{'served $':>13}{'total $':>13}", ""]
    for period in accounting.PERIODS:
        row = t[period]
        lines.append(f"{period:<12}{row['requests']:>10,}"
                     f"{_money(row['judge'])}{_money(row['served'])}{_money(row['total'])}")

    profs = profiles()
    # A model may serve several modes, and several profiles. Label it with every
    # mode that points at it so the breakdown stays honest.
    mode_of: dict[str, list[str]] = {}
    for n in profs.names():
        for mode in MODES:
            model = profs.model_of(n, mode)
            if model:
                mode_of.setdefault(model, []).append(mode if len(profs.names()) == 1
                                                     else f"{n}/{mode}")

    served = accounting.by_model(con)
    if served:
        grand = sum(c for _, _, c in served) or 1.0
        lines += ["", "served traffic by model", ""]
        for model, reqs, cost in served[:12]:
            modes = "/".join(mode_of.get(model, [])) or "-"
            lines.append(f"  {model:<40}{reqs:>9,}  {cost:>12.4f}  {cost/grand*100:>5.1f}%  {modes}")

    tagged = accounting.by_tag(con)
    if tagged:
        grand_all = sum(c for _, _, c in tagged) or 1.0
        lines += ["", "by tag  (who is spending)", ""]
        for tag, reqs, cost in tagged[:12]:
            who = tag if tag == "—" else f"{tag} · {profile_of(tag)}"
            lines.append(f"  {who:<34}{reqs:>9,}  {cost:>12.4f}  {cost / grand_all * 100:>5.1f}%")

    judge = accounting.by_model(con, judge_only=True)
    if judge:
        lines += ["", "judge", ""]
        for model, reqs, cost in judge:
            lines.append(f"  {model:<40}{reqs:>9,}  {cost:>12.4f}")

    total_all = t["total"]["total"]
    if total_all > 0:
        lines += ["", f"routing overhead   {t['total']['judge'] / total_all * 100:.2f}% of spend is classification"]
        d = profs.default_name
        priced = [(m, prices.get(profs.model_of(d, m))) for m in MODES]
        priced = [(m, p) for m, p in priced if p]
        if priced:
            dearest_mode, dearest = max(priced, key=lambda mp: mp[1]["completion"])
            delta = accounting.counterfactual(con, dearest) - t["total"]["served"]
            verb = "saved" if delta >= 0 else "COST EXTRA"
            lines.append(f"vs all-{dearest_mode:<11} {verb} {abs(delta):.4f}   "
                         f"(served traffic re-priced on {profs.model_of(d, dearest_mode)})")

    nrej = con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0]
    last = con.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    size = f"{ROUTING_LOG.stat().st_size / 1e6:.1f} MB" if ROUTING_LOG.exists() else "absent"
    lines += ["", "-" * 74, "",
              f"last ingest {last[0] if last else 'never'}    routing log {size}"]
    if stats["unpriced"]:
        lines.append(f"! no catalogue price for: {', '.join(stats['unpriced'][:5])} — reload the catalogue")
    if nrej:
        lines.append(f"! {nrej} routing-log lines could not be parsed")
    lines += ["",
              "Derived from token counts, not billed amounts, and they UNDERCOUNT.",
              "",
              "A request is recorded only when its stream FINISHES. These are",
              "billed upstream and appear nowhere here:",
              "",
              "  · anything you interrupt — the record is written at the end,",
              "    so an abandoned stream leaves none",
              "  · streams that break mid-flight (Validate counts those)",
              "  · judge calls that fail — never logged upstream at all",
              "",
              "Spend is attributable to a model and to a tag. NOT to a profile:",
              "the routing log records the model that served, not the route that",
              "was asked for, so there is nothing to group by.",
              "",
              "Catalogue prices also drift.",
              "",
              "image-out spend is token-only. Those models also charge per image,",
              "and the routing log records no image count, so generation cost",
              "cannot be reconstructed here at all.",
              "",
              "Each user's own provider dashboard is the authoritative total."]
    last_clear = con.execute("SELECT value FROM meta WHERE key='last_clear'").fetchone()
    if last_clear:
        lines.insert(1, f"(last cleared {last_clear[0]})")

    key = pager(stdscr, "Accounting", "\n".join(lines), keys=("c", "t"))
    if key == "c":
        _clear_accounting(stdscr, con)
    elif key == "t":
        available = accounting.tags(con)
        if available:
            j = choose(stdscr, "Show which tag?", [f"  {t} · {profile_of(t)}" for t in available])
            if j is not None:
                _tag_detail(stdscr, con, available[j])
    # No close: this is the process-wide connection, and every other screen
    # still needs it. It used to be a private one opened per visit.


def _tag_detail(stdscr, con, tag: str) -> None:
    t = accounting.totals(con, tag=tag)
    lines = [f"{tag}   profile {profile_of(tag)}", "",
             f"{'period':<12}{'requests':>10}{'judge $':>13}{'served $':>13}{'total $':>13}", ""]
    for period in accounting.PERIODS:
        row = t[period]
        lines.append(f"{period:<12}{row['requests']:>10,}"
                     f"{_money(row['judge'])}{_money(row['served'])}{_money(row['total'])}")
    lines += ["", "models used", ""]
    for model, reqs, cost in accounting.by_model(con, tag=tag):
        lines.append(f"  {model:<44}{reqs:>9,}  {cost:>12.4f}")
    lines += ["", "This is what they spent on their own account — derived from token",
              "counts here, and authoritative only on their provider dashboard."]
    pager(stdscr, f"Accounting · {tag}", "\n".join(lines))


CLEAR_PERIODS = list(accounting.CLEAR_WINDOWS)


def _clear_accounting(stdscr, con) -> None:
    """Zero the counters — mostly for separating test runs from real usage."""
    now = datetime.now(timezone.utc)
    rows = []
    for period in CLEAR_PERIODS:
        q, c = accounting.clear_preview(con, period, now)
        rows.append(f"  clear {period:<11} {q:>8,} requests   ${c:>10.4f}")
    rows += ["", "  also re-read the routing log from the start"]

    idx = choose(stdscr, "Clear accounting", rows,
                 hint="enter to choose   esc cancel   (deleted history does not come back)")
    if idx is None:
        return

    if idx >= len(CLEAR_PERIODS):
        h, w = stdscr.getmaxyx()
        stdscr.addstr(h - 1, 0, "wipe ALL history and re-ingest the whole log? y/N"[: w - 1], curses.A_BOLD)
        stdscr.refresh()
        if stdscr.getch() in (ord("y"), ord("Y")):
            accounting.clear(con, "all", now)
            accounting.reset_ingest(con)
            stats = accounting.ingest(
                con, ROUTING_LOG,
                accounting.load_prices(con))
            pager(stdscr, "Rebuilt", f"Re-read {stats['records']} records from the routing log "
                                     f"at current catalogue prices.\n\nAnything already rotated "
                                     f"away is not in the file and cannot be recovered.")
        return

    period = CLEAR_PERIODS[idx]
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 1, 0, f"delete accounting for '{period}'? y/N"[: w - 1], curses.A_BOLD)
    stdscr.refresh()
    if stdscr.getch() not in (ord("y"), ord("Y")):
        return
    removed = accounting.clear(con, period, now)
    pager(stdscr, "Cleared",
          f"Removed {removed['requests']:,} requests and ${removed['cost']:.4f} "
          f"from '{period}'.\n\n"
          "The routing log itself is untouched; only the aggregates were deleted.\n"
          "The ingest checkpoint did not move, so this data will not reappear.")


def action_upstream(stdscr) -> None:
    """Verify the vendored Switchyard still honours everything we depend on."""
    ok, results = upstream.check(ROUTES)
    lines = [f"Pinned against upstream v{upstream.PINNED_VERSION}", ""]
    for passed, name, detail in results:
        lines.append(f"  {'PASS' if passed else 'FAIL'}  {name:<44} {detail}")
    lines += ["", "-" * 74, ""]
    lines += (["Everything this project depends on is intact."] if ok else
              ["Something changed upstream. Nothing here is written inside the",
               "vendored directory, so your config and accounting are safe — but a",
               "FAIL above means a feature is broken until tui/upstream.py is",
               "updated to match. See UPSTREAM.md."])
    pager(stdscr, "Upstream compatibility", "\n".join(lines))


# --------------------------------------------------------------------------
# menu
# --------------------------------------------------------------------------

def _status_line() -> str:
    profs = profiles()
    n_keys = len(active_tags())
    broken = {n: routes_gen.unservable(profs, n) for n in profs.names()}
    broken = {n: why for n, why in broken.items() if why}
    bits = [f"{provider().DISPLAY} · caller-pays",
            f"{n_keys} key{'s' if n_keys != 1 else ''}",
            f"{len(profs.names())} profile{'s' if len(profs.names()) != 1 else ''}"]
    if not n_keys:
        bits.append("! nobody can call this service")
    cov = judge_coverage()
    if cov["served"] and cov["judged"] < cov["served"]:
        bits.insert(0, f"! judge decided {cov['judged']}/{cov['served']} requests")
    elif cov["fail_open"]:
        bits.insert(0, f"! {cov['fail_open']} verdict(s) discarded")
    broken_streams, _ = stream_errors()
    if broken_streams:
        bits.insert(0, f"! {broken_streams} broken stream(s) — clients saw a reset")
    stale = stale_config()
    if stale:
        bits.insert(0, "! NOT APPLIED — restart the router")
    idle = [n for n in profs.names() if routes_gen.not_routing(profs, n)]
    if idle:
        bits.insert(0, "! NOT ROUTING — " + ", ".join(f"'{n}' is one model" for n in idle))
    if broken:
        detail = ", ".join(
            f"'{n}' " + (f"{len(SLOTS) - len(profs.missing(n))}/{len(SLOTS)}"
                         if profs.missing(n) else why.split(";")[0])
            for n, why in broken.items())
        bits.insert(0, f"! NOT SERVING — {detail}")
    return "Switchyard — " + "  ·  ".join(bits)


def _failure_summary() -> str:
    try:
        rows = health.recent(db(), hours=24)
    except Exception:
        return "unavailable"
    if not rows:
        return "none in 24h"
    total = sum(r["total"] for r in rows)
    return f"! {total} in 24h · {rows[0]['kind']} ({rows[0]['reason']})"


def build_menu() -> list[tuple[str, str, object]]:
    """(group, label-with-state, handler). State is on the row so the menu is
    also the status screen."""
    profs = profiles()
    keys = active_tags()
    incomplete = [n for n in profs.names() if not profs.complete(n)]
    ok_caddy, _ = validate_caddy()
    stale = stale_config()

    items = []
    if stale:
        # First, and only when it applies: everything else on this menu
        # describes a configuration the router is not running.
        items.append(("Setup", "Apply configuration      ! router is serving an older config",
                      action_apply))
    items += [
        ("Setup", f"Authorised keys          {len(keys) or 'none'}"
                  + (f" · {', '.join(keys[:3])}" if keys else " — nobody can call this"),
         action_roster),
        ("Setup", f"Profiles                 {len(profs.names())} · {', '.join(profs.names()[:3])}"
                  + (f"   ! {len(incomplete)} incomplete" if incomplete else ""),
         action_profiles),
        ("Setup", f"Defaults for new users   newcomers get '{newcomer_profile()[0]}'",
         action_newcomer),
        ("Setup", f"Routing behaviour        fail open to "
                  f"'{routing_settings()['fallback_mode']}' · judge budget "
                  f"{routing_settings()['max_output_tokens']}",
         action_routing),
        ("Setup", f"Gateway and logs         {'config valid' if ok_caddy else '! config INVALID'}",
         action_gateway),
        ("Use", "Connect a client         curl · OpenCode · Claude Code · SDKs", action_connect),
        ("Use", "Test routing             probes: is the judge discriminating?", action_probe),
        ("Use", "Accounting               spend by period, model and tag", action_accounting),
        ("Maintain", f"Recent failures          {_failure_summary()}", action_failures),
        ("Maintain", "Show configuration", action_summary),
        ("Maintain", "Validate configuration", action_validate),
        ("Maintain", f"Reload model catalogue   {catalogue_age()}", action_reload),
        ("Maintain", f"Check upstream           pinned v{upstream.PINNED_VERSION}", action_upstream),
    ]
    return items


def level_warnings(profs: Profiles) -> list[str]:
    """Slots whose stored reasoning level is not one the model lists.

    Only reachable from a hand-edited file or a catalogue that has moved since
    the choice was made. Not fatal — the provider maps an unknown effort to its
    nearest supported one — so it is reported, not blocked.
    """
    out = []
    for n in profs.names():
        for slot in SLOTS:
            model = profs.model_of(n, slot)
            level = profs.effort_of(n, slot)
            if not model or not level:
                continue
            adapter = profs.adapter_of(n, slot)
            record = model_record(adapter, model)
            if not record:
                continue            # unknown slug: cannot judge, do not guess
            offered = providers.get(adapter).levels(record)
            if offered and level not in offered:
                out.append(f"{n}/{slot}: {model} at {level}; it lists "
                           f"{', '.join(offered)}")
            elif not offered:
                out.append(f"{n}/{slot}: {model} accepts no reasoning level, "
                           f"{level} is stored")
    return out


def key_adapter_warnings(profs: Profiles) -> list[str]:
    """Roster keys issued for one system but assigned to a profile on another.

    Caddy admits them on the hash alone, so the failure surfaces upstream as a
    rejected credential and reads as ours.
    """
    out = []
    for entry in load_roster():
        name = entry.get("profile", "")
        if name not in profs.names():
            continue
        primary = profs.primary_adapter(name)
        held = entry.get("provider", "")
        if held and held != primary:
            out.append(f"{entry.get('tag', '?')}: {providers.get(held).DISPLAY} key on "
                       f"profile '{name}' ({providers.get(primary).DISPLAY})")
    return out


def action_apply(stdscr) -> None:
    """Restart the router so it picks up the config we generated.

    There is no reload: switchyard-server parses --config once and never looks
    again — no signal, no file watch, no admin endpoint. A restart is the only
    mechanism, and it is a complete one, because the entrypoint regenerates
    routes.toml from the database before exec-ing the server.

    This works only when the TUI shares the router's container, i.e. it was
    started with `docker compose exec`. Under `docker compose run --rm` the TUI
    is a separate container with its own PID namespace and PID 1 is this
    process; signalling it would kill the TUI and leave the router untouched.
    """
    stale = stale_config()
    if not stale:
        pager(stdscr, "Already applied",
              "The running router is serving the current configuration.\n\n"
              + (f"Nothing to do." if running_routes() is not None else
                 "(The router could not be reached from here, so this could not\n"
                 "be confirmed — restart anyway if you have just changed routing.)"))
        return
    if _router_started() is None:
        pager(stdscr, "Cannot restart from here", "\n".join([
            f"! {stale}.", "",
            "This TUI is running in its own container, so it cannot signal the",
            "router. That is deliberate: it holds no Docker socket.", "",
            "From the host:", "", "    docker compose restart switchingyard", "",
            "Or start the TUI alongside the router next time, where this option",
            "can do it for you:", "", "    docker compose exec switchingyard manage",
        ]))
        return
    pager(stdscr, "Restart the router", "\n".join([
        f"! {stale}.", "",
        "This stops the router; Docker restarts it and it reloads the config.",
        "", "  · this TUI session ends with it — reopen after",
        "  · in-flight requests drain for up to 30s, then are cut",
        "  · the gateway stays up and returns 502 for a second or two",
        "  · keys and accounting are unaffected",
    ]))
    if not confirm(stdscr, "restart the router now?"):
        return
    os.kill(1, signal.SIGTERM)
    pager(stdscr, "Restarting",
          "Sent SIGTERM to the router. Docker will bring it back with the\n"
          "current configuration.\n\nThis session is ending; reopen with:\n"
          "    docker compose exec switchingyard manage")


def action_failures(stdscr) -> None:
    """What has gone wrong recently, and what the provider actually said.

    The router counts faults but keeps no history, and the ones that matter most
    — a judge refused by the provider — never reach the request log at all,
    because a rejected call is never billed and never logged. The sidecar samples
    the counters; this shows them with first- and last-seen, and can ask the
    judge directly for the message behind the count.
    """
    selected = 0
    while True:
        rows_data = health.recent(db(), hours=24)
        rows = []
        for r in rows_data:
            when = f"{r['first_seen'][11:16]}→{r['last_seen'][11:16]}"
            rows.append(f"  {r['total']:>5}x  {r['kind']:<16} {r['subject']:<32} "
                        f"{r['reason']:<20} {when}")
        if not rows:
            rows = ["  (nothing recorded in the last 24 hours)"]
        rows += ["", "  > ask the judge now, and show what the provider replies"]
        idx = choose(stdscr, "Recent failures — last 24 hours", rows, start=selected,
                     hint="enter for detail   esc back")
        if idx is None:
            return
        selected = idx
        label = rows[idx].strip()
        if label.startswith(">"):
            profs = profiles()
            names = [n for n in profs.names() if profs.model_of(n, "judge")]
            if not names:
                pager(stdscr, "No judge set", "No profile has a judge model yet.")
                continue
            j = 0 if len(names) == 1 else (choose(
                stdscr, "Test which profile's judge?",
                [f"  {n:<16} {profs.model_of(n, 'judge')}" for n in names]) or 0)
            key = prompt(stdscr, "a key authorised for this service (hidden): ", hidden=True)
            if not key:
                continue
            healthy, detail = judge_check(names[j], key)
            pager(stdscr, "Judge — live" + ("" if healthy else "  ! FAILING"),
                  "\n".join(detail))
            continue
        if idx < len(rows_data):
            r = rows_data[idx]
            body = [f"{r['kind']}", "",
                    f"  what      {r['subject']}",
                    f"  reason    {r['reason']}",
                    f"  times     {r['total']}",
                    f"  first     {r['first_seen'].replace('T', ' ')}",
                    f"  last      {r['last_seen'].replace('T', ' ')}", ""]
            help_text = health.REASON_HELP.get(r["reason"])
            if help_text:
                body += [help_text, ""]
            if r["kind"] == "judge fail-open":
                body += ["Every one of these was a request the judge did not decide.",
                         "It fell open to the fallback mode instead, and was billed",
                         "for the judge call unless the provider refused it outright.",
                         "",
                         "A refused call is never billed and never logged upstream,",
                         "which is why these counts can have no matching rows in your",
                         "provider's activity log.", "",
                         "Use '> ask the judge now' below for the exact message."]
            elif r["kind"] == "broken stream":
                body += ["The response had already started, so the client saw a",
                         "truncated stream with no terminating [DONE] — usually",
                         "reported as a connection reset. The request log shows",
                         "these as status 200."]
            pager(stdscr, "Failure detail", "\n".join(body))


def action_validate(stdscr) -> None:
    """Both generated files, checked by the tools that actually consume them."""
    skipped, (caddy_ok, caddy_detail) = write_config()
    routes_ok, routes_detail = validate_routes()
    profs = profiles()
    lines = [
        f"routes.toml   {'VALID' if routes_ok else 'INVALID'}   {routes_detail}",
        f"Caddyfile     {'VALID' if caddy_ok else 'INVALID'}   {caddy_detail}",
        "",
    ]
    if skipped:
        lines += ["! not serving:"] + [f"    {reason}" for reason in skipped] + \
                 ["  they emit no routes; their users cannot connect", ""]
    if skipped and len(skipped) == len(profs.names()):
        # Upstream refuses a config with no routes at all, so the router exits
        # rather than idling. The TUI is a separate command and still works.
        lines += ["! NO profile is servable, so routes.toml has no routes and the",
                  "  router will not start at all — 'at least one algorithm route",
                  "  is required'. Reach this screen with:",
                  "      docker compose run --rm switchingyard manage", ""]
    for n in profs.names():
        if not routes_gen.unservable(profs, n):
            groups = routes_gen.groups(profs, n, profs.auto_modes(n))
            merged = [g for g in groups if len(g[2]) > 1]
            note = ("   merged: " + "; ".join("+".join(g[2]) for g in merged)) if merged else ""
            away = profs.outside_auto(n)
            if away:
                note += f"   outside auto: {', '.join(away)}"
            lines.append(f"  {n:<14} \"{profs.route_id(n)}\"   "
                         f"{len(groups)} distinct model(s){note}")
            idle = routes_gen.not_routing(profs, n)
            if idle:
                lines += [f"      ! {idle}",
                          "        give at least two modes different models, or this",
                          "        service is a plain proxy with extra steps"]
    stale = level_warnings(profs)
    if stale:
        lines += ["", "! reasoning levels the model does not list:"] + \
                 [f"    {w}" for w in stale] + \
                 ["  the provider maps these to its nearest supported level,",
                  "  so the effect is an approximation rather than an error."]
    mismatched = key_adapter_warnings(profs)
    if mismatched:
        lines += ["", "! keys whose system is not their profile's:"] + \
                 [f"    {w}" for w in mismatched] + \
                 ["  the gateway admits them and the provider then rejects them."]
    for n in profs.names():
        jn = _judge_note(profs, n)
        if jn:
            lines += [f"! {n}: {jn}", ""]
    cov = judge_coverage()
    if cov["served"]:
        gap = cov["served"] - cov["judged"]
        lines += ["", f"judge decided {cov['judged']} of {cov['served']} request(s)"]
        if cov["fail_open"]:
            lines.append(f"  ! {cov['fail_open']} verdict(s) were produced and then DISCARDED "
                         f"— those requests fell open to the fallback")
        if gap > 0:
            lines.append(f"  ! {gap} request(s) never reached the judge:")
            lines += [f"      {r}" for r in cov["reasons"]] or \
                     ["      no configured reason — check what model clients send"]
        elif not cov["fail_open"]:
            lines.append("  every request was classified")
        lines.append("")
    total_broken, by_model = stream_errors()
    if total_broken:
        lines += ["", f"! {total_broken} stream(s) broke after the response had started:"]
        lines += [f"    {m:<40} {n}" for m, n in sorted(by_model.items(),
                                                        key=lambda kv: -kv[1])]
        lines += ["  The client saw a truncated response — no terminating [DONE] —",
                  "  which most SDKs report as a connection reset. The request log",
                  "  shows these as status 200: it is written before the body streams.",
                  "  Counted only here, and reset when the router restarts.", ""]
    stale = stale_config()
    lines += ["", "-" * 66, ""]
    if stale:
        lines += [f"! NOT APPLIED — {stale}.", "",
                  "  The router reads its config once, at startup. Everything above",
                  "  describes the file; the process is serving something else.", "",
                  "    docker compose restart switchingyard", ""]
    elif running_routes() is None:
        lines += ["The router is not reachable from here, so whether it is serving",
                  "this config could not be checked.", "",
                  "  docker compose restart switchingyard   (after changing routing)", ""]
    else:
        lines += ["The running router is serving this configuration.", ""]
    lines += ["Adding or revoking a key needs no restart — authcheck reads the",
              "roster per request. Gateway limits need: docker compose restart gateway"]
    pager(stdscr, "Configuration", "\n".join(lines))


def main(stdscr) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass        # no real terminal (tests); the cursor is cosmetic
    write_config()          # keep generated output in step with the stores
    selected = 0
    while True:
        items = build_menu()
        rows, targets, group = [], [], None
        for g, label, handler in items:
            if g != group:
                # blank line between groups, but never above the first — the
                # opening row is where the cursor lands by default.
                if group is not None:
                    rows.append(""); targets.append(None)
                rows.append(f"  {g}"); targets.append(None); group = g
            rows.append(f"     {len(targets) + 1 - sum(1 for t in targets if t is None):>2}  {label}")
            targets.append(handler)
        rows += ["", "  q  Quit"]; targets += [None, None]
        quit_row = len(rows) - 1
        # land on the first thing that needs attention
        if selected == 0:
            selected = next((i for i, r in enumerate(rows) if "!" in r and targets[i]), 1)
        idx = choose(stdscr, _status_line(), rows, start=selected,
                     hint="↑↓ move   enter select   q quit    (restart after changes)")
        # Selecting the Quit row must quit, whether reached by enter or by
        # pressing q — it says Quit, and before this it did neither.
        if idx is None or idx == quit_row:
            return
        if idx >= len(targets) or targets[idx] is None:
            selected = idx
            continue
        selected = idx
        try:
            targets[idx](stdscr)
        except Exception as exc:
            pager(stdscr, "Error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    curses.wrapper(main)
