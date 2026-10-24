"""Integration tests against the local Compose Postgres (`make test-env`). Skip
if unreachable. Covers LLD-05 §13's "out-of-order product events -> final
doc equals the latest version" and the full-text/trigram/filter query shapes.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app import repository
from sf_db import build_session_factory
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    import os

    try:
        eng = create_async_engine(
            f"postgresql+asyncpg://svc_search:{os.environ['SF_DB_PASSWORD']}"
            f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
        )
        async with eng.connect():
            pass
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = build_session_factory(engine)
    async with factory() as s:
        yield s
        await s.commit()


def _org() -> UUID:
    return uuid4()


async def test_out_of_order_updates_converge_on_the_latest_version(session: AsyncSession) -> None:
    org, product = _org(), uuid4()

    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="v3",
        name="V3",
        description_text="",
        category_ids=[],
        min_price_minor=300,
        max_price_minor=300,
        version=3,
    )
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="v1",
        name="V1",
        description_text="",
        category_ids=[],
        min_price_minor=100,
        max_price_minor=100,
        version=1,
    )
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="v2",
        name="V2",
        description_text="",
        category_ids=[],
        min_price_minor=200,
        max_price_minor=200,
        version=2,
    )

    params = repository.SearchParams(
        q="",
        category=None,
        price_min=None,
        price_max=None,
        in_stock=None,
        sort="newest",
        limit=10,
        cursor=None,
        page=1,
    )
    hits = await repository.search(session, org, params)
    assert [h.slug for h in hits] == ["v3"]


async def test_a_stale_tombstone_does_not_hide_a_newer_active_product(
    session: AsyncSession,
) -> None:
    org, product = _org(), uuid4()
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="current",
        name="Current",
        description_text="",
        category_ids=[],
        min_price_minor=100,
        max_price_minor=100,
        version=5,
    )

    await repository.apply_tombstone(session, org_id=org, product_id=product, version=2)

    params = repository.SearchParams(
        q="",
        category=None,
        price_min=None,
        price_max=None,
        in_stock=None,
        sort="newest",
        limit=10,
        cursor=None,
        page=1,
    )
    hits = await repository.search(session, org, params)
    assert [h.slug for h in hits] == ["current"]


async def test_a_stale_active_update_cannot_resurrect_a_newer_tombstone(
    session: AsyncSession,
) -> None:
    org, product = _org(), uuid4()
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="was-active",
        name="Was active",
        description_text="",
        category_ids=[],
        min_price_minor=100,
        max_price_minor=100,
        version=1,
    )
    await repository.apply_tombstone(session, org_id=org, product_id=product, version=4)

    # A late, stale "active" update at v2 (< v4) arrives after the tombstone.
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=product,
        slug="stale",
        name="Stale",
        description_text="",
        category_ids=[],
        min_price_minor=1,
        max_price_minor=1,
        version=2,
    )

    params = repository.SearchParams(
        q="",
        category=None,
        price_min=None,
        price_max=None,
        in_stock=None,
        sort="newest",
        limit=10,
        cursor=None,
        page=1,
    )
    hits = await repository.search(session, org, params)
    assert hits == []


async def test_relevance_search_ranks_a_real_match_and_tolerates_a_typo(
    session: AsyncSession,
) -> None:
    org = _org()
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=uuid4(),
        slug="red-shirt",
        name="Red Running Shirt",
        description_text="a breathable shirt for running",
        category_ids=[],
        min_price_minor=1000,
        max_price_minor=1000,
        version=1,
    )
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=uuid4(),
        slug="blue-hat",
        name="Blue Hat",
        description_text="a warm hat",
        category_ids=[],
        min_price_minor=500,
        max_price_minor=500,
        version=1,
    )

    exact = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="running shirt",
            category=None,
            price_min=None,
            price_max=None,
            in_stock=None,
            sort="relevance",
            limit=10,
            cursor=None,
            page=1,
        ),
    )
    assert [h.slug for h in exact] == ["red-shirt"]

    typo = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="runing shrt",
            category=None,
            price_min=None,
            price_max=None,
            in_stock=None,
            sort="relevance",
            limit=10,
            cursor=None,
            page=1,
        ),
    )
    assert [h.slug for h in typo] == ["red-shirt"]


async def test_category_and_price_filters(session: AsyncSession) -> None:
    org, category = _org(), uuid4()
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=uuid4(),
        slug="in-category",
        name="In category",
        description_text="",
        category_ids=[category],
        min_price_minor=1000,
        max_price_minor=1000,
        version=1,
    )
    await repository.apply_visible_snapshot(
        session,
        org_id=org,
        product_id=uuid4(),
        slug="other-category",
        name="Other category",
        description_text="",
        category_ids=[],
        min_price_minor=50,
        max_price_minor=50,
        version=1,
    )

    by_category = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="",
            category=category,
            price_min=None,
            price_max=None,
            in_stock=None,
            sort="newest",
            limit=10,
            cursor=None,
            page=1,
        ),
    )
    assert [h.slug for h in by_category] == ["in-category"]

    by_price = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="",
            category=None,
            price_min=500,
            price_max=2000,
            in_stock=None,
            sort="price_asc",
            limit=10,
            cursor=None,
            page=1,
        ),
    )
    assert [h.slug for h in by_price] == ["in-category"]


async def test_newest_sort_pages_with_a_keyset_cursor(session: AsyncSession) -> None:
    org = _org()
    for i in range(3):
        await repository.apply_visible_snapshot(
            session,
            org_id=org,
            product_id=uuid4(),
            slug=f"p{i}",
            name=f"P{i}",
            description_text="",
            category_ids=[],
            min_price_minor=i,
            max_price_minor=i,
            version=1,
        )

    first = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="",
            category=None,
            price_min=None,
            price_max=None,
            in_stock=None,
            sort="newest",
            limit=2,
            cursor=None,
            page=1,
        ),
    )
    assert len(first) == 2

    second = await repository.search(
        session,
        org,
        repository.SearchParams(
            q="",
            category=None,
            price_min=None,
            price_max=None,
            in_stock=None,
            sort="newest",
            limit=2,
            cursor=(first[-1].created_at, first[-1].product_id),
            page=1,
        ),
    )
    assert {h.slug for h in first} & {h.slug for h in second} == set()
