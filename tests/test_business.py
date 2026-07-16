from app import Business, db


def _register_business(client, **overrides):
    payload = {
        "name": "Sweet Tooth Bakery",
        "address": "12 Main St",
        "latitude": 42.0,
        "longitude": -71.0,
        "category": "bakery",
        "description": "Fresh donuts and cider donuts all October.",
        "contact_email": "owner@sweettooth.example",
        "reward_offer": "Free mini donut with any purchase",
        "device_id": "biz-registrant-1",
    }
    payload.update(overrides)
    return client.post("/register-business", json=payload)


def test_register_business_succeeds_with_all_fields(client):
    response = _register_business(client)

    assert response.status_code == 201
    body = response.get_json()
    assert body["name"] == "Sweet Tooth Bakery"
    assert body["category"] == "bakery"
    assert body["reward_offer"] == "Free mini donut with any purchase"
    assert body["id"] is not None

    stored = db.session.get(Business, body["id"])
    assert stored is not None
    assert stored.contact_email == "owner@sweettooth.example"


def test_register_business_allows_missing_address(client):
    response = _register_business(client, address=None)

    assert response.status_code == 201
    assert response.get_json()["address"] is None


def test_register_business_rejects_missing_required_field(client):
    response = _register_business(client, reward_offer=None)

    assert response.status_code == 400
    assert "reward_offer" in response.get_json()["error"]


def test_register_business_rejects_non_numeric_coordinates(client):
    response = _register_business(client, latitude="not-a-number")

    assert response.status_code == 400
    assert "latitude" in response.get_json()["error"]


def test_nearby_businesses_filters_by_radius(client):
    _register_business(client, name="Close Shop", latitude=42.0, longitude=-71.0)
    _register_business(client, name="Far Shop", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=5")

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == 1
    assert body[0]["name"] == "Close Shop"
    assert "distance_km" in body[0]


def test_nearby_businesses_sorted_by_distance(client):
    _register_business(client, name="Nearer", latitude=42.01, longitude=-71.0)
    _register_business(client, name="Nearest", latitude=42.001, longitude=-71.0)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=10")

    assert response.status_code == 200
    body = response.get_json()
    assert [b["name"] for b in body] == ["Nearest", "Nearer"]


def test_nearby_businesses_requires_lat_lon(client):
    response = client.get("/nearby-businesses")

    assert response.status_code == 400
