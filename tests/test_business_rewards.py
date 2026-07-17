from app import BUSINESS_REWARD_DEFAULT_POINTS_COST, Business, Household, Redemption, db


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


# ── Business points_cost ─────────────────────────────────────────────────


def test_register_business_without_points_cost_defaults(client):
    response = _register_business(client)

    assert response.status_code == 201
    assert response.get_json()["points_cost"] == BUSINESS_REWARD_DEFAULT_POINTS_COST


def test_register_business_accepts_custom_points_cost(client):
    response = _register_business(client, points_cost=75)

    assert response.status_code == 201
    assert response.get_json()["points_cost"] == 75


def test_register_business_rejects_negative_points_cost(client):
    response = _register_business(client, points_cost=-5)

    assert response.status_code == 400
    assert "points_cost" in response.get_json()["error"]


def test_register_business_rejects_non_integer_points_cost(client):
    response = _register_business(client, points_cost="a lot")

    assert response.status_code == 400
    assert "points_cost" in response.get_json()["error"]


def test_nearby_businesses_includes_points_cost(client):
    _register_business(client, name="Close Shop", latitude=42.0, longitude=-71.0, points_cost=15)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=5")

    assert response.status_code == 200
    assert response.get_json()[0]["points_cost"] == 15


# ── Rewards catalog ───────────────────────────────────────────────────────


def test_rewards_catalog_includes_registered_business(client):
    _register_business(client, name="Village Toy Shop", reward_offer="Free sticker sheet")

    response = client.get("/rewards-catalog")

    assert response.status_code == 200
    body = response.get_json()
    business_entries = [item for item in body if item["id"].startswith("business-")]
    assert len(business_entries) == 1
    entry = business_entries[0]
    assert entry["name"] == "Free sticker sheet"
    assert entry["sponsor_name"] == "Village Toy Shop"
    assert entry["sponsor_type"] == "local_business"
    assert entry["points_cost"] == BUSINESS_REWARD_DEFAULT_POINTS_COST


def test_rewards_catalog_reflects_custom_business_points_cost(client):
    register_response = _register_business(client, points_cost=60)
    business_id = register_response.get_json()["id"]

    response = client.get("/rewards-catalog")

    entry = next(item for item in response.get_json() if item["id"] == f"business-{business_id}")
    assert entry["points_cost"] == 60


# ── Redemption ────────────────────────────────────────────────────────────


def test_redeem_business_reward_deducts_points_and_returns_code(client):
    register_response = _register_business(client, points_cost=40)
    business_id = register_response.get_json()["id"]

    household = Household(device_id="redeemer-1", points=100)
    db.session.add(household)
    db.session.commit()

    response = client.post(
        f"/redeem-reward/business-{business_id}",
        json={"device_id": "redeemer-1"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["points_remaining"] == 60
    assert body["code"].startswith("TM-")

    redemption = Redemption.query.filter_by(device_id="redeemer-1").first()
    assert redemption is not None
    assert redemption.reward_id == f"business-{business_id}"
    assert redemption.points_spent == 40


def test_redeem_business_reward_insufficient_points_rejected(client):
    register_response = _register_business(client, points_cost=40)
    business_id = register_response.get_json()["id"]

    household = Household(device_id="poor-1", points=5)
    db.session.add(household)
    db.session.commit()

    response = client.post(
        f"/redeem-reward/business-{business_id}",
        json={"device_id": "poor-1"},
    )

    assert response.status_code == 400
    assert "enough points" in response.get_json()["error"]


def test_redeem_reward_unknown_business_id_returns_404(client):
    household = Household(device_id="redeemer-2", points=1000)
    db.session.add(household)
    db.session.commit()

    response = client.post(
        "/redeem-reward/business-999999",
        json={"device_id": "redeemer-2"},
    )

    assert response.status_code == 404


def test_redeem_reward_malformed_business_id_returns_404(client):
    household = Household(device_id="redeemer-3", points=1000)
    db.session.add(household)
    db.session.commit()

    response = client.post(
        "/redeem-reward/business-not-a-number",
        json={"device_id": "redeemer-3"},
    )

    assert response.status_code == 404


def test_redeem_business_reward_rejects_second_redemption(client):
    register_response = _register_business(client, points_cost=10)
    business_id = register_response.get_json()["id"]

    household = Household(device_id="repeat-redeemer", points=1000)
    db.session.add(household)
    db.session.commit()

    payload = {"device_id": "repeat-redeemer"}
    first = client.post(f"/redeem-reward/business-{business_id}", json=payload)
    second = client.post(f"/redeem-reward/business-{business_id}", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already redeemed" in second.get_json()["error"]


def test_get_redemptions_includes_business_reward_with_resolved_name(client):
    register_response = _register_business(
        client, name="Village Toy Shop", reward_offer="Free glow bracelet", points_cost=15
    )
    business_id = register_response.get_json()["id"]

    household = Household(device_id="history-biz", points=1000)
    db.session.add(household)
    db.session.commit()

    client.post(f"/redeem-reward/business-{business_id}", json={"device_id": "history-biz"})

    response = client.get("/redemptions/history-biz")

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == 1
    assert body[0]["reward_id"] == f"business-{business_id}"
    assert body[0]["reward_name"] == "Free glow bracelet"
    assert body[0]["sponsor_name"] == "Village Toy Shop"
    assert body[0]["points_spent"] == 15
