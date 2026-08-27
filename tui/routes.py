"""Generates routes.toml from the profiles.

This file is output, not input. Per-user routing means one target set and one
route set per profile, which cannot be maintained by hand-editing — Switchyard
selects a route purely by the model name in the request, so a profile *is* a
route set.
"""

from __future__ import annotations

import json
import re

import providers
from profiles import MODES, Profiles

# Per-mode descriptions kept as data, because modes that share a model must be
# merged into one judge label — see `groups()`.
MODE_DESC = {
    "simple": "a short factual answer, chat, rewriting, translation or formatting; also any turn whose only work is relaying or acknowledging one small tool result already in the conversation — several results that must be digested together are more than this mode",
    "toolcall": "this turn's own work is invoking a tool or function — reading or writing files, running commands, querying an API, fetching or searching the web; recent tool calls awaiting a follow-up action are evidence for this mode, and the surrounding conversation being a plan or an investigation does not change it — the act decides, not the topic; a tool result already returned and only needing description is not this mode",
    "coder": "this turn will itself write, edit, review or debug code",
    "planner": "this turn produces the plan itself — breaking work into steps, sequencing it, comparing approaches, or writing a design or specification; a turn that merely carries out one step of a plan already in the conversation is whatever that step's work is, not this mode",
    "researcher": "synthesising across many sources, long documents, or a long transcript — pages, search results and files that tools returned earlier in the conversation count as sources; the work is digesting and combining that material into an answer",
    "reasoner": "mathematics, proofs, logic, or careful multi-step derivation where a wrong intermediate step invalidates the answer",
    "image-in": "the turn cannot be answered without looking at an image already in the conversation — a screenshot, diagram, photograph or scan",
    "image-out": "the turn must produce or edit an image; the deliverable is a picture, not a description of one",
    "narrative": "prose, fiction, dialogue, scripts or long-form creative writing, where voice and readability matter more than correctness",
    "user-interface": "the turn produces or changes an interface — layout, components, styling, interaction, accessibility; the deliverable is something a person looks at and uses, not logic behind it",
    "security": "the turn is about attack surface — reviewing code or a design for vulnerabilities, authentication, authorisation, secrets handling, injection, unsafe defaults; the question is what an attacker could do",
    "debug": "the turn diagnoses something already broken from evidence in front of it — a stack trace, a failing test, an error log, a reproduction; the work is finding the cause, not writing new code",
}

PREAMBLE = "Classify the request into exactly one task mode, and answer with that mode's exact label.\n"

POSTAMBLE = """
Classify the work THIS turn produces, not the subject matter of the text in context. A conversation about a codebase is not `coder` unless this turn actually writes or analyses code, and a conversation building a plan is not `planner` on the turns that carry the plan's steps out. In a long exchange the topic stays constant while the work changes turn by turn — judge the turn, not the conversation.

Prefer the cheapest mode that can do the work. Reach for a more capable mode only when the turn plainly requires it — but do not let cheapness reclassify the work: digesting several fetched sources is research however routine each fetch was.

Return JSON matching the response schema supplied with the request.
"""

def groups(profs: Profiles, name: str, modes: list[str] | None = None) -> list[tuple]:
    """Merge modes that share a target into one judge label.

    The engine rejects a classifier route whose targets resolve to the same model
    twice (`llm_class.rs:750-755`), so `coder` and `planner` on one model cannot
    be two labels. Merging keeps that legal without constraining what the operator
    may choose: the label becomes `coder-planner` and the prompt describes the
    combined category. Order follows MODE_DESC so labels are stable.

    The merge key is (adapter, model) — not the reasoning level. `extra_body` is
    not part of the identity the engine deduplicates on, so one model at two
    levels inside one route is a startup failure rather than two targets; the
    store propagates a level across the modes that share a target so that state
    cannot be reached.

    Returns (label, model, modes, level, adapter).
    """
    by_target: dict[tuple[str, str], list[str]] = {}
    for mode in (modes if modes is not None else MODES):
        key = (profs.adapter_of(name, mode), profs.model_of(name, mode))
        by_target.setdefault(key, []).append(mode)
    out = []
    for (adapter, model), grouped in by_target.items():
        out.append(("-".join(grouped), model, grouped,
                    profs.effort_of(name, grouped[0]), adapter))
    return sorted(out, key=lambda g: MODES.index(g[2][0]))


def build_prompt(grouped: list[tuple[str, str, list[str]]], profile: str) -> str:
    """The bullet label MUST be the target's table name.

    libsy matches the judge's string against the target label, and the TOML gives
    no way to label a target differently from its table name (`llm_class.rs:741`,
    fed by `resolve_targets`). Since table names are global they carry the profile
    prefix, so that prefix has to appear in the prompt and the schema too — a
    mode name alone never matches and every request would fall through to
    default_target.
    """
    lines = [PREAMBLE]
    for label, _model, modes, *_ in grouped:
        desc = "; also, ".join(MODE_DESC[m] for m in modes)
        human = "/".join(modes)
        lines.append(f'- "{profile}_{label}" ({human}): {desc}.')
    return "\n".join(lines) + "\n" + POSTAMBLE


