"""orders-svc business logic: the checkout saga (LLD-06 §3-§5), the order
state machine, and admin ops (§6).

Scope note (this epic, E3.5): payments-svc doesn't exist yet (E3.6 is
next), so the AUTHORIZING/CAPTURING steps are written against LLD-06 §7's
documented contract but can't run live. Every other participant
(cart-svc, catalog-svc, inventory-svc) does. See the "As built" notes in
LLD-06 for what's simplified here (RE_RESERVING, the 5x capture retry,
VOIDING) and why.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid7

import httpx
from sf_db import transaction, violated_unique_constraint
from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import clients, idempotency, repository
from .models import Order, OrderLine

_NOTIFY_CHANNEL = "outbox_orders"
_EVENTS_EXCHANGE = "sf.events"
_COMMANDS_EXCHANGE = "sf.commands"


class CheckoutInProgress(Exception):
    pass


class IdempotencyKeyReused(Exception):
    pass


@dataclass(frozen=True)
class ActionResult:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class Participants:
    cart: clients.CartClient
    catalog: clients.CatalogClient
    inventory: clients.InventoryClient
    payments: clients.PaymentsClient


# --- shared helpers -----------------------------------------------------------


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    exchange: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    org_id: UUID,
    data: dict[str, Any],
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="orders-svc",
        subject=aggregate_id,
        data=data,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_version=aggregate_version,
        org_id=str(org_id),
    )
    await add_to_outbox(
        session,
        envelope,
        exchange=exchange,
        routing_key=routing_key,
        aggregate_id=aggregate_id,
        notify_channel=_NOTIFY_CHANNEL,
    )


def _order_summary(order: Order) -> dict[str, Any]:
    return {
        "order_id": str(order.id),
        "org_id": str(order.org_id),
        "customer_id": str(order.customer_id),
        "status": order.status,
        "payment_status": order.payment_status,
        "total_minor": order.total_minor,
        "currency": order.currency,
    }


async def _emit_status_changed(session: AsyncSession, order: Order, *, from_status: str) -> None:
    await _emit(
        session,
        event_type="order.status_changed",
        routing_key="order.status_changed.v1",
        exchange=_EVENTS_EXCHANGE,
        aggregate_type="order",
        aggregate_id=str(order.id),
        aggregate_version=order.version,
        org_id=order.org_id,
        data={
            "order_id": str(order.id),
            "customer_id": str(order.customer_id),
            "org_id": str(order.org_id),
            "from": from_status,
            "to": order.status,
        },
    )


async def _mark_order_cancelled(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID, reason: str
) -> None:
    """Idempotent: a duplicate compensation call for an already-cancelled
    order is a no-op (several saga paths can converge here)."""
    async with transaction(session_factory) as session:
        order = await repository.lock_order(session, order_id)
        assert order is not None
        if order.status == "cancelled":
            return
        from_status = order.status
        repository.apply_order_transition(
            session,
            order,
            to_status="cancelled",
            actor_type="system",
            actor_id="checkout-saga",
            reason=reason,
            cancel_reason=reason,
        )
        await _emit(
            session,
            event_type="order.cancelled",
            routing_key="order.cancelled.v1",
            exchange=_EVENTS_EXCHANGE,
            aggregate_type="order",
            aggregate_id=str(order.id),
            aggregate_version=order.version,
            org_id=order.org_id,
            data=_order_summary(order),
        )
        await _emit_status_changed(session, order, from_status=from_status)


async def _emit_command(
    session: AsyncSession,
    *,
    command_type: str,
    routing_key: str,
    org_id: UUID,
    order_id: UUID,
    data: dict[str, Any],
) -> None:
    await _emit(
        session,
        event_type=command_type,
        routing_key=routing_key,
        exchange=_COMMANDS_EXCHANGE,
        aggregate_type="order",
        aggregate_id=str(order_id),
        aggregate_version=1,
        org_id=org_id,
        data=data,
    )


# --- generic idempotency wrapper (LLD-06 §4) --------------------------------


async def _with_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: str,
    principal_id: UUID,
    key: UUID,
    request_hash: str,
    run: Callable[[UUID | None], Awaitable[ActionResult]],
) -> ActionResult:
    claim = await idempotency.claim(
        session_factory, scope=scope, principal_id=principal_id, key=key, request_hash=request_hash
    )
    if isinstance(claim, idempotency.Replay):
        return ActionResult(status_code=claim.status_code, body=claim.body)
    if isinstance(claim, idempotency.InProgress):
        raise CheckoutInProgress()
    if isinstance(claim, idempotency.KeyReused):
        raise IdempotencyKeyReused()

    result = await run(claim.resource_id)
    await idempotency.complete(
        session_factory,
        scope=scope,
        principal_id=principal_id,
        key=key,
        status_code=result.status_code,
        body=result.body,
    )
    return result


# --- checkout (LLD-06 §3-§5) -------------------------------------------------


def _pending_body(order_id: UUID, *, payment_status: str) -> dict[str, Any]:
    return {
        "order_id": str(order_id),
        "status": "pending",
        "payment_status": payment_status,
        "links": {
            "self": f"/v1/me/orders/{order_id}",
            "events": f"/v1/orders/{order_id}/status",
        },
    }


async def checkout(
    session_factory: async_sessionmaker[AsyncSession],
    participants: Participants,
    *,
    customer_id: UUID,
    org_id: UUID,
    idempotency_key: UUID,
    cart_version: int,
    promo_code: str | None,
    shipping_address: dict[str, Any],
    payment_method_token: str,
    reservation_ttl_seconds: int,
    saga_deadline_seconds: int,
) -> ActionResult:
    request_hash = idempotency.canonical_hash(
        {
            "org_id": str(org_id),
            "cart_version": cart_version,
            "promo_code": promo_code,
            "shipping_address": shipping_address,
            "payment_method_token": payment_method_token,
        }
    )

    async def run(resource_id: UUID | None) -> ActionResult:
        if resource_id is not None:
            # LLD-06 §4: "resume from checkout_sagas via resource_id (never
            # re-create the order)". The owner crashed after creating the
            # order -- this retried request carries the same body (the
            # request_hash match already proved that), so it still has
            # the payment_method_token needed to resume reserve/authorize
            # idempotently; a background recovery tick (recovery.py)
            # never does, which is why it's limited to deadline
            # compensation instead (see its own docstring).
            return await _resume_checkout(
                session_factory,
                participants,
                order_id=resource_id,
                payment_method_token=payment_method_token,
                reservation_ttl_seconds=reservation_ttl_seconds,
            )
        return await _run_checkout(
            session_factory,
            participants,
            customer_id=customer_id,
            org_id=org_id,
            idempotency_key=idempotency_key,
            cart_version=cart_version,
            promo_code=promo_code,
            shipping_address=shipping_address,
            payment_method_token=payment_method_token,
            reservation_ttl_seconds=reservation_ttl_seconds,
            saga_deadline_seconds=saga_deadline_seconds,
        )

    return await _with_idempotency(
        session_factory,
        scope="checkout",
        principal_id=customer_id,
        key=idempotency_key,
        request_hash=request_hash,
        run=run,
    )


async def _checkout_status(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID
) -> ActionResult:
    """Reports the order's current outcome without driving it forward --
    for a resumed saga that's already past the synchronous steps (nothing
    left for *this* request to do), or already terminal."""
    async with transaction(session_factory) as session:
        order = await repository.get_order(session, order_id)
    assert order is not None
    if order.status == "cancelled":
        title = {
            "out_of_stock": "out-of-stock",
            "payment_declined": "payment-declined",
        }.get(order.cancel_reason or "", "checkout-failed")
        status_code = 402 if order.cancel_reason == "payment_declined" else 409
        return ActionResult(status_code=status_code, body={"title": title})
    return ActionResult(
        status_code=202, body=_pending_body(order_id, payment_status=order.payment_status)
    )


async def _resume_checkout(
    session_factory: async_sessionmaker[AsyncSession],
    participants: Participants,
    *,
    order_id: UUID,
    payment_method_token: str,
    reservation_ttl_seconds: int,
) -> ActionResult:
    """LLD-06 §5.4: "crash orders-svc between reserve and authorize ->
    the retry resumes". Only a *client* retry (this function) can resume
    the synchronous reserve/authorize steps -- it alone carries the
    payment_method_token again. Re-runs whichever idempotent step the
    saga hadn't finished; anything past that is just reported."""
    async with transaction(session_factory) as session:
        order = await repository.get_order(session, order_id)
        saga = await repository.get_saga(session, order_id)
    assert order is not None and saga is not None

    if saga.state in ("RESERVING", "RESERVED"):
        lines = await _order_line_tuples(session_factory, order_id)
        return await _advance_past_reserve(
            session_factory,
            participants,
            order=order,
            reserve_lines=lines,
            reservation_ttl_seconds=reservation_ttl_seconds,
            payment_method_token=payment_method_token,
        )
    if saga.state == "AUTHORIZING":
        return await _do_authorize(
            session_factory,
            participants,
            order=order,
            payment_method_token=payment_method_token,
            from_state="AUTHORIZING",
        )
    return await _checkout_status(session_factory, order_id=order_id)


