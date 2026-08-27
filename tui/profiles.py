"""Profiles: a complete mode→model set plus its judge.

Each roster entry is assigned one, which is how per-user routing is expressed —
Switchyard picks a route purely by the model name in the request, so a profile
becomes its own route set and its own model name.

Backed by two tables rather than a JSON file. A slot is a row: which model, how
hard it may think, and which adapter it came from.
"""

from __future__ import annotations

import json
import re

import providers

MODES = ["simple", "toolcall", "coder", "planner", "researcher", "reasoner",
         "image-in", "image-out", "narrative",
         "user-interface", "security", "debug"]

# Slots whose model must have a particular capability, enforced by the picker so
# an unworkable choice is never offered. The judge needs image input because it
# is shown the conversation verbatim — including image blocks.
SLOT_REQUIRES = {
    "image-in": ("image_in",),
    "image-out": ("image_out",),
}

# What a judge must be able to do. The judge is shown the conversation verbatim
# — `trim_messages` (llm_class.rs:83) clones whole Messages, image blocks
# included — so a text-only judge errors on any turn carrying an image and the
# request falls open to the fallback target, silently.
#
# That is a real cost either way: `full` narrows the choice to models that can
# both emit a strict verdict and read an image; `imageless` opens it up and
# accepts that image turns are never classified.
JUDGE_KINDS = {
    "full": ("structured", "image_in"),
    "imageless": ("structured",),
}
JUDGE_KIND_DEFAULT = "full"
# Models to fill in when a mode is ADDED to the taxonomy, so a deployment that
# was serving keeps serving. Applied only to a profile that already has models —
# see `_migrate`. A mode with no entry here is left unset deliberately, and every
# profile then reports it as missing until someone chooses.
BACKFILL = {
    "user-interface": ("minimax/minimax-m3", "high"),
    "debug": ("z-ai/glm-5.3-flash", "high"),
}

JUDGE_KIND_NOTE = {
    "full": "reads images; classifies every turn",
    "imageless": "text only; image turns fail open to the fallback",
}
SLOTS = ["judge"] + MODES

# `_` separates profile from mode in generated TOML table names, so it must not
# appear in a profile name. Kept to TOML bare-key characters.
NAME_RE = re.compile(r"^[a-z0-9-]{1,24}$")
DEFAULT_NAME = "default"

# The adapter a slot gets when nothing else says otherwise.
DEFAULT_ADAPTER = providers.DEFAULT

# The judge starts with reasoning off: its thinking and its verdict JSON share
# one output budget, and thinking that exhausts the budget truncates the verdict,
# which fails open to default_target on every request. Raising it is allowed, but
# it must be a decision rather than an inherited default.
JUDGE_LEVEL = "off"

# Which profile is the default. Lives in `setting` because it is one value, not
# a property of any one profile.
DEFAULT_KEY = "default_profile"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


