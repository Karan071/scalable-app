"""Integration tests against the local Compose Postgres (`make test-env`). Skip if unreachable."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from app import repository
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"search-svc not ready (DB unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")


def _org() -> UUID:
    return uuid4()


async def _seed(org_id: UUID, *, slug: str, name: str, price_minor: int = 1000) -> None:
    from app.db import build_search_engine, build_search_session_factory
    from app.settings import Settings
    from sf_db import transaction

    settings = Settings()
    engine = build_search_engine(settings)
    factory = build_search_session_factory(engine)
    async with transaction(factory) as session:
        await repository.apply_visible_snapshot(
            session,
            org_id=org_id,
            product_id=uuid4(),
            slug=slug,
            name=name,
            description_text="",
            category_ids=[],
            min_price_minor=price_minor,
            max_price_minor=price_minor,
            version=1,
        )
    await engine.dispose()


def test_search_finds_a_seeded_product(client: TestClient) -> None:
    import asyncio

    org_id = _org()
    asyncio.run(_seed(org_id, slug="red-shirt", name="Red Running Shirt"))

    resp = client.get(f"/v1/stores/{org_id}/search", params={"q": "running shirt"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["slug"] for item in body["items"]] == ["red-shirt"]


def test_search_defaults_to_newest_when_q_is_empty(client: TestClient) -> None:
    resp = client.get(f"/v1/stores/{_org()}/search")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * 101},
        {"sort": "not-a-sort"},
        {"limit": 0},
        {"limit": 49},
        {"sort": "relevance", "page": 51},
        {"price_min": 100, "price_max": 50},
    ],
)
def test_invalid_queries_are_400_not_422(client: TestClient, params: dict) -> None:
    """LLD-05 §13's explicit contract for this one endpoint."""
    resp = client.get(f"/v1/stores/{_org()}/search", params=params)

    assert resp.status_code == 400, resp.text


def test_a_tampered_cursor_is_400(client: TestClient) -> None:
    resp = client.get(
        f"/v1/stores/{_org()}/search", params={"sort": "newest", "cursor": "not-a-real-cursor"}
    )

    assert resp.status_code == 400, resp.text


def test_search_is_scoped_to_its_org(client: TestClient) -> None:
    import asyncio

    org_a, org_b = _org(), _org()
    asyncio.run(_seed(org_b, slug="other-org-product", name="Other Org Product"))

    resp = client.get(f"/v1/stores/{org_a}/search", params={"q": "other org product"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