def response_schema(grouped, profile: str) -> str:
    enum = ", ".join(f'"{profile}_{label}"' for label, *_ in grouped)
    return """{
  "type": "object",
  "properties": {
    "decision": {
      "type": "object",
      "properties": {
        "mode": {
          "type": "string",
          "enum": [%s]
        }
      },
      "required": ["mode"],
      "additionalProperties": false
    }
  },
  "required": ["decision"],
  "additionalProperties": false
}""" % enum

HEADER = """# GENERATED FILE — do not edit.
#
# Rewritten from /data/switchyard.db whenever profiles or models change, so any
# hand edit is lost on the next change. Use the management TUI:
#     docker compose exec switchingyard manage
#
# ONE route per profile: "auto" for the default profile, "auto-<name>" for the
# rest. Per-mode route ids are deliberately NOT emitted — each was a passthrough,
# so naming one skipped the judge, which is the one thing this service exists to
# do. Profiles are a mapping choice, not an isolation boundary — any caller may
# name any profile, and pays with their own key.
#
# There is one llm_client per (adapter, reasoning level). That is not
# redundancy: a client keys its models by id, so two targets sharing an id on
# one client collapse into one and the loser's extra_body is dropped silently.
#
# The router reads this file ONCE, at startup. Editing a profile rewrites it
# immediately, but the running process keeps serving what it loaded. Apply with:
#     docker compose restart switchingyard
#
# No API key anywhere: forward_auth relays each caller's own credential.
"""


