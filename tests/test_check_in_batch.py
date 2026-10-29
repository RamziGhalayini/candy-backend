"""No wall clock in this file. Every timestamp a test sends, and every
calendar day it expects the server to file a check-in under, comes from the
`frozen_clock` fixture in conftest.py, which also pins app.py's date.today().
That fixture runs each test at two instants, one before and one after the
20:00 US-Eastern rollover where the UTC day and the Eastern day diverge."""

from app import CheckIn, Stop, db


def _make_verified_stop(client, device_id="registrant-1", lat=42.0, lon=-71.0, name="Original House"):
    """Same pattern as test_rewards.py's own helper -- two registrations at
    the same spot via the real endpoint, so the verification-bonus side
    effect fires exactly like production."""
    original_resp = client.post(
        "/register-stop",
        json={"name": name, "type": "house", "latitude": lat, "longitude": lon, "device_id": device_id},
    )
    original_id = original_resp.get_json()["id"]

    client.post(
        "/register-stop",
        json={
            "name": f"Confirming {name}",
            "type": "house",
            "latitude": lat,
            "longitude": lon,
            "device_id": "registrant-2",
        },
    )

    return db.session.get(Stop, original_id)


def test_batch_accepts_multiple_valid_checkins(client, frozen_clock):
    stop_a = _make_verified_stop(client, lat=42.0, lon=-71.0, name="House A")
    stop_b = _make_verified_stop(client, lat=43.0, lon=-72.0, name="House B")

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop_a.id,
                    "latitude": stop_a.latitude,
                    "longitude": stop_a.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
                {
                    "stop_id": stop_b.id,
                    "latitude": stop_b.latitude,
                    "longitude": stop_b.longitude,
                    "checked_in_at": frozen_clock.iso(minutes=5),
                },
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert len(results) == 2
    assert all(r["status"] == "accepted" for r in results)
    assert results[0]["points_awarded"] == 5
    assert results[1]["points_total"] == 10
    assert CheckIn.query.filter_by(device_id="offline-device").count() == 2


def test_batch_rejects_duplicate_stop_id_within_same_batch(client, frozen_clock):
    stop = _make_verified_stop(client)

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(minutes=1),
                },
            ],
        },
    )

    assert response.status_code == 400
    assert CheckIn.query.filter_by(device_id="offline-device").count() == 0


def test_batch_rejects_non_chronological_order(client, frozen_clock):
    stop_a = _make_verified_stop(client, lat=42.0, lon=-71.0, name="House A")
    stop_b = _make_verified_stop(client, lat=43.0, lon=-72.0, name="House B")

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop_a.id,
                    "latitude": stop_a.latitude,
                    "longitude": stop_a.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
                {
                    "stop_id": stop_b.id,
                    "latitude": stop_b.latitude,
                    "longitude": stop_b.longitude,
                    "checked_in_at": frozen_clock.iso(minutes=-5),
                },
            ],
        },
    )

    assert response.status_code == 400
    assert CheckIn.query.filter_by(device_id="offline-device").count() == 0


def test_batch_rejects_more_than_max_items(client, frozen_clock):
    stop = _make_verified_stop(client)
    # One over the cap -- every item shares the SAME stop_id, which would
    # also trip the duplicate check, but the size cap should reject first
    # since it's the more fundamental "is this even a sane request" bound.
    checkins = [
        {
            "stop_id": stop.id + i,  # distinct ids -- isolates the size check from the duplicate check
            "latitude": stop.latitude,
            "longitude": stop.longitude,
            "checked_in_at": frozen_clock.iso(minutes=i),
        }
        for i in range(51)
    ]

    response = client.post("/check-in/batch", json={"device_id": "offline-device", "checkins": checkins})

    assert response.status_code == 400


def test_batch_requires_at_least_one_checkin(client):
    response = client.post("/check-in/batch", json={"device_id": "offline-device", "checkins": []})

    assert response.status_code == 400


def test_batch_rejects_missing_device_id(client):
    response = client.post("/check-in/batch", json={"checkins": []})

    assert response.status_code == 400


