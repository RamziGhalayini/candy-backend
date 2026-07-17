from app import Stop, db


def _register_stop(client, **overrides):
    payload = {
        "name": "My House",
        "type": "house",
        "latitude": 42.0,
        "longitude": -71.0,
        "device_id": "owner-1",
    }
    payload.update(overrides)
    response = client.post("/register-stop", json=payload)
    return response.get_json()


def test_my_stops_returns_all_stops_for_device(client):
    _register_stop(client, name="First House")
    _register_stop(client, name="Second House")
    _register_stop(client, name="Someone Else's House", device_id="other-device")

    response = client.get("/my-stops/owner-1")

    assert response.status_code == 200
    body = response.get_json()
    assert {stop["name"] for stop in body} == {"First House", "Second House"}


def test_my_stops_returns_empty_list_for_unknown_device(client):
    response = client.get("/my-stops/no-such-device")

    assert response.status_code == 200
    assert response.get_json() == []


def test_update_stop_updates_candy_count(client):
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_count": 42},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["candy_count"] == 42

    stored = db.session.get(Stop, stop["id"])
    assert stored.candy_count == 42


def test_update_stop_updates_candy_available(client):
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_available": False},
    )

    assert response.status_code == 200
    assert response.get_json()["candy_available"] is False


def test_update_stop_rejects_wrong_device_id(client):
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "not-the-owner", "candy_count": 5},
    )

    assert response.status_code == 403
    stored = db.session.get(Stop, stop["id"])
    assert stored.candy_count is None


def test_update_stop_requires_device_id(client):
    stop = _register_stop(client)

    response = client.patch(f"/update-stop/{stop['id']}", json={"candy_count": 5})

    assert response.status_code == 400


def test_update_stop_rejects_negative_candy_count(client):
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_count": -3},
    )

    assert response.status_code == 400
    assert "candy_count" in response.get_json()["error"]


def test_update_stop_rejects_non_boolean_candy_available(client):
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_available": "yes"},
    )

    assert response.status_code == 400
    assert "candy_available" in response.get_json()["error"]


def test_update_stop_missing_stop_returns_404(client):
    response = client.patch(
        "/update-stop/999999",
        json={"device_id": "owner-1", "candy_count": 5},
    )

    assert response.status_code == 404


def test_update_stop_allows_clearing_candy_count(client):
    stop = _register_stop(client, candy_count=10)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_count": None},
    )

    assert response.status_code == 200
    assert response.get_json()["candy_count"] is None
