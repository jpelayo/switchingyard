# Switchingyard model router

One OpenAI/Anthropic-compatible endpoint. A cheap judge model classifies each
prompt into a task mode, and the request is served by the model you chose for
that mode.

```
client ──▶ /v1/chat/completions  model="auto"
             │
             ├─▶ judge  ──▶ {"decision":{"mode":"coder"}}
             │
             └─▶ targets.coder ──▶ OpenRouter
```

Modes: `simple`, `toolcall`, `coder`, `planner`, `researcher`, `reasoner`,
`image-in`, `image-out`, `narrative`, `user-interface`, `security`, `debug`.

Adding one is a procedure with silent failure modes — see the `profile-add`
skill.

## First run

Nobody holds anyone else's credentials. You authorise people; they pay.

```bash
# the routing engine is a submodule; without --recursive it clones empty and
# the build fails at a COPY that does not explain why
git clone --recursive <this repo>
# already cloned without it:  git submodule update --init

# once per host — volumes no project owns, so the stack stays disposable
docker volume create switchyard-config
docker volume create switchyard-logs

docker compose build
docker compose run --rm switchingyard manage
docker compose up -d
```

In the TUI: **Profiles** to set the ten models, then **Authorised keys → add**
to admit your first user. Nothing can call the service until both are done.

Upgrading the vendored NVIDIA checkout later: see [UPSTREAM.md](UPSTREAM.md).

## The management TUI

```bash
docker compose run --rm switchingyard manage     # stack down
docker compose exec switchingyard manage         # stack running
```

```
Switchyard — OpenRouter · caller-pays · 2 keys · 2 profiles
  Setup
     1  Authorised keys       who may call this, and as whom
     2  Profiles              mode→model sets
     3  Gateway and logs      public URL, limits, rotation
  Use
     4  Connect a client      curl · OpenCode · Claude Code · SDKs
     5  Test routing          probes: is the judge discriminating?
     6  Accounting            spend by period, model and tag
  Maintain
     7  Show configuration    8  Validate    9  Reload catalogue    10  Check upstream
```

Every row carries its own state, so the menu is also the status screen, and it
opens on the first thing that needs attention. Throughout: **type** to filter any
long list, **esc** goes back one level (clearing a filter first), destructive
actions preview then ask `y/N`, and lists you enter repeatedly stay open until
you leave them.

**The router reads its config once, at startup.** Editing a profile rewrites
`routes.toml` at once, but the running process keeps serving what it loaded — so
routing changes need a restart, and until then nothing you changed is in effect:

| you changed | takes effect |
|---|---|
| a key: added, revoked, reassigned | **immediately** — `authcheck` reads the roster per request |
| a profile: model, reasoning level, adapter | `docker compose restart switchingyard` |
| gateway limits, timeouts, log rolling | `docker compose restart gateway` |

You do not have to remember which. The TUI compares the config the router
actually loaded against the one on disk: while they differ the menu leads with
**Apply configuration**, the status line says `! NOT APPLIED`, and **Test
routing** refuses to run rather than measure the old routing and blame the
judge for it.

## Access control — caller-pays

**This server holds no provider credential.** Each user presents their own
OpenRouter key; the gateway checks its SHA-256 digest against a roster and
Switchyard relays the key upstream, so every user is billed on their own
account — including their share of the judge calls.

```
client ──Authorization: Bearer sk-or-…──▶ Caddy
                                            │ forward_auth ─▶ authcheck
                                            │   digest lookup → 200 + tag, or 401
                                            ▼
                                        switchyard  (forward_auth = true)
                                            │ relays the caller's key
                                            ▼
                                        OpenRouter — billed to that user
```

Adding someone: **Authorised keys → + add a key**. The wizard checks the key
prefix, verifies it live with the provider, asks for a **tag** (whose key it is)
and a **profile**, then stores `{tag, provider, sha256, last-4, profile}`. The key
itself is never written down — a leak of the database exposes nothing
spendable.

Two consequences of holding only a digest:

- Validation happens **once**, at add time; it is the only moment the plaintext
  exists. A key later rotated or revoked upstream simply starts returning 401,
  and must be re-authorised.
- Revocation is instant — `authcheck` re-reads the roster per request, so no
  restart is needed.

`/v1/stats`, `/v1/stats/reset`, `/metrics` and `/v1/routing/session-stats` are
not reachable through the gateway at all, even with a valid key.

## Profiles — per-user model mapping

A **profile** is a complete mode→model set plus its judge. Each user is assigned
one, which is how different people get different routing.

Switchyard picks a route purely by the model name in a request, so each profile
becomes its own model name: the default profile is `auto`, others are
`auto-<profile>`. Pinning a mode works the same way — `coder`, `coder-marta`.

```
profile        model name          slots set
default        auto               10/10
economy        auto-economy       10/10
```

There is no per-profile usage figure, and no count of who uses one. Nothing
observes it: any caller may send any profile's model name, and the routing log
records the model that *served* a request, not the route that was asked for.
Spend is attributable to a **model** and to a **tag**, never to a profile.