def _toml(value) -> str:
    """Inline TOML for an extra_body map.

    Deliberately small: the only shapes emitted are nested tables of strings and
    booleans, which is all the reasoning controls need.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + " }"
    return json.dumps(value)


def client_name(adapter: str, level: str) -> str:
    """One llm_client per (adapter, reasoning level).

    An llm client keys its models by id, so two targets that share an id on one
    client end up as one entry and the other is dropped with nothing but a log
    warning — including its extra_body (`config.rs:73-89`; last target name
    wins). Splitting by level keeps every (client, id) pair unique, which is the
    escape that same comment names: "The same id on two different clients never
    collides: each client keeps its own model."
    """
    return f"{adapter}_{level or 'plain'}"


def _target(name: str, model: str, adapter: str, level: str) -> str:
    body = providers.get(adapter).extra_body(level)
    extra = f"extra_body = {_toml(body)}\n" if body else ""
    return (f'[targets.{name}]\nid = "{model}"\n'
            f'llm_client = "{client_name(adapter, level)}"\n{extra}\n')


def _client_block(client: str, adapter: str) -> str:
    mod = providers.get(adapter)
    return (f"\n[llm_clients.{client}]\n"
            f'format = "{mod.WIRE_FORMAT}"\n'
            f'base_url = "{mod.BASE_URL}"\n'
            "forward_auth = true\n"
            "max_retries = 2\n")


def unservable(profiles: Profiles, name: str) -> str:
    """Why this profile cannot be emitted, or "" if it can.

    Kept here rather than in the UI so the generator and the validator cannot
    disagree about what is servable.
    """
    missing = profiles.missing(name)
    if missing:
        return f"{len(missing)} slot(s) unset: {', '.join(missing)}"
    primary = profiles.primary_adapter(name)
    judge_adapter = profiles.adapter_of(name, "judge")
    if judge_adapter != primary:
        return (f"judge is on {judge_adapter} but auto classifies on {primary}; "
                "one route carries one caller credential")
    # Every one of these would put a request in front of a model without the
    # judge choosing it. None of them is emitted: a profile that cannot be
    # classified does not serve.
    away = profiles.outside_auto(name)
    if away:
        return (f"{', '.join(away)} sit on another adapter, so auto cannot classify "
                f"them — one profile, one adapter")
    grouped = groups(profiles, name, profiles.auto_modes(name))
    if len(grouped) < 2:
        return (f"every mode resolves to {grouped[0][1]}, so there is nothing to "
                f"classify and no judge call would be made — give at least two "
                f"modes different models")
    disagreeing = profiles.disagreeing(name)
    if disagreeing:
        model, modes = disagreeing[0]
        return (f"{model} is asked for two reasoning levels across "
                f"{', '.join(modes)}; one target cannot hold both")
    # The judge shares the route's client map with the mode targets
    # (callable_target_names, algorithm.rs:481-484) and that map is keyed on the
    # model id ALONE (config.rs:289) — llm_client is not part of the key. So a
    # judge on the same model as any mode collides with it and one client
    # silently overwrites the other, taking its extra_body and therefore its
    # reasoning level with it. Giving each level its own client used to prevent
    # exactly this; upstream removed the distinction that made it work.
    #
    # Refused rather than warned: the damage is a judge running at the mode's
    # level instead of `off`, whose reasoning then eats the verdict budget, so
    # every request fails open while still billing the judge. Nothing in the
    # response says so, and no warning is logged — the config-level duplicate
    # warning only fires for a shared (llm_client, id), which our split avoids.
    judge_model = profiles.model_of(name, "judge")
    clash = [m for m in profiles.auto_modes(name)
             if profiles.model_of(name, m) == judge_model]
    if clash:
        return (f"the judge and {', '.join(clash)} both use {judge_model}; one "
                f"route cannot hold that model twice, and the judge would "
                f"silently lose its reasoning level — give the judge its own model")
    return ""


def route_ids(toml_text: str) -> set[str]:
    """The model names this config serves, read back from what we generated.

    Parsed from the output rather than recomputed from the profiles, so it
    cannot drift from what was actually written — it is compared against the
    ids a running router reports, and a comparison between two derivations of
    the same thing would prove nothing.
    """
    return set(re.findall(r'^\[routes\.[^\]]+\]\nid = "([^"]+)"', toml_text, re.M))


def not_routing(profiles: Profiles, name: str) -> str:
    """Kept as a name for one specific unservable reason: nothing to classify.

    It is no longer a separate state. A profile that would not consult the judge
    is not emitted at all, so this only reports WHY, never a live condition.
    """
    reason = unservable(profiles, name)
    return reason if "nothing to " in reason else ""


def render(profiles: Profiles,
           recent_turn_window: int = 4, max_output_tokens: int = 1024,
           default_target: str = "coder") -> tuple[str, list[str]]:
    """Return (toml, skipped) — a profile that cannot be served is named, never
    emitted half-working."""
    body: list[str] = []
    clients: dict[str, str] = {}
    skipped: list[str] = []

    def target(table: str, model: str, adapter: str, level: str) -> str:
        clients[client_name(adapter, level)] = adapter
        return _target(table, model, adapter, level)

    for name in profiles.names():
        reason = unservable(profiles, name)
        if reason:
            skipped.append(f"{name} ({reason})")
            continue

        primary = profiles.primary_adapter(name)
        # auto can only classify among targets the caller's one credential pays
        # for, so it covers the primary adapter; anything else stays reachable
        # unservable() has already refused anything that could bypass the
        # judge, so from here every profile is a classifier over >= 2 targets
        # on one adapter.
        auto_modes = profiles.auto_modes(name)
        grouped = groups(profiles, name, auto_modes)
        auto_id = profiles.route_id(name)

        body.append(f"\n# {'=' * 74}\n# profile: {name}   →   model \"{auto_id}\"\n"
                    f"# {len(grouped)} distinct model(s) across {len(auto_modes)} modes\n"
                    f"# {'=' * 74}\n")

        judge_level = profiles.effort_of(name, "judge")
        body.append("# The judge's reasoning shares one output budget with its verdict\n"
                    "# JSON; thinking that exhausts the budget truncates the verdict and\n"
                    "# fails open to default_target on every request.\n")
        body.append(target(f"{name}_judge", profiles.model_of(name, "judge"),
                           primary, judge_level))

        for label, model, modes, level, adapter in grouped:
            note = f"# modes: {', '.join(modes)}\n" if len(modes) > 1 else ""
            body.append(note + target(f"{name}_{label}", model, adapter, level))

        targets = ", ".join(f'"{name}_{label}"' for label, *_ in grouped)
        # default_target must be one of the emitted labels: find the group that
        # contains the configured fallback mode.
        fallback = next((f"{name}_{label}" for label, _m, modes, *_ in grouped
                         if default_target in modes), f"{name}_{grouped[0][0]}")
        prompt = (profiles.get(name).get("prompt")
                  or build_prompt(grouped, name)).strip()
        # NO pinned per-mode routes are emitted. They were a passthrough each,
        # so a client naming one skipped the judge entirely — the one thing this
        # service exists to do. `auto` is the only way in.
        #
        # classify_trigger replaced session_affinity upstream. `every_request` is
        # already the default, and it is still written out: this one word is the
        # judge-decides-every-turn guarantee, and it must not silently become
        # `new_session` because a default moved. The other two values are exactly
        # the affinity this service refuses. message_hash_fallback stays false
        # and legal — upstream only rejects it when TRUE without new_session.
        body.append(f'''
[routes.auto_{name}]
id = "{auto_id}"
type = "llm_classifier"
mode = "custom"
classifier_target = "{name}_judge"
targets = [{targets}]
default_target = "{fallback}"
classify_trigger = "every_request"
message_hash_fallback = false
recent_turn_window = {recent_turn_window}
max_output_tokens = {max_output_tokens}
prompt = """
{prompt}
"""
response_schema = \'\'\'
{response_schema(grouped, name)}
\'\'\'

[routes.auto_{name}.policy]
type = "target_selector"
selector = "/decision/mode"
''')

    out = [HEADER, "\nschema_version = 1\n\n[targets]\n[routes]\n",
           "\n# The caller's own credential is relayed upstream; the server holds none.\n"
           "# forward_auth and api_key_env are mutually exclusive by design.\n"]
    out += [_client_block(client, adapter) for client, adapter in sorted(clients.items())]
    out += body
    return "".join(out), skipped
