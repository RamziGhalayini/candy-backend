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


# ---------------------------------------------------------------------------
# POST /my-stops (2026-10-25). GET /my-stops/<device_id> puts the device id
# into the gunicorn access line in Render's app log. The POST form carries it
# in the JSON body instead and returns the identical list. GET stays as it is
# for the installed dev build.
# ---------------------------------------------------------------------------


def test_my_stops_post_returns_the_same_list_as_get(client):
    _register_stop(client, name="First House")
    _register_stop(client, name="Second House", latitude=42.5)
    _register_stop(client, name="Someone Else's House", device_id="other-device")

    via_get = client.get("/my-stops/owner-1")
    via_post = client.post("/my-stops", json={"device_id": "owner-1"})

    assert via_get.status_code == 200
    assert via_post.status_code == 200
    assert via_post.get_json() == via_get.get_json()
    assert {stop["name"] for stop in via_post.get_json()} == {"First House", "Second House"}


def test_my_stops_post_carries_the_verified_flag_like_get(client):
    """The helper both routes share is the one that resolves verification in
    a single query; a neighbour's twin registration verifies the owner's."""
    _register_stop(client, name="Mine", latitude=42.0, longitude=-71.0)
    _register_stop(client, name="Neighbour", latitude=42.0001, longitude=-71.0, device_id="other-device")

    via_get = client.get("/my-stops/owner-1").get_json()
    via_post = client.post("/my-stops", json={"device_id": "owner-1"}).get_json()

    assert via_get == via_post
    assert via_post[0]["verified"] is True


def test_my_stops_post_returns_empty_list_for_unknown_device(client):
    response = client.post("/my-stops", json={"device_id": "no-such-device"})

    assert response.status_code == 200
    assert response.get_json() == []


def test_my_stops_post_requires_a_device_id(client):
    """The GET form cannot be called without an id (the path needs one); the
    POST form refuses the same way /register-stop does, with its message."""
    for body in ({}, {"device_id": ""}, {"device_id": "   "}, {"device_id": None}, {"device_id": 123}):
        response = client.post("/my-stops", json=body)

        assert response.status_code == 400, body
        assert response.get_json() == {"error": "device_id is required"}, body


def test_my_stops_post_without_a_json_object_is_a_missing_device_id(client):
    no_body = client.post("/my-stops")
    a_list = client.post("/my-stops", json=["owner-1"])

    for response in (no_body, a_list):
        assert response.status_code == 400
        assert response.get_json() == {"error": "device_id is required"}


def test_my_stops_post_does_not_read_the_device_id_from_the_url(client):
    """The device id has no business in the URL on this route -- that is the
    reason the route exists."""
    _register_stop(client, name="First House")

    response = client.post("/my-stops?device_id=owner-1", json={})

    assert response.status_code == 400


def test_my_stops_get_path_is_unchanged(client):
    """Pins the installed dev build's call: path form, no body, same list."""
    _register_stop(client, name="First House")

    response = client.get("/my-stops/owner-1")

    assert response.status_code == 200
    assert [stop["name"] for stop in response.get_json()] == ["First House"]
    assert client.post("/my-stops/owner-1").status_code == 405, "the path form is GET-only"


def test_both_my_stops_forms_sit_far_above_household_volume(client):
    """Until 2026-10-29 neither form had an entry in _RATE_LIMITS (a limit on
    only one of them would be no limit). Both now sit in the normal-use group
    at 2000/hr, in one change -- tests/test_live_route_bounds.py pins the
    number and the 429. What this test still pins: fifty calls each from one
    address, the most a household plausibly makes, pass on both forms."""
    headers = {"X-Forwarded-For": "203.0.113.50"}
    for _ in range(50):
        assert client.get("/my-stops/owner-1", headers=headers).status_code == 200
        assert client.post("/my-stops", json={"device_id": "owner-1"}, headers=headers).status_code == 200
