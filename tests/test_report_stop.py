from app import REPORT_HIDE_THRESHOLD, Stop, db


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


def test_report_stop_does_not_return_the_household_coordinates(client):
    """The security case. Reporting is open to strangers by design and stop
    ids are sequential, so anything this endpoint returns is readable by
    anyone willing to count. It must not hand back the address."""
    stop = _register_stop(client)

    response = client.post(f"/report-stop/{stop['id']}", json={"reason": ""})

    assert response.status_code == 200
    body = response.get_json()
    assert "latitude" not in body
    assert "longitude" not in body


def test_report_stop_returns_only_an_acknowledgement(client):
    """Stronger than the coordinate check above: pin the whole body, so any
    future change that starts leaking a field again fails here."""
    stop = _register_stop(client)

    response = client.post(f"/report-stop/{stop['id']}", json={"reason": ""})

    assert response.get_json() == {"status": "reported"}


def test_report_stop_increments_the_report_count(client):
    stop = _register_stop(client)

    client.post(f"/report-stop/{stop['id']}", json={"reason": "not real"})

    stored = db.session.get(Stop, stop["id"])
    assert stored.report_count == 1
    assert stored.is_hidden is False


def test_report_stop_hides_the_stop_at_the_threshold(client):
    stop = _register_stop(client)

    for _ in range(REPORT_HIDE_THRESHOLD - 1):
        client.post(f"/report-stop/{stop['id']}", json={"reason": ""})
    assert db.session.get(Stop, stop["id"]).is_hidden is False

    client.post(f"/report-stop/{stop['id']}", json={"reason": ""})

    stored = db.session.get(Stop, stop["id"])
    assert stored.report_count == REPORT_HIDE_THRESHOLD
    assert stored.is_hidden is True


def test_hidden_stop_drops_out_of_nearby_stops(client):
    stop = _register_stop(client)

    for _ in range(REPORT_HIDE_THRESHOLD):
        client.post(f"/report-stop/{stop['id']}", json={"reason": ""})

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=5")

    assert response.status_code == 200
    assert [s for s in response.get_json() if s["id"] == stop["id"]] == []


def test_report_stop_accepts_a_missing_reason(client):
    stop = _register_stop(client)

    response = client.post(f"/report-stop/{stop['id']}", json={})

    assert response.status_code == 200


def test_report_stop_rejects_a_non_string_reason(client):
    stop = _register_stop(client)

    response = client.post(f"/report-stop/{stop['id']}", json={"reason": 7})

    assert response.status_code == 400
    assert db.session.get(Stop, stop["id"]).report_count == 0


def test_report_stop_missing_stop_returns_404(client):
    response = client.post("/report-stop/999999", json={"reason": ""})

    assert response.status_code == 404
