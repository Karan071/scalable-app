"""Handle normalization (LLD-01 §11.1). The DB unique constraint is the authority (PRD §5.3)."""

import re
import unicodedata

_SEPARATORS = re.compile(r"[._-]")
_ALLOWED = re.compile(r"^[a-z0-9]{3,30}$")


def normalize_handle(handle: str) -> str:
    """NFKC -> casefold -> strip separators -> restrict to ``[a-z0-9]``, 3-30 chars.

    Raises ``ValueError`` if the result falls outside the allowed charset/length.
    """
    normalized = unicodedata.normalize("NFKC", handle).casefold()
    normalized = _SEPARATORS.sub("", normalized)
    if not _ALLOWED.match(normalized):
        raise ValueError(f"invalid handle: {handle!r}")
    return normalized