async def _order_line_tuples(
    session_factory: async_sessionmaker[AsyncSession], order_id: UUID
) -> list[tuple[UUID, int]]:
    async with transaction(session_factory) as session:
        lines = await repository.list_order_lines(session, order_id)
    return [(line.sku_id, line.qty) for line in lines]


async def _run_checkout(
    session_factory: async_sessionmaker[AsyncSession],
    participants: Participants,
    *,
    customer_id: UUID,
    org_id: UUID,
    idempotency_key: UUID,
    cart_version: int,
    promo_code: str | None,
    shipping_address: dict[str, Any],
    payment_method_token: str,
    reservation_ttl_seconds: int,
    saga_deadline_seconds: int,
) -> ActionResult:
    # LLD-06 §5.1's sequence diagram shows the cart snapshot and the
    # catalog quote as parallel ("par") calls, but the quote needs the
    # snapshot's own lines (sku_id/qty) to price -- there is no way to
    # start both at once. Run them sequentially; corrected in this LLD's
    # "As built" note.
    snapshot = await participants.cart.get_active_snapshot(customer_id=customer_id, org_id=org_id)
    if snapshot is None or not snapshot.items:
        return ActionResult(status_code=409, body={"title": "cart-changed"})
    if snapshot.version != cart_version:
        return ActionResult(status_code=409, body={"title": "cart-changed"})

    try:
        quote = await participants.catalog.quote(
            org_id=org_id,
            lines=[(item.sku_id, item.qty) for item in snapshot.items],
            promo_code=promo_code,
        )
    except clients.StoreUnavailable:
        return ActionResult(status_code=403, body={"title": "store-unavailable"})
    if not quote.valid:
        return ActionResult(status_code=409, body={"title": "cart-changed"})

    order_id = uuid7()
    now = datetime.now(UTC)
    order = Order(
        id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        status="pending",
        payment_status="none",
        cart_id=snapshot.id,
        cart_version=snapshot.version,
        idempotency_key=idempotency_key,
        currency=quote.currency,
        subtotal_minor=quote.subtotal_minor,
        discount_minor=quote.discount_minor,
        shipping_minor=quote.shipping_minor,
        tax_minor=quote.tax_minor,
        total_minor=quote.total_minor,
        promo_code=promo_code,
        pricing_fingerprint=quote.pricing_fingerprint,
        shipping_address=shipping_address,
        cancel_reason=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    lines = [
        OrderLine(
            order_id=order_id,
            line_no=i + 1,
            sku_id=line.sku_id,
            product_id=line.product_id,
            name_snapshot=line.product_name,
            unit_price_minor=line.unit_price_minor,
            qty=line.qty,
            line_total_minor=line.line_total_minor,
        )
        for i, line in enumerate(quote.lines)
    ]
    deadline_at = now + timedelta(seconds=saga_deadline_seconds)

    try:
        async with transaction(session_factory) as session:
            await repository.insert_order(session, order, lines)
            await repository.insert_saga(session, order_id=order_id, deadline_at=deadline_at)
            await idempotency.attach_resource(
                session,
                scope="checkout",
                principal_id=customer_id,
                key=idempotency_key,
                resource_id=order_id,
            )
            await _emit(
                session,
                event_type="order.created",
                routing_key="order.created.v1",
                exchange=_EVENTS_EXCHANGE,
                aggregate_type="order",
                aggregate_id=str(order_id),
                aggregate_version=order.version,
                org_id=org_id,
                data=_order_summary(order),
            )
    except IntegrityError as exc:
        if violated_unique_constraint(exc) == "uq_orders_cart_version_active":
            return ActionResult(status_code=409, body={"title": "cart-changed"})
        raise

    return await _advance_past_reserve(
        session_factory,
        participants,
        order=order,
        reserve_lines=[(line.sku_id, line.qty) for line in quote.lines],
        reservation_ttl_seconds=reservation_ttl_seconds,
        payment_method_token=payment_method_token,
    )


async def _do_reserve(
    session_factory: async_sessionmaker[AsyncSession],
    inventory_client: clients.InventoryClient,
    *,
    order: Order,
    reserve_lines: list[tuple[UUID, int]],
    reservation_ttl_seconds: int,
) -> ActionResult | None:
    """LLD-06 §5.1 steps 5-7. Idempotent by ``order_id``, so safe to call
    again whether the saga is still ``RESERVING`` or already ``RESERVED``.
    Returns an :class:`ActionResult` only if checkout ends here
    (out-of-stock); ``None`` means it advanced to ``AUTHORIZING``."""
    order_id, org_id = order.id, order.org_id
    reserve_result = await inventory_client.reserve(
        order_id=order_id,
        org_id=org_id,
        lines=reserve_lines,
        ttl_seconds=reservation_ttl_seconds,
        on_behalf_of=order.customer_id,
    )
    if not reserve_result.reserved:
        async with transaction(session_factory) as session:
            await repository.guarded_transition(
                session, order_id, from_state="RESERVING", to_state="CANCELLED"
            )
        await _mark_order_cancelled(session_factory, order_id=order_id, reason="out_of_stock")
        return ActionResult(status_code=409, body={"title": "out-of-stock"})

    async with transaction(session_factory) as session:
        await repository.guarded_transition(
            session,
            order_id,
            from_state="RESERVING",
            to_state="RESERVED",
            reservation_id=reserve_result.reservation_id,
        )
        await repository.guarded_transition(
            session, order_id, from_state="RESERVED", to_state="AUTHORIZING"
        )
    return None


async def _do_authorize(
    session_factory: async_sessionmaker[AsyncSession],
    participants: Participants,
    *,
    order: Order,
    payment_method_token: str,
    from_state: str,
) -> ActionResult:
    """LLD-06 §5.1 steps 8-13. ``from_state`` is ``AUTHORIZING`` (first
    attempt, or a client retry resuming one) -- idempotent by
    ``order_id`` on the provider side (ADR-0008), so safe to call again."""
    order_id, org_id, customer_id = order.id, order.org_id, order.customer_id
    try:
        auth = await participants.payments.authorize(
            order_id=order_id,
            org_id=org_id,
            customer_id=customer_id,
            amount_minor=order.total_minor,
            currency=order.currency,
            payment_method_token=payment_method_token,
        )
    except clients.PaymentDeclined:
        await _release_reservation(
            session_factory,
            participants.inventory,
            order_id=order_id,
            org_id=org_id,
            from_state=from_state,
            reason="payment_declined",
        )
        return ActionResult(status_code=402, body={"title": "payment-declined"})
    except (httpx.TimeoutException, httpx.TransportError):
        # payments-svc unreachable or slow -- today that's because it
        # doesn't exist yet (E3.6); in production it's a real provider
        # timeout. Either way: AUTH_PENDING. LLD-06 §5.4: it's
        # payments-svc that polls the provider from here, and a webhook
        # that ultimately resolves it -- orders-svc's own recovery worker
        # only compensates past the deadline (see its docstring).
        async with transaction(session_factory) as session:
            await repository.guarded_transition(
                session,
                order_id,
                from_state=from_state,
                to_state="AUTH_PENDING",
                next_action_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        return ActionResult(
            status_code=202, body=_pending_body(order_id, payment_status="authorizing")
        )

    async with transaction(session_factory) as session:
        order_row = await repository.lock_order(session, order_id)
        assert order_row is not None
        repository.set_payment_status(order_row, "authorized")
        await repository.guarded_transition(
            session,
            order_id,
            from_state=from_state,
            to_state="AUTHORIZED",
            payment_id=auth.payment_id,
        )
        await repository.guarded_transition(
            session, order_id, from_state="AUTHORIZED", to_state="COMMITTING"
        )
        await _emit_command(
            session,
            command_type="inventory.commit_requested",
            routing_key="inventory.commit_requested.v1",
            org_id=org_id,
            order_id=order_id,
            data={"order_id": str(order_id)},
        )

    return ActionResult(status_code=202, body=_pending_body(order_id, payment_status="authorized"))


async def _advance_past_reserve(
    session_factory: async_sessionmaker[AsyncSession],
    participants: Participants,
    *,
    order: Order,
    reserve_lines: list[tuple[UUID, int]],
    reservation_ttl_seconds: int,
    payment_method_token: str,
) -> ActionResult:
    """The synchronous tail of checkout (LLD-06 §5.1, steps 5-13): reserve,
    then authorize, then hand off to the async commit/capture continuation."""
    result = await _do_reserve(
        session_factory,
        participants.inventory,
        order=order,
        reserve_lines=reserve_lines,
        reservation_ttl_seconds=reservation_ttl_seconds,
    )
    if result is not None:
        return result
    return await _do_authorize(
        session_factory,
        participants,
        order=order,
        payment_method_token=payment_method_token,
        from_state="AUTHORIZING",
    )


async def _release_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    inventory_client: clients.InventoryClient,
    *,
    order_id: UUID,
    org_id: UUID,
    from_state: str,
    reason: str,
) -> bool:
    """Returns whether this call actually performed the release (``False``
    means the saga wasn't in ``from_state`` -- a stale/duplicate call)."""
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session, order_id, from_state=from_state, to_state="RELEASING"
        )
    if not transitioned:
        return False
    await inventory_client.release(order_id=order_id, org_id=org_id, reason=reason)
    async with transaction(session_factory) as session:
        await repository.guarded_transition(
            session, order_id, from_state="RELEASING", to_state="CANCELLED"
        )
    await _mark_order_cancelled(session_factory, order_id=order_id, reason=reason)
    return True


