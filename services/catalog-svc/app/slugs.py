"""Product/category slug normalization (LLD-05 §2). The DB unique constraint
is the authority — unlike org slugs (orgs-svc/app/slugs.py), these have no
reserved-word list: a product slug is never used as a route or subdomain."""

import re

_ALLOWED = re.compile(r"^[a-z0-9-]{1,80}$")


def normalize_slug(slug: str) -> str:
    """Lowercase, ``[a-z0-9-]``, 1-80 chars. Raises ``ValueError`` otherwise."""
    normalized = slug.strip().lower()
    if not _ALLOWED.match(normalized):
        raise ValueError(f"invalid slug: {slug!r}")
    return normalized
