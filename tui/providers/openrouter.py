"""OpenRouter adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

NAME = "openrouter"
DISPLAY = "OpenRouter"
# Deliberately loose: matches sk-or-v1-… and any future version suffix, so a
# version bump upstream does not lock people out at the add-key step.
KEY_PREFIX = "sk-or-"
BASE_URL = "https://openrouter.ai/api/v1"
WIRE_FORMAT = "openai_chat"
CONSOLE_URL = "https://openrouter.ai/keys"

MODELS_URL = f"{BASE_URL}/models"
KEY_URL = f"{BASE_URL}/key"

# Reasoning levels, strongest first. These are OpenRouter's own `effort` values
# except `off`, which stands for its `enabled = false` — one name for one idea,
# rather than showing "none" in one place and "off" in another.
#
# The share of the output budget each spends on thinking, per OpenRouter's docs:
# max/xhigh ~95%, high ~80%, medium ~50%, low ~20%, minimal ~10%.
OFF = "off"
LEVELS = ("max", "xhigh", "high", "medium", "low", "minimal", OFF)
LEVEL_NOTE = {"max": "~95% of the output budget on reasoning",
              "xhigh": "~95%, same allocation as max",
              "high": "~80%", "medium": "~50%", "low": "~20%",
              "minimal": "~10%", OFF: "reasoning disabled"}
# Offered when the catalogue does not enumerate a model's own list.
FALLBACK_LEVELS = ("max", "xhigh", "high", "medium", "low")
DEFAULT_LEVEL = "high"


def levels(record: dict | None) -> list[str]:
    """Levels offerable for one catalogue record, strongest first.

    Empty means the model does not reason at all and no `extra_body` should be
    emitted for it. A model that reasons but exposes no effort selection gets
    the fallback ladder — OpenRouter maps an unsupported value to the nearest
    supported one rather than rejecting it, so offering the ladder is safe.
    """
    if not record or not record.get("reasoning"):
        return []
    offered = list(record.get("efforts") or FALLBACK_LEVELS)
    if record.get("reasoning_mandatory"):
        # These reject being disabled outright; hiding the rung is the only way
        # to keep a 400 out of a config that otherwise looks fine.
        offered = [level for level in offered if level != OFF]
    elif OFF not in offered:
        offered.append(OFF)
    return [level for level in LEVELS if level in offered]


def default_level(record: dict | None) -> str:
    """`high` where the model allows it, else its own default, else the best on offer."""
    offered = levels(record)
    if not offered:
        return ""
    if DEFAULT_LEVEL in offered:
        return DEFAULT_LEVEL
    own = (record or {}).get("default_effort") or ""
    if own in offered:
        return own
    return offered[0]


def extra_body(level: str) -> dict:
    """The request fields that impose `level`, merged into the outbound body.

    Nested `reasoning`, never the flat `reasoning_effort`: the flat parameter's
    documented enum omits `max`, and it has no way to say "off" at all.
    """
    if not level:
        return {}
    if level == OFF:
        return {"reasoning": {"enabled": False}}
    return {"reasoning": {"effort": level}}


def validate(key: str) -> tuple[bool | None, str, dict]:
    """Check a key against the provider.

    Returns (ok, detail, meta). `ok` is None for "could not tell" — a network
    failure, which must not be reported as a rejection.
    """
    request = urllib.request.Request(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "rejected by OpenRouter — wrong, truncated or revoked key", {}
        return None, f"OpenRouter returned HTTP {exc.code}", {}
    except Exception as exc:
        return None, f"could not reach OpenRouter ({type(exc).__name__})", {}

    # The payload is nested under "data" and field names have moved before;
    # read defensively and show whatever is present rather than assuming.
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    meta = {}
    for key_name, label in (("label", "label"), ("usage", "used"), ("limit", "limit"),
                            ("limit_remaining", "remaining"), ("is_free_tier", "free tier"),
                            ("rate_limit", "rate limit")):
        if data.get(key_name) is not None:
            meta[label] = data[key_name]
    return True, "verified", meta


def _rate(pricing: dict, field: str) -> float:
    try:
        return float(pricing.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def _efforts(reasoning: dict) -> list[str]:
    """`supported_efforts` in our vocabulary, or [] when the model exposes none.

    Absent and null mean different things upstream — absent is "no effort
    selection", null is "all values accepted" — but both land on the fallback
    ladder here, so they collapse to the same empty result.
    """
    supported = reasoning.get("supported_efforts")
    if not isinstance(supported, list):
        return []
    return [OFF if e == "none" else e for e in supported if isinstance(e, str)]


def catalogue() -> list[dict]:
    """Public model list. No credential needed — verified unauthenticated."""
    with urllib.request.urlopen(MODELS_URL, timeout=30) as response:
        data = json.load(response)["data"]
    rows = []
    for m in data:
        if m["id"].startswith("~"):
            continue          # floating alias; pin a dated slug instead
        pricing = m.get("pricing", {}) or {}
        # openrouter/auto* report -1 as a "varies" sentinel. Priced literally
        # that produces negative spend in accounting, so drop them: a meta-router
        # is not a routing target for us anyway.
        if any(_rate(pricing, f) < 0 for f in ("prompt", "completion")):
            continue
        params = m.get("supported_parameters") or []
        bench = (m.get("benchmarks") or {}).get("artificial_analysis") or {}
        arch = m.get("architecture") or {}
        modes_in = arch.get("input_modalities") or []
        modes_out = arch.get("output_modalities") or []
        # Present-but-null on the models that cannot reason at all, so the
        # truthiness of the object is the capability test.
        reasoning = m.get("reasoning") or {}
        rows.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "in": _rate(pricing, "prompt") * 1e6,
            "out": _rate(pricing, "completion") * 1e6,
            "price_prompt": _rate(pricing, "prompt"),
            "price_completion": _rate(pricing, "completion"),
            "price_cache_read": _rate(pricing, "input_cache_read"),
            "price_cache_write": _rate(pricing, "input_cache_write"),
            # Per-image, charged on top of tokens. The routing log has no image
            # count, so this cannot be applied to spend — it is shown, not billed.
            "price_image": _rate(pricing, "image"),
            "ctx": m.get("context_length") or 0,
            "max_out": (m.get("top_provider") or {}).get("max_completion_tokens") or 0,
            "structured": "structured_outputs" in params,
            "tools": "tools" in params,
            "reasoning": bool(reasoning),
            # Which levels this model actually accepts, and whether it may be
            # turned off at all — `mandatory` models reject being disabled.
            "efforts": _efforts(reasoning),
            "default_effort": reasoning.get("default_effort") or "",
            "reasoning_mandatory": bool(reasoning.get("mandatory")),
            "image_in": "image" in modes_in,
            "image_out": "image" in modes_out,
            "agentic": bench.get("agentic_index"),
            "coding": bench.get("coding_index"),
        })
    return sorted(rows, key=lambda r: r["id"])


def judge_level(record: dict | None) -> str:
    """The weakest setting the model allows, for the classifier slot.

    A judge's reasoning competes with its verdict JSON for one output budget, so
    less is safer. `mandatory` models cannot be turned off at all — the least
    they will do is their lowest rung.
    """
    offered = levels(record)
    if not offered:
        return ""
    return OFF if OFF in offered else offered[-1]