# --- async saga continuation (LLD-06 §5.1 steps 14-18, event-driven) -------


async def on_inventory_committed(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID
) -> None:
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session, order_id, from_state="COMMITTING", to_state="CAPTURING"
        )
        if not transitioned:
            return
        order = await repository.lock_order(session, order_id)
        assert order is not None
        repository.set_payment_status(order, "capturing")
        await _emit_command(
            session,
            command_type="payment.capture_requested",
            routing_key="payment.capture_requested.v1",
            org_id=order.org_id,
            order_id=order_id,
            data={"order_id": str(order_id), "org_id": str(order.org_id)},
        )


async def on_inventory_reservation_expired(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID
) -> None:
    """Simplified from LLD-06 §5.2's ``COMMITTING --> RE_RESERVING`` edge:
    this cancels rather than re-reserving. See the LLD's "As built" note."""
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session, order_id, from_state="COMMITTING", to_state="CANCELLED"
        )
    if transitioned:
        await _mark_order_cancelled(
            session_factory, order_id=order_id, reason="reservation_expired"
        )


async def on_payment_authorized(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID, payment_id: UUID
) -> None:
    """The async resolution of ``AUTH_PENDING`` (a delayed webhook) --
    continues exactly where the synchronous path would have."""
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session,
            order_id,
            from_state="AUTH_PENDING",
            to_state="AUTHORIZED",
            payment_id=payment_id,
        )
        if not transitioned:
            return
        order = await repository.lock_order(session, order_id)
        assert order is not None
        repository.set_payment_status(order, "authorized")
        await repository.guarded_transition(
            session, order_id, from_state="AUTHORIZED", to_state="COMMITTING"
        )
        await _emit_command(
            session,
            command_type="inventory.commit_requested",
            routing_key="inventory.commit_requested.v1",
            org_id=order.org_id,
            order_id=order_id,
            data={"order_id": str(order_id)},
        )