def test_batch_is_a_no_op_for_a_checkin_already_recorded_live(client, frozen_clock):
    """The exact scenario the plan called out: a check-in that already
    succeeded via the real-time path shouldn't be double-counted just
    because a stale offline queue still has it too.

    The live path files the row under the server's date.today(); the batch
    path files it under checked_in_at.date(). With both clocks frozen to the
    same instant the two dates agree on either side of the Eastern 20:00
    rollover, which is the property this test exists to pin."""
    stop = _make_verified_stop(client)

    live = client.post(
        "/check-in",
        json={"device_id": "offline-device", "stop_id": stop.id, "latitude": stop.latitude, "longitude": stop.longitude},
    )
    assert live.status_code == 200
    assert live.get_json()["points_total"] == 5

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(),
                }
            ],
        },
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "already_recorded"
    assert CheckIn.query.filter_by(device_id="offline-device", stop_id=stop.id).count() == 1

    from app import Household

    household = Household.query.filter_by(device_id="offline-device").first()
    assert household.points == 5  # unchanged -- not re-awarded


def test_batch_is_a_no_op_when_the_same_item_is_resynced_in_a_later_batch(client, frozen_clock):
    """Simulates a partial-sync retry: the same queued item gets submitted
    in two separate batch calls (e.g. the first sync succeeded server-side
    but the client never got the response before losing connection
    again, so it retries the whole queue next time)."""
    stop = _make_verified_stop(client)
    payload = {
        "device_id": "offline-device",
        "checkins": [
            {
                "stop_id": stop.id,
                "latitude": stop.latitude,
                "longitude": stop.longitude,
                "checked_in_at": frozen_clock.iso(),
            }
        ],
    }

    first = client.post("/check-in/batch", json=payload)
    second = client.post("/check-in/batch", json=payload)

    assert first.get_json()["results"][0]["status"] == "accepted"
    assert second.get_json()["results"][0]["status"] == "already_recorded"
    assert CheckIn.query.filter_by(device_id="offline-device", stop_id=stop.id).count() == 1

    from app import Household

    household = Household.query.filter_by(device_id="offline-device").first()
    assert household.points == 5


def test_batch_reports_stop_unavailable_distinctly_from_stop_not_found(client, frozen_clock):
    """A stop hidden by reports between the offline tap and the eventual sync
    should report a DIFFERENT reason than a stop that genuinely never existed,
    so the client can say something specific rather than a generic error.

    NOTE: stop_unavailable no longer covers "the house ran out" -- running out
    sets candy_available = False and the check-in is accepted. See
    test_batch_accepts_a_queued_check_in_at_a_stop_that_ran_out_later."""
    stop = _make_verified_stop(client)
    stop.is_hidden = True
    db.session.commit()

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
            ],
        },
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "rejected"
    assert result["reason"] == "stop_unavailable"

    nonexistent_response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id + 99999,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
            ],
        },
    )
    nonexistent_result = nonexistent_response.get_json()["results"][0]
    assert nonexistent_result["reason"] == "stop_not_found"
    assert nonexistent_result["reason"] != result["reason"]


def test_batch_accepts_a_queued_check_in_at_a_stop_that_ran_out_later(client, frozen_clock):
    """The offline case this change exists for: a kid taps check-in at a house
    that still has candy, goes out of signal, and the house runs out before the
    queue syncs. That check-in must still be credited -- the visit really
    happened -- so running out must not reject it."""
    stop = _make_verified_stop(client)
    stop.candy_count = 0
    stop.candy_available = False
    db.session.commit()

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": frozen_clock.iso(),
                },
            ],
        },
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "accepted"

    from app import Household

    household = Household.query.filter_by(device_id="offline-device").first()
    assert household.points == 5


def test_batch_records_checkin_under_the_captured_date_not_todays_date(client, frozen_clock):
    """A check-in captured just before midnight and synced after it should
    still count for the night it actually happened, not the day it
    happened to reach the server.

    "Yesterday" is one day before the frozen instant, so its UTC calendar
    day is one before the server's frozen today by construction -- the
    comparison no longer depends on the wall clock of the machine running
    the suite."""
    stop = _make_verified_stop(client)
    yesterday = frozen_clock.utc(days=-1)

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": "offline-device",
            "checkins": [
                {
                    "stop_id": stop.id,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "checked_in_at": yesterday.isoformat(),
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.get_json()["results"][0]["status"] == "accepted"

    recorded = CheckIn.query.filter_by(device_id="offline-device", stop_id=stop.id).first()
    assert recorded.check_in_date == yesterday.date()
    assert recorded.check_in_date != frozen_clock.today
