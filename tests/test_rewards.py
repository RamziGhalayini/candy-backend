from datetime import date, datetime, timezone

from app import REWARDS_CATALOG, TRIVIA_QUESTIONS, Household, Redemption, Stop, db


def _todays_question():
    today = date.today()
    return TRIVIA_QUESTIONS[today.toordinal() % len(TRIVIA_QUESTIONS)]


def _make_verified_stop(client, device_id="registrant-1", lat=42.0, lon=-71.0):
    """Registers two stops at the same spot (via the real endpoint, so the
    verification-bonus side effect fires exactly like production) and
    returns the original (first) stop."""
    original_resp = client.post(
        "/register-stop",
        json={
            "name": "Original House",
            "type": "house",
            "latitude": lat,
            "longitude": lon,
            "device_id": device_id,
        },
    )
    original_id = original_resp.get_json()["id"]

    client.post(
        "/register-stop",
        json={
            "name": "Confirming House",
            "type": "house",
            "latitude": lat,
            "longitude": lon,
            "device_id": "registrant-2",
        },
    )

    return db.session.get(Stop, original_id)


# ── Check-in ──────────────────────────────────────────────────────────────


def test_check_in_awards_points_at_verified_stop_within_range(client):
    stop = _make_verified_stop(client)

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"points_awarded": 5, "points_total": 5}


def test_check_in_rejects_when_too_far(client):
    stop = _make_verified_stop(client)

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude + 1.0,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 400
    assert "far" in response.get_json()["error"]


def test_check_in_rejects_unverified_stop(client):
    register_resp = client.post(
        "/register-stop",
        json={
            "name": "Lonely House",
            "type": "house",
            "latitude": 42.0,
            "longitude": -71.0,
            "device_id": "solo-registrant",
        },
    )
    stop = register_resp.get_json()

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop["id"],
            "latitude": stop["latitude"],
            "longitude": stop["longitude"],
        },
    )

    assert response.status_code == 400
    assert "verified" in response.get_json()["error"]