async def on_payment_failed(
    session_factory: async_sessionmaker[AsyncSession],
    inventory_client: clients.InventoryClient,
    *,
    order_id: UUID,
    org_id: UUID,
) -> None:
    """LLD-06 §7.2 has one ``payment.failed`` event for both a declined
    authorization and a failed capture -- which one applies depends on the
    saga's *current* state, not the event itself. Tries the AUTH_PENDING
    decline path's guarded transition first (a no-op if the saga isn't
    there); if that didn't apply, falls through to the capture-failure
    path, whose own guard makes it equally safe to call unconditionally."""
    released = await _release_reservation(
        session_factory,
        inventory_client,
        order_id=order_id,
        org_id=org_id,
        from_state="AUTH_PENDING",
        reason="payment_declined",
    )
    if not released:
        await on_capture_failed(session_factory, order_id=order_id, org_id=org_id)


async def on_payment_succeeded(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID, payment_id: UUID
) -> None:
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session, order_id, from_state="CAPTURING", to_state="COMPLETED"
        )
        if not transitioned:
            return
        order = await repository.lock_order(session, order_id)
        assert order is not None
        from_status = order.status
        repository.apply_order_transition(
            session,
            order,
            to_status="confirmed",
            actor_type="system",
            actor_id="checkout-saga",
            payment_status="captured",
        )
        await _emit(
            session,
            event_type="order.confirmed",
            routing_key="order.confirmed.v1",
            exchange=_EVENTS_EXCHANGE,
            aggregate_type="order",
            aggregate_id=str(order_id),
            aggregate_version=order.version,
            org_id=order.org_id,
            data=_order_summary(order),
        )
        await _emit_status_changed(session, order, from_status=from_status)
        await _emit(
            session,
            event_type="notification.requested",
            routing_key="notification.requested.v1",
            exchange=_EVENTS_EXCHANGE,
            aggregate_type="order",
            aggregate_id=str(order_id),
            aggregate_version=order.version,
            org_id=order.org_id,
            data={
                "template": "order_confirmed",
                "customer_id": str(order.customer_id),
                "params": {"order_id": str(order_id)},
            },
        )


