from datetime import date, timedelta

from app import CheckIn, Household, Stop, db


def _make_verified_stop(client, device_id="registrant-1", lat=42.0, lon=-71.0, name="Original House"):
    original_resp = client.post(
        "/register-stop",
        json={
            "name": name,
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
            "device_id": f"confirmer-{original_id}",
        },
    )

    return db.session.get(Stop, original_id)


def _check_in(client, device_id, stop):
    return client.post(
        "/check-in",
        json={
            "device_id": device_id,
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        },
    )


def test_night_ledger_no_checkins_returns_zeros(client):
    response = client.get("/night-ledger/never-checked-in")

    assert response.status_code == 200
    assert response.get_json() == {
        "device_id": "never-checked-in",
        "date": date.today().isoformat(),
        "total_checkins": 0,
        "verified_stops_checked_in": 0,
        "candy_available_count": 0,
        "greetings_encountered": 0,
        "greetings_heard": 0,
        "greetings_unlocked": False,
    }


def test_night_ledger_counts_todays_checkins_and_candy(client):
    stop_a = _make_verified_stop(client, device_id="reg-a", lat=42.0, lon=-71.0, name="House A")
    stop_b = _make_verified_stop(client, device_id="reg-b", lat=43.0, lon=-71.0, name="House B")
    stop_b.candy_available = False
    db.session.commit()

    _check_in(client, "walker-1", stop_a)
    _check_in(client, "walker-1", stop_b)

    response = client.get("/night-ledger/walker-1")

    assert response.status_code == 200
    body = response.get_json()
    assert body["total_checkins"] == 2
    assert body["verified_stops_checked_in"] == 2
    assert body["candy_available_count"] == 1


def test_night_ledger_locked_household_shows_zero_heard(client):
    stop = _make_verified_stop(client, lat=42.0, lon=-71.0)
    stop.greeting_audio_url = "https://example.com/greeting.m4a"
    db.session.commit()

    _check_in(client, "walker-2", stop)

    response = client.get("/night-ledger/walker-2")

    assert response.status_code == 200
    body = response.get_json()
    assert body["greetings_unlocked"] is False
    assert body["greetings_encountered"] == 1
    assert body["greetings_heard"] == 0


def test_night_ledger_unlocked_household_shows_all_heard(client):
    stop_a = _make_verified_stop(client, device_id="reg-a", lat=42.0, lon=-71.0, name="House A")
    stop_a.greeting_audio_url = "https://example.com/a.m4a"
    stop_b = _make_verified_stop(client, device_id="reg-b", lat=43.0, lon=-71.0, name="House B")
    stop_b.greeting_audio_url = "https://example.com/b.m4a"
    db.session.commit()

    household = Household(device_id="walker-3", points=0, greetings_unlocked=True)
    db.session.add(household)
    db.session.commit()

    _check_in(client, "walker-3", stop_a)
    _check_in(client, "walker-3", stop_b)

    response = client.get("/night-ledger/walker-3")

    assert response.status_code == 200
    body = response.get_json()
    assert body["greetings_unlocked"] is True
    assert body["greetings_encountered"] == 2
    assert body["greetings_heard"] == 2


def test_night_ledger_ignores_stops_without_greetings(client):
    stop = _make_verified_stop(client, lat=42.0, lon=-71.0)
    assert stop.greeting_audio_url is None

    household = Household(device_id="walker-4", points=0, greetings_unlocked=True)
    db.session.add(household)
    db.session.commit()

    _check_in(client, "walker-4", stop)

    response = client.get("/night-ledger/walker-4")

    body = response.get_json()
    assert body["greetings_encountered"] == 0
    assert body["greetings_heard"] == 0


def test_night_ledger_excludes_checkins_from_a_previous_day(client):
    stop = _make_verified_stop(client, lat=42.0, lon=-71.0)

    db.session.add(
        CheckIn(device_id="walker-5", stop_id=stop.id, check_in_date=date.today() - timedelta(days=1))
    )
    db.session.commit()

    response = client.get("/night-ledger/walker-5")

    assert response.status_code == 200
    assert response.get_json()["total_checkins"] == 0
