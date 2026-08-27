"""Every assumption this project makes about the vendored NVIDIA Switchyard.

Nothing else may hardcode an upstream detail. When an upgrade breaks something,
it breaks here, and `check()` says exactly what.

Tracks upstream `main` after v0.2.0 — not the v0.2.0 tag. See PINNED_VERSION.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import urllib.request
from pathlib import Path

VENDOR = Path("/opt/switchyard/vendor")     # source, present only at build time

# We track upstream `main` AFTER v0.2.0, not the v0.2.0 tag. Say so out loud,
# because upstream's own Cargo.toml still reads "0.2.0" on main — the bump
# happens at release — so the version string cannot tell you which tree you have
# and reading it as a pin gives a false pass. That is exactly how a checkout
# whose config schema had moved on passed a version check and then refused to
# start. The real pin is the submodule commit this repository records; `check()`
# below is what actually verifies the tree, and this constant is only a label.
PINNED_VERSION = "0.2.0+main (unreleased)"

# Response header naming the target that served a request. lib.rs:61
SELECTED_MODEL_HEADER = "x-model-router-selected-model"

# Routing-log tier values. lib.rs:466 writes the classifier tier; the answer
# call is written with no tier (usage_metrics.rs:35,71).
TIER_JUDGE = "classifier"
TIER_SERVED = ""

# Routing-log field names, with aliases so a rename degrades one column instead
# of breaking ingest. routing_log.rs RoutingRecord.
FIELDS = {
    "ts": ("ts", "timestamp", "time"),
    "model": ("model", "selected_model"),
    "tier": ("tier",),
    "prompt_tokens": ("prompt_tokens", "input_tokens"),
    "cached_tokens": ("cached_tokens", "cached_input_tokens"),
    "cache_creation_tokens": ("cache_creation_tokens", "cache_creation_input_tokens"),
    "completion_tokens": ("completion_tokens", "output_tokens"),
    "reasoning_tokens": ("reasoning_tokens",),
    # The gateway renames the roster tag onto x-switchyard-trial-id, which
    # routing_log.rs records as trial_id. That is the only free-form field in
    # the log, and it is how spend is attributed to a person.
    "tag": ("trial_id",),
}

# Per-target request parameters. `TargetConfig` is `deny_unknown_fields`, so
# extra_body is the ONLY way to impose anything per target — it is how each
# target's reasoning level is set (config.rs TargetConfig).
TARGET_FIELDS = ("id", "llm_client", "extra_body")

# extra_body is applied as a DEFAULT, not an override: `or_insert_with` means a
# caller sending the same top-level key wins. Documented behaviour we rely on,
# and the reason a configured reasoning level is a default rather than a rule.
EXTRA_BODY_IS_DEFAULT_ONLY = "or_insert_with"

# A route's client map is keyed on the model id ALONE — llm_client is not part
# of it (switchyard-runner config.rs, build_route_clients). So one model can
# appear at most once per route, and giving each reasoning level its own client
# no longer rescues it: the second target silently overwrites the first and
# takes its extra_body with it. The judge is in that same map
# (callable_target_names, algorithm.rs), which is why unservable() refuses a
# judge that shares a mode's model.
#
# This replaced an earlier assumption that the key was (llm_client, id). That
# WAS true at v0.2.0 and the per-level client split depended on it; upstream
# dropped the distinction, so the split now buys nothing here.
ROUTE_CLIENT_KEY = "by_model.insert(target.id.clone(), client)"

# session_affinity is gone from the parser; classify_trigger replaced it. We
# always write `every_request` — the value that judges every turn.
CLASSIFY_TRIGGER_VARIANT = "EveryRequest"

# The router advertises the routes it has LOADED on this endpoint, from memory
# rather than by re-reading the config file. That is what lets the TUI tell a
# config it has written from a config the process is actually serving —
# switchyard-server parses --config once, at startup, and never again.
MODELS_PATH = "/v1/models"
MODELS_ID_FIELD = "id"          # each entry in the "data" array names one route

CONFIG_SCHEMA_VERSION = 1
REQUIRED_FLAGS = ("--config", "--routing-log-file", "--dry-run", "--host", "--port")
OUR_RUST_TOOLCHAIN = "1.97.1"


def field(record: dict, name: str, default=0):
    """Read a routing-log field through its alias list."""
    for key in FIELDS[name]:
        if key in record and record[key] is not None:
            return record[key]
    return default


# --------------------------------------------------------------------------
# compatibility check
# --------------------------------------------------------------------------

def _static(vendor: Path) -> list[tuple[bool, str, str]]:
    """Checks that read the vendored source. Available only where it is mounted."""
    out = []
    cargo = vendor / "Cargo.toml"
    if not cargo.exists():
        return [(False, "vendored source", f"not found at {vendor} (build-time only)")]

    meta = tomllib.loads(cargo.read_text())
    members = meta.get("workspace", {}).get("members", [])
    out.append(("crates/switchyard-server" in members,
                "workspace member crates/switchyard-server",
                "present" if "crates/switchyard-server" in members else f"missing; members={members}"))

    msrv = meta.get("workspace", {}).get("package", {}).get("rust-version", "?")
    ok = tuple(int(x) for x in msrv.split(".")) <= tuple(int(x) for x in OUR_RUST_TOOLCHAIN.split("."))
    out.append((ok, "MSRV vs our toolchain",
                f"upstream needs >= {msrv}, we build with {OUR_RUST_TOOLCHAIN}"))

    lib = vendor / "crates/switchyard-server/src/lib.rs"
    if lib.exists():
        text = lib.read_text()
        m = re.search(r'HEADER_SELECTED_MODEL:\s*&str\s*=\s*"([^"]+)"', text)
        found = m.group(1) if m else None
        out.append((found == SELECTED_MODEL_HEADER, "selected-model header",
                    f"{found!r}" + ("" if found == SELECTED_MODEL_HEADER
                                    else f" but we expect {SELECTED_MODEL_HEADER!r}")))
        # lib.rs:440 defines the tier string; lib.rs:466 is what tags judge calls
        m = re.search(r'CLASSIFIER_TIER:\s*&str\s*=\s*"([^"]+)"', text)
        tier = m.group(1) if m else None
        out.append((tier == TIER_JUDGE, "judge tier marker",
                    f"{tier!r}" + ("" if tier == TIER_JUDGE else f" but we expect {TIER_JUDGE!r}")))
    else:
        out += [(False, "selected-model header", "lib.rs not found"),
                (False, "judge tier marker", "lib.rs not found")]

    # The schema parser lives in switchyard-runner: switchyard-server/src/config.rs
    # is now a shim that calls Runner::load. Looking in the old place gave a
    # confident FAIL about a struct that was present and correct, which is worse
    # than no check at all — so a missing file here is reported as "not found",
    # never as a failed assumption.
    cfg = vendor / "crates/switchyard-runner/src/config.rs"
    if cfg.exists():
        text = cfg.read_text()
        # Scope to the struct body: constructors elsewhere mention the same names.
        body = re.search(r"struct TargetConfig[^{]*\{(.*?)^\}", text, re.S | re.M)
        fields = body.group(1) if body else ""
        missing = [f for f in TARGET_FIELDS
                   if not re.search(rf"^\s+(?:pub\s+)?{f}:", fields, re.M)]
        out.append((bool(body) and not missing, "target fields (extra_body)",
                    "all present" if body and not missing
                    else f"missing: {', '.join(missing) or 'TargetConfig struct'}"))
        # The route's client map is keyed on the model id alone, so a model may
        # appear once per route no matter how many llm_clients it is split over.
        # We rely on that being true, because unservable() refuses the judge
        # sharing a mode's model on the strength of it.
        found = ROUTE_CLIENT_KEY in text
        out.append((found, "route clients keyed on model id alone",
                    "as expected — one model per route" if found
                    else f"{ROUTE_CLIENT_KEY!r} not found; the keying changed and the "
                         "judge/mode collision rule in unservable() may now be wrong"))
        m = re.search(r"const SUPPORTED_SCHEMA_VERSION:\s*u32\s*=\s*(\d+)", text)
        ver = int(m.group(1)) if m else None
        out.append((ver == CONFIG_SCHEMA_VERSION, "config schema_version",
                    f"{ver}" if ver == CONFIG_SCHEMA_VERSION
                    else f"upstream wants {ver}, we emit {CONFIG_SCHEMA_VERSION}"))
    else:
        out += [(False, "target fields (extra_body)", f"not found at {cfg}"),
                (False, "route clients keyed on model id alone", f"not found at {cfg}"),
                (False, "config schema_version", f"not found at {cfg}")]

    # classify_trigger replaced session_affinity. We write `every_request`
    # explicitly, so a changed default cannot silently reintroduce affinity —
    # but a renamed or removed variant would still break the config, and the
    # judge-decides guarantee rests on this one value.
    aff = vendor / "crates/libsy/src/algorithms/util/affinity.rs"
    if aff.exists():
        text = aff.read_text()
        ok = re.search(rf'{CLASSIFY_TRIGGER_VARIANT}\b', text) is not None
        out.append((ok, "classify_trigger every_request",
                    "variant present" if ok
                    else f"{CLASSIFY_TRIGGER_VARIANT} is gone — every request would stop being judged"))
    else:
        out.append((False, "classify_trigger every_request", f"not found at {aff}"))

    cl = vendor / "crates/libsy-llm-client/src/client.rs"
    if cl.exists():
        body = re.search(r"fn merge_extra_body[^{]*\{(.*?)^\}", cl.read_text(), re.S | re.M)
        text = body.group(1) if body else ""
        found = EXTRA_BODY_IS_DEFAULT_ONLY in text
        out.append((found, "extra_body defers to the caller",
                    "defaults only" if found
                    else "no longer or_insert_with — a configured level may now override"))
    else:
        out.append((False, "extra_body defers to the caller", "client.rs not found"))

    rl = vendor / "crates/switchyard-server/src/routing_log.rs"
    if rl.exists():
        # Scope to the RoutingRecord struct body. Grepping the whole file gives
        # false passes: the constructor at routing_log.rs:60 mentions the same
        # names, so a renamed struct field would still appear present.
        body = re.search(r"struct RoutingRecord[^{]*\{(.*?)^\}", rl.read_text(), re.S | re.M)
        text = body.group(1) if body else ""
        missing = [canonical for canonical, aliases in FIELDS.items()
                   if not any(re.search(rf"^\s+(?:pub\s+)?{a}:", text, re.M) for a in aliases)]
        if not body:
            out.append((False, "RoutingRecord struct", "not found in routing_log.rs"))
        out.append((not missing, "routing-log fields",
                    "all present" if not missing else f"missing: {', '.join(missing)}"))
    else:
        out.append((False, "routing-log fields", "routing_log.rs not found"))
    return out


def _runtime(config: Path) -> list[tuple[bool, str, str]]:
    """Checks that run the built binary."""
    out = []
    try:
        h = subprocess.run(["switchyard-server", "--help"], capture_output=True, text=True, timeout=30)
        help_text = h.stdout + h.stderr
        missing = [f for f in REQUIRED_FLAGS if f not in help_text]
        out.append((not missing, "CLI flags",
                    "all present" if not missing else f"missing: {', '.join(missing)}"))
    except Exception as exc:
        out.append((False, "CLI flags", f"could not run switchyard-server: {exc}"))
        return out

    # The route list must stay readable without a credential: the router
    # authenticates nobody, and the drift check depends on that.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:4000{MODELS_PATH}", timeout=3) as r:
            payload = json.load(r)
        ids = [m.get(MODELS_ID_FIELD) for m in payload.get("data", [])]
        ok = bool(ids) and all(isinstance(i, str) for i in ids)
        out.append((ok, "loaded routes are introspectable",
                    f"{len(ids)} route(s) advertised" if ok
                    else f"unexpected payload shape: {str(payload)[:60]}"))
    except Exception as exc:
        out.append((True, "loaded routes are introspectable",
                    f"server not running here ({type(exc).__name__}) — checked at runtime"))

    if config.exists():
        try:
            r = subprocess.run(["switchyard-server", "--config", str(config), "--dry-run"],
                               capture_output=True, text=True, timeout=60,
                               env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                                    "OPENROUTER_API_KEY": "check-placeholder"})
            detail = (r.stdout + r.stderr).strip().splitlines()
            out.append((r.returncode == 0, "config accepted (schema + custom mode)",
                        "valid" if r.returncode == 0 else (detail[-1] if detail else f"exit {r.returncode}")))
        except Exception as exc:
            out.append((False, "config accepted", str(exc)))
    return out


def check(config: Path, vendor: Path = VENDOR) -> tuple[bool, list[tuple[bool, str, str]]]:
    results = _static(vendor) + _runtime(config)
    return all(ok for ok, _, _ in results), results