async def on_capture_failed(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID, org_id: UUID
) -> None:
    """Simplified from LLD-06 §5.4's "retry ≤ 5, then restock + void":
    this treats the first capture failure as permanent. See the LLD's "As
    built" note -- the retry budget needs payments-svc to exist to be
    worth building against."""
    async with transaction(session_factory) as session:
        transitioned = await repository.guarded_transition(
            session, order_id, from_state="CAPTURING", to_state="RESTOCKING"
        )
        if not transitioned:
            return
        lines = await repository.list_order_lines(session, order_id)
        await _emit_command(
            session,
            command_type="inventory.restock_requested",
            routing_key="inventory.restock_requested.v1",
            org_id=org_id,
            order_id=order_id,
            data={
                "order_id": str(order_id),
                "org_id": str(org_id),
                "lines": [{"sku_id": str(line.sku_id), "qty": line.qty} for line in lines],
            },
        )
    async with transaction(session_factory) as session:
        await repository.guarded_transition(
            session, order_id, from_state="RESTOCKING", to_state="CANCELLED"
        )
    await _mark_order_cancelled(session_factory, order_id=order_id, reason="capture_failed")


# --- recovery worker steps (LLD-06 §5.3) ------------------------------------


#: These need a client retry to resume (only that request still carries
#: the payment_method_token, per ADR's privacy stance of never persisting
#: a raw payment token) -- recovery can't drive them forward, only
#: compensate once they're past deadline. See ``recover_one``'s docstring.
_NEEDS_CLIENT_RETRY = ("RESERVING", "RESERVED", "AUTHORIZING")


