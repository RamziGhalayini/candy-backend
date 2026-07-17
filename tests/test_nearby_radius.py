"""request.args.get(..., type=float) silently falls back to the default on a
parse failure instead of erroring, so a malformed radius used to quietly
search 1km instead of failing loudly. These tests cover the explicit
_parse_radius_km validation added to /nearby-stops and /nearby-businesses."""


def _register_stop(client, **overrides):
    payload = {
        "name": "Nearby Test House",
        "type": "house",
        "latitude": 42.0,
        "longitude": -71.0,
        "device_id": "registrant-1",
    }
    payload.update(overrides)
    return client.post("/register-stop", json=payload)


def _register_business(client, **overrides):
    payload = {
        "name": "Nearby Test Shop",
        "latitude": 42.0,
        "longitude": -71.0,
        "category": "shop",
        "description": "A shop",
        "contact_email": "owner@example.com",
        "reward_offer": "Free thing",
        "device_id": "biz-registrant-1",
    }
    payload.update(overrides)
    return client.post("/register-business", json=payload)


def test_nearby_stops_rejects_malformed_radius(client):
    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=99%2C999")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_stops_rejects_empty_radius(client):
    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_stops_omitted_radius_defaults_to_one_km(client):
    _register_stop(client, name="Close", latitude=42.0, longitude=-71.0)
    _register_stop(client, name="Far", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0")

    assert response.status_code == 200
    names = [s["name"] for s in response.get_json()]
    assert "Close" in names
    assert "Far" not in names


def test_nearby_stops_accepts_valid_large_radius(client):
    _register_stop(client, name="Far House", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=99999")

    assert response.status_code == 200
    assert any(s["name"] == "Far House" for s in response.get_json())


def test_nearby_businesses_rejects_malformed_radius(client):
    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=99%2C999")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_businesses_rejects_empty_radius(client):
    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_businesses_omitted_radius_defaults_to_one_km(client):
    _register_business(client, name="Close Shop", latitude=42.0, longitude=-71.0)
    _register_business(client, name="Far Shop", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0")

    assert response.status_code == 200
    names = [b["name"] for b in response.get_json()]
    assert "Close Shop" in names
    assert "Far Shop" not in names


def test_nearby_businesses_accepts_valid_large_radius(client):
    _register_business(client, name="Far Shop", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=99999")

    assert response.status_code == 200
    assert any(b["name"] == "Far Shop" for b in response.get_json())
