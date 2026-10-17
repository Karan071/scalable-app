import pytest
from app.slugs import normalize_slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme-Store", "acme-store"),
        ("coffee123", "coffee123"),
        ("  padded  ", "padded"),
    ],
)
def test_normalizes_case_and_whitespace(raw: str, expected: str) -> None:
    assert normalize_slug(raw) == expected


def test_rejects_too_short() -> None:
    with pytest.raises(ValueError, match="invalid slug"):
        normalize_slug("ab")


def test_rejects_disallowed_characters() -> None:
    with pytest.raises(ValueError, match="invalid slug"):
        normalize_slug("acme_store!")


@pytest.mark.parametrize("reserved", ["admin", "api", "platform", "ADMIN"])
def test_rejects_reserved_slugs(reserved: str) -> None:
    with pytest.raises(ValueError, match="reserved slug"):
        normalize_slug(reserved)
