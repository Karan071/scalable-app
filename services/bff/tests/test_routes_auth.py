import pytest
from app.routes_auth import _validate_return_to

ALLOWED = ["/", "/orders", "/console/"]


@pytest.mark.parametrize(
    "return_to",
    ["/orders", "/console/settings", "/"],
)
def test_allows_an_allow_listed_relative_path(return_to: str) -> None:
    assert _validate_return_to(return_to, ALLOWED) == return_to


@pytest.mark.parametrize(
    "return_to",
    [
        "//evil.example",
        "https://evil.example",
        "http://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
    ],
)
def test_falls_back_to_root_for_open_redirect_attempts(return_to: str) -> None:
    assert _validate_return_to(return_to, ALLOWED) == "/"


def test_rejects_a_path_outside_the_allow_list_when_root_is_not_allowed() -> None:
    assert _validate_return_to("/not-allow-listed", ["/orders", "/console/"]) == "/"