async def recover_one(
    session_factory: async_sessionmaker[AsyncSession], participants: Participants, *, order_id: UUID
) -> None:
    """LLD-06 §5.3's recovery worker step.

    What it can actually do without the original request in hand:
      - past the saga's deadline: compensate (release the reservation,
        or just cancel if nothing was ever reserved) and cancel the order.
      - ``COMMITTING``/``CAPTURING``: re-emit the command. Both are
        idempotent on the receiving side, so a redundant resend is
        harmless -- this is purely a hedge against an outbox row that
        was written but, for whatever reason, never got consumed.
      - anything else still within budget (``RESERVING``/``RESERVED``/
        ``AUTHORIZING``): orders-svc never persists the raw payment
        token, so only a client retry of the same checkout request can
        resume these (``_resume_checkout``, driven by the idempotency
        key, not this worker). There's nothing safe to do here but wait.
    """
    async with transaction(session_factory) as session:
        saga = await repository.lock_saga(session, order_id)
        if saga is None or saga.state in ("COMPLETED", "CANCELLED"):
            return
        order = await repository.lock_order(session, order_id)
        assert order is not None
        past_deadline = datetime.now(UTC) > saga.deadline_at
        state = saga.state
        saga.attempts += 1
        # The claim already leased next_action_at forward (repository.
        # CLAIM_LEASE_SECONDS); each branch below extends or clears it.
        saga.next_action_at = None

    if past_deadline and state in _NEEDS_CLIENT_RETRY + ("AUTH_PENDING",):
        if state == "RESERVING":
            async with transaction(session_factory) as session:
                transitioned = await repository.guarded_transition(
                    session, order_id, from_state="RESERVING", to_state="CANCELLED"
                )
            if transitioned:
                await _mark_order_cancelled(
                    session_factory, order_id=order_id, reason="payment_timeout"
                )
        else:
            await _release_reservation(
                session_factory,
                participants.inventory,
                order_id=order_id,
                org_id=order.org_id,
                from_state=state,
                reason="payment_timeout",
            )
        return

    if state == "COMMITTING":
        async with transaction(session_factory) as session:
            transitioned = await repository.guarded_transition(
                session,
                order_id,
                from_state="COMMITTING",
                to_state="COMMITTING",
                next_action_at=datetime.now(UTC) + timedelta(seconds=10),
            )
            if transitioned:
                await _emit_command(
                    session,
                    command_type="inventory.commit_requested",
                    routing_key="inventory.commit_requested.v1",
                    org_id=order.org_id,
                    order_id=order_id,
                    data={"order_id": str(order_id)},
                )
        return

    if state == "CAPTURING":
        async with transaction(session_factory) as session:
            transitioned = await repository.guarded_transition(
                session,
                order_id,
                from_state="CAPTURING",
                to_state="CAPTURING",
                next_action_at=datetime.now(UTC) + timedelta(seconds=10),
            )
            if transitioned:
                await _emit_command(
                    session,
                    command_type="payment.capture_requested",
                    routing_key="payment.capture_requested.v1",
                    org_id=order.org_id,
                    order_id=order_id,
                    data={"order_id": str(order_id), "org_id": str(order.org_id)},
                )
        return

    # RESERVING/RESERVED/AUTHORIZING/AUTH_PENDING, still within budget:
    # nothing to do yet but keep the lease moving so R1 can see this is
    # still "stuck, waiting on a client retry" rather than losing track
    # of it (claim_due_sagas requires next_action_at to be set).
    async with transaction(session_factory) as session:
        await repository.guarded_transition(
            session,
            order_id,
            from_state=state,
            to_state=state,
            next_action_at=datetime.now(UTC) + timedelta(seconds=10),
        )


