import pytest
from app.handles import normalize_handle


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Karan", "karan"),
        ("ka-ran_c.9", "karanc9"),
        ("KARAN", "karan"),
    ],
)
def test_normalizes_case_and_strips_separators(raw: str, expected: str) -> None:
    assert normalize_handle(raw) == expected


def test_rejects_too_short() -> None:
    with pytest.raises(ValueError, match="invalid handle"):
        normalize_handle("ab")


def test_rejects_too_long() -> None:
    with pytest.raises(ValueError, match="invalid handle"):
        normalize_handle("a" * 31)


def test_rejects_disallowed_characters() -> None:
    with pytest.raises(ValueError, match="invalid handle"):
        normalize_handle("karan!!")


def test_nfkc_confusables_collapse_to_the_same_normalized_handle() -> None:
    # U+FF2B FULLWIDTH LATIN CAPITAL LETTER K -> NFKC folds to ASCII 'K'
    assert normalize_handle("Ｋar-an") == normalize_handle("Karan")
