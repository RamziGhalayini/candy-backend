from datetime import date

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


def test_check_in_hides_stop_when_candy_count_reaches_zero(client):
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
    assert refreshed.is_hidden is True


def test_check_in_without_candy_count_never_hides_stop(client):
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