def test_check_in_rejects_second_time_same_day(client):
    stop = _make_verified_stop(client)
    payload = {
        "device_id": "checker-1",
        "stop_id": stop.id,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
    }

    first = client.post("/check-in", json=payload)
    second = client.post("/check-in", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already checked in" in second.get_json()["error"]


# ── Verification bonus ───────────────────────────────────────────────────


def test_verification_bonus_awarded_once_to_original_registrant(client):
    original = _make_verified_stop(client, device_id="original-device")

    household = Household.query.filter_by(device_id="original-device").first()
    assert household is not None
    assert household.points == 10

    # A third matching stop registering shouldn't pay the bonus again.
    client.post(
        "/register-stop",
        json={
            "name": "Third House",
            "type": "house",
            "latitude": original.latitude,
            "longitude": original.longitude,
            "device_id": "third-device",
        },
    )

    household = Household.query.filter_by(device_id="original-device").first()
    assert household.points == 10


# ── Trivia ────────────────────────────────────────────────────────────────


def test_trivia_correct_answer_awards_points_once(client):
    question = _todays_question()

    response = client.post(
        "/trivia/answer",
        json={"device_id": "trivia-1", "answer": question["correct_index"]},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body == {"correct": True, "points_awarded": 10, "points_total": 10}


def test_trivia_incorrect_answer_awards_no_points(client):
    question = _todays_question()
    wrong_index = (question["correct_index"] + 1) % len(question["choices"])

    response = client.post(
        "/trivia/answer",
        json={"device_id": "trivia-1", "answer": wrong_index},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body == {"correct": False, "points_awarded": 0, "points_total": 0}


def test_trivia_second_answer_same_day_rejected(client):
    question = _todays_question()
    payload = {"device_id": "trivia-1", "answer": question["correct_index"]}

    first = client.post("/trivia/answer", json=payload)
    second = client.post("/trivia/answer", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already answered" in second.get_json()["error"]


# ── Redemption ────────────────────────────────────────────────────────────


def test_redeem_reward_deducts_points_and_returns_code(client):
    household = Household(device_id="redeemer-1", points=100)
    db.session.add(household)
    db.session.commit()

    reward = REWARDS_CATALOG[0]

    response = client.post(
        f"/redeem-reward/{reward['id']}",
        json={"device_id": "redeemer-1"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["points_remaining"] == 100 - reward["points_cost"]
    assert body["code"].startswith("TM-")

    redemption = Redemption.query.filter_by(device_id="redeemer-1").first()
    assert redemption is not None
    assert redemption.code == body["code"]
    assert redemption.points_spent == reward["points_cost"]


def test_redeem_reward_insufficient_points_rejected(client):
    household = Household(device_id="poor-1", points=1)
    db.session.add(household)
    db.session.commit()

    reward = REWARDS_CATALOG[0]

    response = client.post(
        f"/redeem-reward/{reward['id']}",
        json={"device_id": "poor-1"},
    )

    assert response.status_code == 400
    assert "enough points" in response.get_json()["error"]


def test_redeem_reward_unknown_id_returns_404(client):
    household = Household(device_id="redeemer-2", points=1000)
    db.session.add(household)
    db.session.commit()

    response = client.post(
        "/redeem-reward/not-a-real-reward",
        json={"device_id": "redeemer-2"},
    )

    assert response.status_code == 404


def test_redeem_reward_rejects_second_redemption_of_same_reward(client):
    household = Household(device_id="repeat-redeemer", points=1000)
    db.session.add(household)
    db.session.commit()

    reward = REWARDS_CATALOG[0]
    payload = {"device_id": "repeat-redeemer"}

    first = client.post(f"/redeem-reward/{reward['id']}", json=payload)
    second = client.post(f"/redeem-reward/{reward['id']}", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already redeemed" in second.get_json()["error"]

    # Points were only spent once.
    assert db.session.get(Household, household.id).points == 1000 - reward["points_cost"]
    assert Redemption.query.filter_by(device_id="repeat-redeemer", reward_id=reward["id"]).count() == 1


def test_redeem_reward_allows_a_different_reward_after_first(client):
    household = Household(device_id="multi-redeemer", points=1000)
    db.session.add(household)
    db.session.commit()

    first_reward, second_reward = REWARDS_CATALOG[0], REWARDS_CATALOG[1]

    first = client.post(f"/redeem-reward/{first_reward['id']}", json={"device_id": "multi-redeemer"})
    second = client.post(f"/redeem-reward/{second_reward['id']}", json={"device_id": "multi-redeemer"})

    assert first.status_code == 200
    assert second.status_code == 200


def test_rewards_catalog_returns_seeded_list(client):
    response = client.get("/rewards-catalog")

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == len(REWARDS_CATALOG)
    expected_keys = {"id", "name", "points_cost", "sponsor_name", "sponsor_type", "image_url"}
    assert expected_keys <= set(body[0].keys())


def test_points_lookup_defaults_to_zero_without_creating_household(client):
    response = client.get("/points/never-seen-device")

    assert response.status_code == 200
    assert response.get_json() == {
        "device_id": "never-seen-device",
        "points": 0,
        "greetings_unlocked": False,
    }
    assert Household.query.filter_by(device_id="never-seen-device").first() is None


# ── Candy count ───────────────────────────────────────────────────────────


def test_register_stop_accepts_optional_candy_count(client):
    response = client.post(
        "/register-stop",
        json={
            "name": "Stocked House",
            "type": "house",
            "latitude": 42.0,
            "longitude": -71.0,
            "device_id": "registrant-1",
            "candy_count": 3,
        },
    )

    assert response.status_code == 201
    assert response.get_json()["candy_count"] == 3


def test_register_stop_without_candy_count_defaults_to_null(client):
    response = client.post(
        "/register-stop",
        json={
            "name": "Unstocked House",
            "type": "house",
            "latitude": 42.0,
            "longitude": -71.0,
            "device_id": "registrant-1",
        },
    )

    assert response.status_code == 201
    assert response.get_json()["candy_count"] is None


def test_register_stop_rejects_negative_candy_count(client):
    response = client.post(
        "/register-stop",
        json={
            "name": "Bad House",
            "type": "house",
            "latitude": 42.0,
            "longitude": -71.0,
            "candy_count": -1,
        },
    )

    assert response.status_code == 400
    assert "candy_count" in response.get_json()["error"]


def test_check_in_decrements_candy_count(client):
    stop = _make_verified_stop(client)
    stop.candy_count = 2
    db.session.commit()

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 200
    refreshed = db.session.get(Stop, stop.id)
    assert refreshed.candy_count == 1
    assert refreshed.is_hidden is False
    assert refreshed.candy_available is True


def test_check_in_marks_stop_unavailable_when_candy_count_reaches_zero(client):
    """Running out flips candy_available, NOT is_hidden. is_hidden is terminal
    and means "removed by reports" only."""
    stop = _make_verified_stop(client)
    stop.candy_count = 1
    db.session.commit()

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 200
    refreshed = db.session.get(Stop, stop.id)
    assert refreshed.candy_count == 0
    assert refreshed.candy_available is False
    assert refreshed.is_hidden is False


def test_stop_that_ran_out_still_appears_in_nearby_stops(client):
    """The whole point of not hiding: the household stays on the map, just
    without the live glow."""
    stop = _make_verified_stop(client)
    stop.candy_count = 1
    db.session.commit()

    client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    nearby = client.get(f"/nearby-stops?lat={stop.latitude}&lon={stop.longitude}").get_json()
    listed = [s for s in nearby if s["id"] == stop.id]
    assert len(listed) == 1
    assert listed[0]["candy_available"] is False
    assert listed[0]["is_hidden"] is False


def test_check_in_still_accepted_at_a_stop_that_already_ran_out(client):
    """A kid who walks up to an empty house still gets credit."""
    stop = _make_verified_stop(client)
    stop.candy_count = 0
    stop.candy_available = False
    db.session.commit()

    response = client.post(
        "/check-in",
        json={
            "device_id": "late-arrival",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["points_awarded"] > 0

    refreshed = db.session.get(Stop, stop.id)
    assert refreshed.candy_count == 0  # floors, never goes negative
    assert refreshed.is_hidden is False


def test_owner_can_flip_candy_available_back_after_running_out(client):
    """The recovery path that is_hidden never had -- PATCH /update-stop, which
    already exists and is already ownership-gated."""
    stop = _make_verified_stop(client)
    stop.candy_count = 1
    db.session.commit()

    client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )
    assert db.session.get(Stop, stop.id).candy_available is False

    response = client.patch(
        f"/update-stop/{stop.id}",
        json={"device_id": "registrant-1", "candy_available": True, "candy_count": 10},
    )

    assert response.status_code == 200
    refreshed = db.session.get(Stop, stop.id)
    assert refreshed.candy_available is True
    assert refreshed.candy_count == 10


def test_running_out_does_not_reject_a_stop_hidden_by_reports_differently(client):
    """is_hidden still rejects check-ins. Running out must not have quietly
    weakened the moderation guard."""
    stop = _make_verified_stop(client)
    stop.is_hidden = True
    db.session.commit()

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 404


def test_check_in_without_candy_count_never_changes_availability(client):
    stop = _make_verified_stop(client)
    assert stop.candy_count is None

    response = client.post(
        "/check-in",
        json={
            "device_id": "checker-1",
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )

    assert response.status_code == 200
    refreshed = db.session.get(Stop, stop.id)
    assert refreshed.candy_count is None
    assert refreshed.is_hidden is False
    assert refreshed.candy_available is True


# ── Check-in milestone ───────────────────────────────────────────────────


def test_check_in_includes_milestone_on_twentieth_checkin(client):
    device_id = "milestone-chaser"

    for i in range(20):
        stop = _make_verified_stop(client, device_id=f"registrant-{i}", lat=42.0 + i * 0.01, lon=-71.0)
        response = client.post(
            "/check-in",
            json={
                "device_id": device_id,
                "stop_id": stop.id,
                "latitude": stop.latitude,
                "longitude": stop.longitude,
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        if i < 19:
            assert "milestone" not in body
        else:
            assert body["milestone"] is True
            assert "20" in body["message"]


# ── Redemption history ───────────────────────────────────────────────────


def test_get_redemptions_returns_empty_list_for_new_device(client):
    response = client.get("/redemptions/never-redeemed")

    assert response.status_code == 200
    assert response.get_json() == []


def test_get_redemptions_returns_history_with_expected_fields(client):
    household = Household(device_id="history-1", points=1000)
    db.session.add(household)
    db.session.commit()

    reward = REWARDS_CATALOG[0]
    redeem_response = client.post(f"/redeem-reward/{reward['id']}", json={"device_id": "history-1"})
    code = redeem_response.get_json()["code"]

    response = client.get("/redemptions/history-1")

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == 1
    entry = body[0]
    assert entry["reward_id"] == reward["id"]
    assert entry["reward_name"] == reward["name"]
    assert entry["sponsor_name"] == reward["sponsor_name"]
    assert entry["points_spent"] == reward["points_cost"]
    assert entry["code"] == code
    assert "created_at" in entry


def test_get_redemptions_sorted_most_recent_first(client):
    household = Household(device_id="history-2", points=1000)
    db.session.add(household)
    db.session.commit()

    first_reward, second_reward = REWARDS_CATALOG[0], REWARDS_CATALOG[1]
    client.post(f"/redeem-reward/{first_reward['id']}", json={"device_id": "history-2"})
    client.post(f"/redeem-reward/{second_reward['id']}", json={"device_id": "history-2"})

    response = client.get("/redemptions/history-2")

    body = response.get_json()
    assert len(body) == 2
    assert body[0]["reward_id"] == second_reward["id"]
    assert body[1]["reward_id"] == first_reward["id"]


def test_get_redemptions_orders_by_id_when_created_at_ties(client):
    """The deterministic version of the ordering guarantee above.

    created_at is stamped from the clock at insert, so two redemptions made
    back to back can carry the SAME timestamp -- which is exactly how the test
    above used to fail intermittently. Rather than race the clock and hope,
    this forces an exact tie and pins the tiebreaker: with equal timestamps the
    newer row (higher id) must still come first.

    Without the id.desc() secondary sort this assertion is not merely flaky,
    it is undefined -- SQL may return tied rows in any order."""
    household = Household(device_id="tie-device", points=1000)
    db.session.add(household)
    db.session.commit()

    same_moment = datetime(2026, 10, 31, 20, 0, 0, tzinfo=timezone.utc)

    older = Redemption(
        device_id="tie-device",
        reward_id="tie-older",
        points_spent=10,
        code="TIE-CODE-OLDER",
        created_at=same_moment,
    )
    db.session.add(older)
    db.session.commit()

    newer = Redemption(
        device_id="tie-device",
        reward_id="tie-newer",
        points_spent=10,
        code="TIE-CODE-NEWER",
        created_at=same_moment,
    )
    db.session.add(newer)
    db.session.commit()

    # Precondition: the rows really do tie on created_at, and id really is the
    # only thing that distinguishes their insertion order.
    assert older.created_at == newer.created_at
    assert older.id < newer.id

    body = client.get("/redemptions/tie-device").get_json()

    assert [entry["reward_id"] for entry in body] == ["tie-newer", "tie-older"]


def test_get_redemptions_still_sorts_by_created_at_when_timestamps_differ(client):
    """The tiebreaker must not become the primary sort: an older row inserted
    later (higher id) must still sort BELOW a newer row with an earlier id."""
    household = Household(device_id="order-device", points=1000)
    db.session.add(household)
    db.session.commit()

    # Inserted first (lower id) but timestamped LATER -- so correct output puts
    # it first, which only holds if created_at still dominates id.
    recent = Redemption(
        device_id="order-device",
        reward_id="order-recent",
        points_spent=10,
        code="ORDER-CODE-RECENT",
        created_at=datetime(2026, 10, 31, 22, 0, 0, tzinfo=timezone.utc),
    )
    db.session.add(recent)
    db.session.commit()

    ancient = Redemption(
        device_id="order-device",
        reward_id="order-ancient",
        points_spent=10,
        code="ORDER-CODE-ANCIENT",
        created_at=datetime(2026, 10, 31, 18, 0, 0, tzinfo=timezone.utc),
    )
    db.session.add(ancient)
    db.session.commit()

    assert recent.id < ancient.id

    body = client.get("/redemptions/order-device").get_json()

    assert [entry["reward_id"] for entry in body] == ["order-recent", "order-ancient"]


def test_get_redemptions_only_returns_that_devices_history(client):
    household_a = Household(device_id="history-a", points=1000)
    household_b = Household(device_id="history-b", points=1000)
    db.session.add_all([household_a, household_b])
    db.session.commit()

    reward = REWARDS_CATALOG[0]
    client.post(f"/redeem-reward/{reward['id']}", json={"device_id": "history-a"})

    response = client.get("/redemptions/history-b")

    assert response.status_code == 200
    assert response.get_json() == []
