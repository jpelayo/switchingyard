#!/bin/sh
# Single entry point for the server, the management TUI, the membership check
# and the accounting sidecar.
#
# This server holds NO provider credential. Each caller presents their own key,
# the gateway checks its digest against the roster, and Switchyard relays it
# upstream — so every user is billed on their own account.
set -eu

# routes.toml and the Caddyfile are generated, never seeded from a template:
# /data/switchyard.db is the source of truth, and one route set per profile
# cannot be maintained as a static file.
seed_config() {
    mkdir -p /data
    python3 -c "import sys; sys.path.insert(0, '/opt/switchyard'); \
                import manage; skipped, (ok, detail) = manage.write_config(); \
                print(f'config regenerated: caddy {\"ok\" if ok else detail}' \
                      + (f'; NOT SERVING: {skipped}' if skipped else ''), \
                      file=sys.stderr)"
}

case "${1:-}" in
    manage)
        seed_config
        exec python3 /opt/switchyard/manage.py
        ;;
    authcheck)
        # Membership check for the gateway. Holds digests, never keys.
        exec python3 /opt/switchyard/authcheck.py
        ;;
    ingest)
        # Accounting sidecar: aggregate the routing log into SQLite, then rotate
        # it once everything in it is safely aggregated.
        shift
        exec python3 /opt/switchyard/ingest.py "$@"
        ;;
    check-upstream)
        seed_config
        exec python3 /opt/switchyard/ingest.py --check-upstream
        ;;
esac

seed_config

# Record the config the server is about to load, so the TUI can tell a config
# it has written from one the process is actually serving. Only here, on the
# server branch — the `manage` branch regenerates too, and marking there would
# claim the router had loaded a file it has never seen.
python3 -c "import sys; sys.path.insert(0, '/opt/switchyard'); \
            import manage; manage.mark_router_started()" || true

# No provider credential is loaded, and none exists to load: every caller
# supplies their own and it is relayed upstream.
exec switchyard-server "$@"
