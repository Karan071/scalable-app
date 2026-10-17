"""Org slug normalization (LLD-03 §3). The DB unique constraint is the authority."""

import re

_ALLOWED = re.compile(r"^[a-z0-9-]{3,40}$")
_RESERVED = frozenset({"admin", "api", "www", "platform", "scaleforge", "support", "console"})


def normalize_slug(slug: str) -> str:
    """Lowercase, ``[a-z0-9-]``, 3-40 chars, not reserved. Raises ``ValueError`` otherwise."""
    normalized = slug.strip().lower()
    if not _ALLOWED.match(normalized):
        raise ValueError(f"invalid slug: {slug!r}")
    if normalized in _RESERVED:
        raise ValueError(f"reserved slug: {slug!r}")
    return normalized
