# syntax=docker/dockerfile:1
#
# Build of the vendored Switchyard/ checkout — a git submodule beside this file,
# and empty in a clone made without --recursive, where the first COPY below is
# what fails. Kept separate from upstream's own Dockerfile so the checkout stays
# unmodified. Differences from upstream:
#
#   * Debian trixie (current stable) instead of bookworm (now oldstable).
#   * Rust 1.97.1 instead of 1.96.1. The checkout's rust-toolchain.toml pins
#     1.96.1; RUSTUP_TOOLCHAIN overrides that file, so the toolchain already in
#     the image is used instead of downloading the pinned one mid-build. 1.96.1
#     is the crates' MSRV (rust-version in Cargo.toml), and a newer compiler
#     satisfies a minimum.
#   * BuildKit cache mounts for the cargo registry and target dir. Upstream
#     copies sources then builds, so every source change recompiles all
#     dependencies from scratch.
#   * curl in the runtime image, so compose can health-check /health.
#   * python3 plus a curses management TUI, so the API key and the mode->model
#     mapping are set from one place instead of scattered env files. Stdlib
#     only: no pip, no wheels, nothing to patch beyond Debian's own python3.
#
# Builder and runtime must share a Debian release: the binary links the builder's
# glibc, and glibc compatibility runs forward only.

ARG RUST_VERSION=1.97.1
ARG DEBIAN_RELEASE=trixie

FROM rust:${RUST_VERSION}-${DEBIAN_RELEASE} AS builder
ARG RUST_VERSION
ENV RUSTUP_TOOLCHAIN=${RUST_VERSION}

WORKDIR /opt/switchyard
COPY Switchyard/Cargo.toml Switchyard/Cargo.lock Switchyard/rust-toolchain.toml ./
# .cargo/config.toml carries the workspace rustflags (target-cpu, force-frame-pointers).
COPY Switchyard/.cargo ./.cargo
COPY Switchyard/crates ./crates

# The target dir is a cache mount and so is absent from the resulting layer.
# Copy the binary somewhere real before the mount goes away.
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/switchyard/target,sharing=locked \
    cargo build --locked --release -p switchyard-server \
    && cp target/release/switchyard-server /usr/local/bin/switchyard-server

FROM debian:${DEBIAN_RELEASE}-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/switchyard-server /usr/local/bin/switchyard-server

# Same version the gateway runs, so the TUI can validate a generated Caddyfile
# before you restart the gateway onto it.
COPY --from=caddy:2.11.4-alpine /usr/bin/caddy /usr/local/bin/caddy

# The template is baked into the image; the entrypoint seeds /data/routes.toml
# from it on first start, and everything after that is edited in the volume.
COPY manage.py            /opt/switchyard/manage.py
COPY ingest.py            /opt/switchyard/ingest.py
COPY authcheck.py         /opt/switchyard/authcheck.py
COPY tui                  /opt/switchyard/tui
COPY entrypoint.sh        /usr/local/bin/entrypoint.sh

# The vendored source, kept so `check-upstream` can verify our assumptions
# against the exact tree this image was built from.
#
# The WHOLE checkout, deliberately — not a chosen subset. Naming individual
# crates here encoded a claim ("everything we depend on lives in these two"),
# and when upstream moved its config structs into switchyard-runner the claim
# quietly stopped holding: the checks went on grepping files the image no longer
# carried and reported a confident FAIL about a struct that was present and
# correct. A verifier pointed at a subset cannot do what this one promises, and
# it fails silently in both directions. ~6 MB of text is the right price.
#
# .dockerignore already drops .git, target/, the caches and the benchmark data.
COPY Switchyard /opt/switchyard/vendor
RUN chmod +x /usr/local/bin/entrypoint.sh /opt/switchyard/manage.py /opt/switchyard/ingest.py /opt/switchyard/authcheck.py

# `docker compose exec` does NOT run the entrypoint, so the `manage` verb is not
# reachable that way. A shim makes `exec switchyard manage` work against the
# running container, which is what you want on a live system — `run --rm` would
# start a second one.
RUN ln -s /opt/switchyard/manage.py /usr/local/bin/manage

# Config and secrets live here on a volume; uid 1000 must be able to write it.
RUN mkdir -p /data /var/log/switchyard && chown -R 1000:1000 /data /var/log/switchyard
VOLUME /data

ENV HOME=/tmp

USER 1000:1000
EXPOSE 4000

ENTRYPOINT ["entrypoint.sh"]