# --- order admin ops (LLD-06 §6) --------------------------------------------

#: Allowed staff-driven forward edges. Cancellation and refund are their
#: own dedicated actions below, not part of this table.
ADMIN_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "confirmed": {"processing"},
    "processing": {"shipped"},
    "shipped": {"delivered"},
}


class OrderNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


class VersionConflict(Exception):
    pass


async def _emit_cancellation_commands(session: AsyncSession, order: Order) -> None:
    """LLD-06 §6: cancelling a confirmed/processing order always fires both
    compensating commands. Firing ``payment.refund_requested`` even when
    nothing was ever captured is safe -- payments-svc (doesn't exist yet,
    E3.6) is expected to no-op a refund request against an attempt that
    never reached ``captured``, the same way inventory-svc's own release
    is a no-op for an order with no reservation."""
    lines = await repository.list_order_lines(session, order.id)
    await _emit_command(
        session,
        command_type="inventory.restock_requested",
        routing_key="inventory.restock_requested.v1",
        org_id=order.org_id,
        order_id=order.id,
        data={
            "order_id": str(order.id),
            "org_id": str(order.org_id),
            "lines": [{"sku_id": str(line.sku_id), "qty": line.qty} for line in lines],
        },
    )
    await _emit_command(
        session,
        command_type="payment.refund_requested",
        routing_key="payment.refund_requested.v1",
        org_id=order.org_id,
        order_id=order.id,
        data={"order_id": str(order.id), "org_id": str(order.org_id)},
    )


async def customer_cancel_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: UUID,
    customer_id: UUID,
    idempotency_key: UUID,
) -> ActionResult:
    """LLD-06 §6: customer cancellation, only from ``confirmed``."""
    request_hash = idempotency.canonical_hash({"order_id": str(order_id)})

    async def run(_resource_id: UUID | None) -> ActionResult:
        async with transaction(session_factory) as session:
            order = await repository.get_order_for_customer(
                session, order_id, customer_id=customer_id
            )
            if order is None:
                return ActionResult(status_code=404, body={"title": "order-not-found"})
            order = await repository.lock_order(session, order_id)
            assert order is not None
            if order.status != "confirmed":
                return ActionResult(status_code=409, body={"title": "order-not-cancellable"})
            from_status = order.status
            repository.apply_order_transition(
                session,
                order,
                to_status="cancelled",
                actor_type="user",
                actor_id=str(customer_id),
                reason="customer_request",
                cancel_reason="customer_request",
            )
            await _emit_cancellation_commands(session, order)
            await _emit(
                session,
                event_type="order.cancelled",
                routing_key="order.cancelled.v1",
                exchange=_EVENTS_EXCHANGE,
                aggregate_type="order",
                aggregate_id=str(order_id),
                aggregate_version=order.version,
                org_id=order.org_id,
                data=_order_summary(order),
            )
            await _emit_status_changed(session, order, from_status=from_status)
            return ActionResult(
                status_code=200, body={"order_id": str(order_id), "status": "cancelled"}
            )

    return await _with_idempotency(
        session_factory,
        scope="order_cancel",
        principal_id=customer_id,
        key=idempotency_key,
        request_hash=request_hash,
        run=run,
    )


