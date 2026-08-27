"""Provider adapters.

Everything provider-specific lives behind this registry: the catalogue and its
JSON shape, pricing fields, base URL, wire format, key prefix and key
validation. Adding a provider is one new module plus one line here — nothing
outside this package should mention a provider's hostname.
"""

from __future__ import annotations

from . import openrouter

REGISTRY = {p.NAME: p for p in (openrouter,)}
DEFAULT = openrouter.NAME


def get(name: str):
    return REGISTRY.get(name) or REGISTRY[DEFAULT]


def names() -> list[str]:
    return list(REGISTRY)