class Profiles:
    """The profile store, over an open database connection.

    Every method reads live, so two instances can never disagree and there is
    nothing to reload. Callers that used to rebuild this object to pick up a
    change can simply keep the one they have.
    """

    def __init__(self, con, adapter: str = ""):
        self.con = con
        self.adapter = adapter or DEFAULT_ADAPTER
        self._ensure_default()
        self._backfill()

    def _backfill(self) -> None:
        """Fill in modes added to the taxonomy since a profile was configured.

        Adding a mode makes every existing profile incomplete, and an incomplete
        profile is refused — so without this, growing the taxonomy takes a
        working deployment off the air. That has happened once already.

        Only profiles that already have models are touched. A blank one is left
        alone: pre-filling it would be choosing models for the operator, and a
        mode absent from BACKFILL is deliberately left unset so somebody decides.
        """
        rows = self.con.execute(
            "SELECT profile FROM slot WHERE model <> '' GROUP BY profile").fetchall()
        for r in rows:
            name = r["profile"]
            for mode, (model, level) in BACKFILL.items():
                if self.model_of(name, mode):
                    continue
                self.con.execute(
                    "INSERT INTO slot(profile, slot, model, effort, adapter) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(profile, slot) DO UPDATE SET "
                    "model=excluded.model, effort=excluded.effort",
                    (name, mode, model, level, self.adapter_of(name, mode)))
        if rows:
            self.con.commit()

    def _ensure_default(self) -> None:
        """A deployment always has at least one profile to edit.

        Absent is bootstrapped; it is not an error. What WOULD be an error — a
        store that will not open — raises out of `connect` long before here, so
        the two cases can no longer be confused.
        """
        if self.con.execute("SELECT COUNT(*) c FROM profile").fetchone()["c"]:
            return
        self.create(DEFAULT_NAME)
        self.set_default(DEFAULT_NAME)

    # -- queries ----------------------------------------------------------
    def names(self) -> list[str]:
        # default first, then alphabetical: the default is what people edit most
        rest = [r["name"] for r in self.con.execute(
            "SELECT name FROM profile WHERE name<>? ORDER BY name", (self.default_name,))]
        exists = self.con.execute("SELECT 1 FROM profile WHERE name=?",
                                  (self.default_name,)).fetchone()
        return ([self.default_name] if exists else []) + rest

    @property
    def default_name(self) -> str:
        r = self.con.execute("SELECT value FROM setting WHERE key=?",
                             (DEFAULT_KEY,)).fetchone()
        if r:
            return json.loads(r["value"])
        return DEFAULT_NAME

    def judge_kind(self, name: str) -> str:
        r = self.con.execute("SELECT judge_kind FROM profile WHERE name=?",
                             (name,)).fetchone()
        kind = (r["judge_kind"] if r else "") or JUDGE_KIND_DEFAULT
        return kind if kind in JUDGE_KINDS else JUDGE_KIND_DEFAULT

    def set_judge_kind(self, name: str, kind: str) -> bool:
        """Set the judge's kind. Returns True if the model was cleared with it.

        Tightening to `full` invalidates a text-only judge, and leaving it in
        place would look configured while failing on every image turn — so it
        goes, and has to be chosen again.
        """
        self.con.execute("UPDATE profile SET judge_kind=? WHERE name=?", (kind, name))
        self.con.commit()
        return False

    def requires(self, name: str, slot: str) -> tuple[str, ...]:
        """Capabilities a model must have to fill this slot in this profile."""
        if slot == "judge":
            return JUDGE_KINDS[self.judge_kind(name)]
        return SLOT_REQUIRES.get(slot, ())

    def get(self, name: str) -> dict:
        """The profile as a dict, for the generator and for cloning."""
        row = self.con.execute("SELECT prompt FROM profile WHERE name=?",
                               (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        slots = {r["slot"]: r for r in self.con.execute(
            "SELECT slot, model, effort, adapter FROM slot WHERE profile=?", (name,))}
        return {
            "judge": slots["judge"]["model"] if "judge" in slots else "",
            "modes": {m: (slots[m]["model"] if m in slots else "") for m in MODES},
            "effort": {s: slots[s]["effort"] for s in slots if slots[s]["effort"]},
            "provider": {s: slots[s]["adapter"] for s in slots},
            "prompt": row["prompt"],
        }

    def _cell(self, name: str, slot: str, column: str, default: str = "") -> str:
        r = self.con.execute(f"SELECT {column} v FROM slot WHERE profile=? AND slot=?",
                             (name, slot)).fetchone()
        return r["v"] if r else default

    def model_of(self, name: str, slot: str) -> str:
        return self._cell(name, slot, "model")

    def set_model(self, name: str, slot: str, model: str,
                  effort: str | None = None, adapter: str = "") -> None:
        adapter = adapter or self.adapter_of(name, slot)
        if effort is None:
            effort = (JUDGE_LEVEL if slot == "judge"
                      else providers.get(adapter).DEFAULT_LEVEL)
        self.con.execute(
            "INSERT INTO slot(profile, slot, model, effort, adapter) VALUES(?,?,?,?,?) "
            "ON CONFLICT(profile, slot) DO UPDATE SET "
            "model=excluded.model, effort=excluded.effort, adapter=excluded.adapter",
            (name, slot, model, effort, adapter))
        self.con.commit()

    def missing(self, name: str) -> list[str]:
        return [s for s in SLOTS if not self.model_of(name, s)]

    def complete(self, name: str) -> bool:
        return not self.missing(name)

    def route_id(self, name: str, mode: str | None = None) -> str:
        """The model name a client sends. The default profile keeps plain names."""
        base = mode or "auto"
        return base if name == self.default_name else f"{base}-{name}"

    # -- reasoning level --------------------------------------------------
    def effort_of(self, name: str, slot: str) -> str:
        return self._cell(name, slot, "effort")

    def set_effort(self, name: str, slot: str, level: str) -> list[str]:
        """Set a slot's reasoning level, and every slot it shares a target with.

        Two targets that resolve to one model are rejected by the engine
        (`llm_class.rs:750-755`) and `extra_body` is not part of that identity,
        so modes sharing a model are one target and cannot hold two levels.
        Propagating makes that unrepresentable instead of a startup failure.
        Returns the other slots that moved, so the UI can say so.
        """
        moved = self.sharing(name, slot) if slot != "judge" else []
        self.con.executemany(
            "UPDATE slot SET effort=? WHERE profile=? AND slot=?",
            [(level, name, s) for s in [slot] + moved])
        self.con.commit()
        return moved

    def sharing(self, name: str, slot: str) -> list[str]:
        """Other modes on the same model and adapter — i.e. the same target."""
        if slot == "judge":
            return []
        model, adapter = self.model_of(name, slot), self.adapter_of(name, slot)
        if not model:
            return []
        return [m for m in MODES if m != slot and self.model_of(name, m) == model
                and self.adapter_of(name, m) == adapter]

    def disagreeing(self, name: str) -> list[tuple[str, list[str]]]:
        """Groups sharing a target but holding different levels — only reachable
        by editing the database by hand, and a startup failure if emitted."""
        seen, out = set(), []
        for mode in MODES:
            if mode in seen or not self.model_of(name, mode):
                continue
            group = [mode] + self.sharing(name, mode)
            seen.update(group)
            if len({self.effort_of(name, m) for m in group}) > 1:
                out.append((self.model_of(name, mode), group))
        return out

    # -- adapter ----------------------------------------------------------
    def adapter_of(self, name: str, slot: str) -> str:
        return self._cell(name, slot, "adapter") or self.adapter

    def set_adapter(self, name: str, slot: str, adapter: str) -> None:
        """Change a slot's adapter. Clears the model: a slug means nothing in
        another catalogue, and a stale one would be admitted and then rejected."""
        self.con.execute(
            "INSERT INTO slot(profile, slot, model, effort, adapter) VALUES(?,?,'','',?) "
            "ON CONFLICT(profile, slot) DO UPDATE SET "
            "model='', effort='', adapter=excluded.adapter",
            (name, slot, adapter))
        self.con.commit()

    def primary_adapter(self, name: str) -> str:
        """The adapter `auto` classifies within — the one most modes use.

        forward_auth relays the caller's single credential to whichever target
        the judge picks, so one classifier route cannot span adapters. Ties go to
        the configured default so the answer does not wobble.
        """
        counts: dict[str, int] = {}
        for mode in MODES:
            a = self.adapter_of(name, mode)
            counts[a] = counts.get(a, 0) + 1
        if not counts:
            return self.adapter
        best = max(counts.values())
        top = sorted(a for a, c in counts.items() if c == best)
        return self.adapter if self.adapter in top else top[0]

    def auto_modes(self, name: str) -> list[str]:
        """Modes `auto` can classify among — those on the primary adapter."""
        primary = self.primary_adapter(name)
        return [m for m in MODES if self.adapter_of(name, m) == primary]

    def outside_auto(self, name: str) -> list[str]:
        """Modes the judge can never select, because they sit on another adapter.
        They stay reachable by their own pinned route id."""
        primary = self.primary_adapter(name)
        return [m for m in MODES if self.adapter_of(name, m) != primary]

    def adapters_in_use(self, name: str) -> list[str]:
        return sorted({self.adapter_of(name, s) for s in SLOTS})

    def set_prompt(self, name: str, prompt: str | None) -> None:
        self.con.execute("UPDATE profile SET prompt=? WHERE name=?", (prompt, name))
        self.con.commit()

    # -- mutations --------------------------------------------------------
    def create(self, name: str, clone_from: str | None = None) -> None:
        if not valid_name(name):
            raise ValueError(f"profile names must match {NAME_RE.pattern}")
        if self.con.execute("SELECT 1 FROM profile WHERE name=?", (name,)).fetchone():
            raise ValueError(f"profile {name!r} already exists")
        with self.con:
            self.con.execute("INSERT INTO profile(name, prompt) VALUES(?, NULL)", (name,))
            if clone_from:
                self.con.execute(
                    "INSERT INTO slot(profile, slot, model, effort, adapter) "
                    "SELECT ?, slot, model, effort, adapter FROM slot WHERE profile=?",
                    (name, clone_from))
                self.con.execute("UPDATE profile SET prompt="
                                 "(SELECT prompt FROM profile WHERE name=?) WHERE name=?",
                                 (clone_from, name))
            else:
                self.con.executemany(
                    "INSERT INTO slot(profile, slot, model, effort, adapter) "
                    "VALUES(?,?,'',?,?)",
                    [(name, s, JUDGE_LEVEL if s == "judge" else "", self.adapter)
                     for s in SLOTS])

    def rename(self, old: str, new: str) -> None:
        if not valid_name(new):
            raise ValueError(f"profile names must match {NAME_RE.pattern}")
        if self.con.execute("SELECT 1 FROM profile WHERE name=?", (new,)).fetchone():
            raise ValueError(f"profile {new!r} already exists")
        with self.con:
            # slot.profile is ON DELETE CASCADE, not ON UPDATE, so move the rows
            # explicitly rather than relying on the foreign key.
            self.con.execute("INSERT INTO profile(name, prompt) "
                             "SELECT ?, prompt FROM profile WHERE name=?", (new, old))
            self.con.execute("UPDATE slot SET profile=? WHERE profile=?", (new, old))
            self.con.execute("DELETE FROM profile WHERE name=?", (old,))
        if self.default_name == old:
            self.set_default(new)

    def delete(self, name: str) -> None:
        if name == self.default_name:
            raise ValueError("cannot delete the default profile")
        self.con.execute("DELETE FROM profile WHERE name=?", (name,))
        self.con.commit()

    def set_default(self, name: str) -> None:
        self.con.execute("INSERT INTO setting(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (DEFAULT_KEY, json.dumps(name)))
        self.con.commit()