Every slot carries three things: a **model**, a **reasoning level**, and the
**adapter** the model comes from.

Two things worth knowing:

- **Modes may share a model.** The engine rejects a classifier whose targets
  resolve to the same model twice, so modes sharing one are merged into a single
  judge label (`coder-planner`) and the prompt describes the combined category.
  All nine on one model becomes a plain passthrough with no judge call at all.
  Because they are one target, they also share one reasoning level — setting it
  on either moves both, and the editor says so.
- **Profiles are a mapping choice, not an isolation boundary.** The profile is
  selected by the model name, so any caller may use any profile's name. They
  still pay with their own key, so there is no security consequence — but there
  is no enforcement either.

**`routes.toml` and the `Caddyfile` are generated** from the database, and
rewritten whenever anything in it changes. Read them; do not edit them.

## The judge always decides

There is no way to reach a model without the judge choosing it. Every path that
allowed it has been removed rather than reported:

| was possible | now |
|---|---|
| all modes on one model → passthrough, no judge call | the profile does not serve until two modes differ |
| client sends `coder` instead of `auto` | per-mode route ids are not emitted; `auto` is the only one |
| session affinity reused one decision per conversation | not offered; every turn is classified |
| a mode on another adapter, reachable only by name | mixed adapters do not serve — one profile, one adapter |

Only one thing can still put a request in front of a model the judge did not
pick: a **fail-open**, where the judge answers and its verdict will not parse.
That cannot be prevented from here, so it is counted — `switchyard.classifier_fail_open`
on `/metrics` — and **Validate** reports it as verdicts *discarded*, separately
from requests never judged.

The cost of this is real and worth stating: one judge call per turn, and for an
agent loop that is one per tool hop. A conversation cannot be pinned to a model
any more, so a large context is re-read by whichever model is chosen.

## Reasoning level

Every model that can think is told how hard to. The default is **high**, except
the judge, which starts **off**.

The choices come from the catalogue, which lists each model's own accepted
levels, so an unsupported one is never offered. Where a model lists none, the
ladder is `max, xhigh, high, medium, low` plus `off` — and `off` is hidden for
the models that refuse to be turned off at all. A model that cannot reason shows
`n/a` and is sent nothing.

Press `^E` on any row in the profile editor to change the level alone.

It is emitted as a per-target `extra_body`:

```toml
[targets.default_coder-planner]
id = "z-ai/glm-5.3"
llm_client = "openrouter_low"
extra_body = { reasoning = { effort = "low" } }
```

Three things follow from how the engine applies it:

- **It is a default, not a rule.** `extra_body` is merged only where the caller
  did not set that key, so a client sending its own `reasoning` overrides the
  profile. That is the right precedence; it is not enforcement.
- **One llm_client per level.** A client keys its models by id, so two targets
  sharing an id on one client collapse into one and the loser's `extra_body` is
  dropped with nothing but a log line. Splitting by level keeps every pair
  unique — which is why the file has `openrouter_high`, `openrouter_off` and so
  on, all pointing at the same URL.
- **The judge is the dangerous one.** Its reasoning and its verdict JSON share
  one `max_output_tokens`, and the levels are defined as a share of that budget:
  `max` spends about 95% on thinking. A truncated verdict is unparseable and
  routes every request to the fallback while still billing the judge call, and
  nothing in the response says so. Raising the judge above `off` therefore lifts
  `max_output_tokens` to at least 4096.

## Adapters

Each slot also names the **adapter** its model comes from. OpenRouter is the
only one today; the default for a new slot is set in Setup.

`auto` can only classify among targets **one credential pays for**: the caller
presents a single key and Switchyard relays it to whichever target the judge
picks. So a profile has a primary adapter — the one most of its modes use — and
that is what `auto` covers.

A mode moved to another adapter still works, but only by name: a client sends
`image-out` with that provider's key. The judge can never select it. The editor,
Show configuration and Validate all say which modes those are.

## Where state lives

Everything mutable is in two **external** volumes, so deleting this directory —
or running `docker compose down -v` — cannot touch it:

| path | what it is |
|---|---|
| `/data/switchyard.db` | **everything**: profiles, the key roster, gateway settings, the cached catalogue and the cost history |
| `/data/routes.toml` | generated router config |
| `/data/Caddyfile` | generated gateway config, mode `0600` |
| `/var/log/switchyard/routing.jsonl` | raw routing log, rotated by the sidecar |
| `/var/log/switchyard/gateway.log` | Caddy access log, rolled by Caddy |

One database, and two files derived from it. Both derived files are rewritten on
every change and on every container start, so deleting either is harmless — and
hand-editing either is pointless. (SQLite's own `-wal` and `-shm` companions sit
beside the database; they are part of it, not separate state.)

The roster holds SHA-256 digests and no keys, so a copy of the database exposes
nothing spendable.

Back up with `VACUUM INTO`, which is consistent against a database that is being
written — copying the file while the stack is up can capture a torn page:

