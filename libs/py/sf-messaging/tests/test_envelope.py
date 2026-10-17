from sf_messaging import build_envelope, envelope_id


def test_builds_a_well_formed_cloudevents_envelope() -> None:
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={"user_id": "user-123", "status": "active"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    assert envelope["specversion"] == "1.0"
    assert envelope["type"] == "user.created"
    assert envelope["source"] == "users-svc"
    assert envelope["subject"] == "user-123"
    assert envelope["sfaggregatetype"] == "user"
    assert envelope["sfaggregateversion"] == 1
    assert envelope["data"] == {"user_id": "user-123", "status": "active"}
    assert envelope["time"].endswith("Z")
    assert envelope_id(envelope) is not None


def test_each_call_gets_a_fresh_id() -> None:
    kwargs = dict(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    first = build_envelope(**kwargs)
    second = build_envelope(**kwargs)

    assert first["id"] != second["id"]