async def admin_transition_order(
    session: AsyncSession,
    order_id: UUID,
    *,
    org_id: UUID,
    actor_id: UUID,
    to_status: str,
    tracking_ref: str | None,
    expected_version: int,
) -> Order:
    order = await repository.lock_order(session, order_id)
    if order is None or order.org_id != org_id:
        raise OrderNotFound(order_id)
    if order.version != expected_version:
        raise VersionConflict(order_id)
    if to_status not in ADMIN_ALLOWED_TRANSITIONS.get(order.status, set()):
        raise InvalidTransition(f"{order.status} -> {to_status}")
    from_status = order.status
    repository.apply_order_transition(
        session,
        order,
        to_status=to_status,
        actor_type="user",
        actor_id=str(actor_id),
        reason=tracking_ref,
    )
    await _emit_status_changed(session, order, from_status=from_status)
    return order


async def admin_cancel_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: UUID,
    org_id: UUID,
    actor_id: UUID,
    idempotency_key: UUID,
    expected_version: int,
) -> ActionResult:
    """LLD-06 §6: staff cancellation, from ``confirmed`` or ``processing``."""
    request_hash = idempotency.canonical_hash(
        {"order_id": str(order_id), "expected_version": expected_version}
    )

    async def run(_resource_id: UUID | None) -> ActionResult:
        async with transaction(session_factory) as session:
            order = await repository.lock_order(session, order_id)
            if order is None or order.org_id != org_id:
                return ActionResult(status_code=404, body={"title": "order-not-found"})
            if order.version != expected_version:
                return ActionResult(
                    status_code=412, body={"title": "version conflict, refetch and retry"}
                )
            if order.status not in ("confirmed", "processing"):
                return ActionResult(status_code=409, body={"title": "order-not-cancellable"})
            from_status = order.status
            repository.apply_order_transition(
                session,
                order,
                to_status="cancelled",
                actor_type="user",
                actor_id=str(actor_id),
                reason="staff_request",
                cancel_reason="staff_request",
            )
            await _emit_cancellation_commands(session, order)
            await _emit(
                session,
                event_type="order.cancelled",
                routing_key="order.cancelled.v1",
                exchange=_EVENTS_EXCHANGE,
                aggregate_type="order",
                aggregate_id=str(order_id),
                aggregate_version=order.version,
                org_id=order.org_id,
                data=_order_summary(order),
            )
            await _emit_status_changed(session, order, from_status=from_status)
            return ActionResult(
                status_code=200, body={"order_id": str(order_id), "status": "cancelled"}
            )

    return await _with_idempotency(
        session_factory,
        scope="order_cancel",
        principal_id=actor_id,
        key=idempotency_key,
        request_hash=request_hash,
        run=run,
    )


async def admin_refund_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: UUID,
    org_id: UUID,
    actor_id: UUID,
    idempotency_key: UUID,
) -> ActionResult:
    """LLD-06 §6: "Full refund in v1". Fires the refund command and moves
    ``payment_status`` to ``refund_pending`` -- ``refunded`` lands once
    payments-svc (doesn't exist yet) publishes ``payment.refunded.v1``."""
    request_hash = idempotency.canonical_hash({"order_id": str(order_id)})

    async def run(_resource_id: UUID | None) -> ActionResult:
        async with transaction(session_factory) as session:
            order = await repository.lock_order(session, order_id)
            if order is None or order.org_id != org_id:
                return ActionResult(status_code=404, body={"title": "order-not-found"})
            if order.status != "delivered":
                return ActionResult(status_code=409, body={"title": "order-not-refundable"})
            repository.set_payment_status(order, "refunding")
            await _emit_command(
                session,
                command_type="payment.refund_requested",
                routing_key="payment.refund_requested.v1",
                org_id=order.org_id,
                order_id=order.id,
                data={"order_id": str(order.id), "org_id": str(order.org_id)},
            )
            return ActionResult(
                status_code=202, body={"order_id": str(order_id), "payment_status": "refunding"}
            )

    return await _with_idempotency(
        session_factory,
        scope="refund",
        principal_id=actor_id,
        key=idempotency_key,
        request_hash=request_hash,
        run=run,
    )