```bash
docker compose exec switchingyard python3 -c \
  "import sqlite3; sqlite3.connect('/data/switchyard.db').execute(
     \"VACUUM INTO '/data/backup.db'\")"
docker run --rm -v switchyard-config:/d -v "$PWD":/b alpine \
  cp /d/backup.db /b/switchyard-backup.db
```

## Accounting

Switchyard records tokens, never cost, so option 8 prices the routing log
against the cached catalogue — the same table the model picker reads — and keeps
hour buckets in the same database. The `accounting`
sidecar ingests every 5 minutes and rotates the raw log past 1 GiB — only ever
*after* the bytes are aggregated, so rotation is never lossy.

The judge/served split is the point: it shows what classification costs and
whether routing pays for itself against an all-premium counterfactual.

These figures are derived, not billed, and **undercount** — failed judge calls
are never logged upstream, cancelled requests still cost, image generation is
charged per image on top of tokens, and catalogue prices drift. Treat
OpenRouter's own credits page as the authoritative total.

Rows aggregated before reasoning tokens stopped being counted twice keep the old
arithmetic; only a re-ingest restates them, so the series has a step in it rather
than a rewritten past.

`routes.toml` is generated from the database on every change and on every
start, so a hand edit is lost at the next one. Read it to see what the TUI
produced:

```bash
docker compose exec switchingyard cat /data/routes.toml
```

The **judge** has requirements the mode targets don't:

1. It must support **structured outputs** (JSON Schema). Custom-mode routing
   always sends `"strict": true` and accepts nothing else.
2. Its reasoning and its verdict JSON share the single `max_output_tokens`
   budget. Reasoning that eats the budget truncates the verdict.

And a choice, made when you pick it:

| kind | needs | trade |
|---|---|---|
| **full** | structured outputs **and** image input | classifies every turn |
| **imageless** | structured outputs only | a turn carrying an image is never classified — it falls open to the fallback mode |

The judge is shown the conversation verbatim, image blocks included
(`trim_messages` clones whole messages), so a text-only judge errors on any
image turn. `full` is the default and costs you choice — 203 of 401 models
against 320 — so `imageless` is there when the cheaper or better judge you want
cannot see. The trade travels with it: the profile editor, Show configuration
and Validate all say which turns stop being classified, and where they go
instead.

The TUI enforces the first two by filtering the model list, and handles the
third by starting the judge at `off` and raising the budget if you change it. Both
failures are otherwise silent: the request falls back to `default_target` and
you are still billed for the judge call.

## Is the judge actually working?

The single most important check. A broken judge looks identical to a working one
from the client side.

```bash
docker compose exec switchingyard manage      # option 6, Test routing
```

It sends one deliberately unambiguous prompt per mode — and reads the
`x-model-router-selected-model` header off each response, so it needs no prior
traffic and no log correlation. Two or more distinct targets means the judge is
discriminating. Everything landing on the fail-open target means it is failing
and every request is being misrouted while still billing a judge call.

One judge call and one 1-token completion per mode, so running it costs a fraction of
a cent.

A single probe disagreeing with its label is not a failure — the judge is
allowed to disagree. Only *no variation at all* is the alarm.

Do not use `/v1/stats` or `/metrics` for this. Judge-failure fallbacks are not
attributed there in v0.2.0 (upstream known issue #2), so a failing-open router
looks healthy in both.

For the reason behind a failure, set `RUST_LOG=switchyard_server=debug,libsy=debug`
in `compose.yaml` and restart.

## Skipping the judge

Send a mode name as the model instead of `auto` to pin it — no classification
call, no added latency:

```bash
-d '{"model":"coder","messages":[...]}'
```

Useful for clients that already know what they need, and for A/B testing a
mode's model against another.

## What costs what

Every request with `model="auto"` makes one judge call before the real call.
That is one extra round trip of latency per turn, plus the judge's tokens. It is
the price of tracking mode drift within a conversation, and it is not
configurable: the route is generated with `classify_trigger = "every_request"`,
and the two values that would classify once per conversation instead
(`user_turn`, `new_session`) are the affinity this service exists to refuse. A
conversation that is classified once can be served by a model the judge did not
choose for the current turn, which is indistinguishable from working.

## Log rotation

Three logs, three mechanisms, all bounded:

| log | rotated by | default |
|---|---|---|
| `routing.jsonl` | accounting sidecar, after aggregating | 1 GiB |
| `gateway.log` | Caddy itself | 100 MiB × 3, 30 days |
| container stdout | Docker `json-file` driver | 10 MB × 3 |

The first two are configurable under *Gateway configuration*.

## Operating notes

- Only the gateway is published, on **127.0.0.1** — the router itself has no
  port at all and is reachable only from the gateway on the compose network.
- Upstream Switchyard is pre-alpha (v0.2.0) and explicitly not for production.
  The checkout is pinned; expect breaking changes if you update it.
- A client that disconnects mid-request does not cancel the upstream call, so
  the provider still bills it (upstream known issue #1).
- The image needs a 2013-or-newer x86 host (or Neoverse-N1+ ARM): the workspace
  builds with `target-cpu=x86-64-v3`.
