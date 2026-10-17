"""The daily trivia and the founder-brand rewards catalog were retired on
2026-10-17 (Ramzi's decision: no Halloween content). These tests pin the
removal so the routes are not quietly reintroduced, and pin what still
works around them: business perks redeem as before, and rows already in the
trivia_answer and redemption tables are left alone and still readable."""

from datetime import datetime, timezone

from app import Household, Redemption, TriviaAnswer, db


def _register_business(client, **overrides):
    payload = {
        "name": "Sweet Tooth Bakery",
        "address": "12 Main St",
        "latitude": 42.0,
        "longitude": -71.0,
        "category": "bakery",
        "description": "A short description.",
        "contact_email": "owner@example.test",
        "reward_offer": "Free mini donut",
        "device_id": "biz-registrant-1",
        "points_cost": 40,
    }
    payload.update(overrides)
    return client.post("/register-business", json=payload)


def test_trivia_today_route_no_longer_exists(client):
    assert client.get("/trivia/today").status_code == 404


def test_trivia_answer_route_no_longer_exists(client):
    response = client.post("/trivia/answer", json={"device_id": "trivia-1", "answer": 0})

    assert response.status_code == 404
    assert TriviaAnswer.query.count() == 0


def test_rewards_catalog_route_no_longer_exists(client):
    assert client.get("/rewards-catalog").status_code == 404


def test_founder_catalog_ids_can_no_longer_be_redeemed(client):
    """The ids the retired catalog served are now unknown rewards: 404, and no
    points move."""
    household = Household(device_id="redeemer-1", points=1000)
    db.session.add(household)
    db.session.commit()

    for reward_id in ("founder-sugarplum-bars", "founder-lantern-cocoa", "local-village-bakery", "app-sticker-pack"):
        response = client.post(f"/redeem-reward/{reward_id}", json={"device_id": "redeemer-1"})
        assert response.status_code == 404, reward_id

    assert db.session.get(Household, household.id).points == 1000
    assert Redemption.query.filter_by(device_id="redeemer-1").count() == 0


def test_business_perk_still_redeems(client):
    business_id = _register_business(client).get_json()["id"]
    household = Household(device_id="redeemer-2", points=100)
    db.session.add(household)
    db.session.commit()

    response = client.post(f"/redeem-reward/business-{business_id}", json={"device_id": "redeemer-2"})

    assert response.status_code == 200
    assert response.get_json()["points_remaining"] == 60


def test_existing_founder_redemption_rows_still_list(client):
    """A row redeemed from the old catalog before the retirement is kept and
    still returned by /redemptions; only its display name degrades to the raw
    reward_id, since nothing resolves that id any more."""
    db.session.add(Household(device_id="old-device", points=0))
    db.session.add(
        Redemption(
            device_id="old-device",
            reward_id="founder-sugarplum-bars",
            points_spent=50,
            code="TM-OLDCODE1",
            created_at=datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
    )
    db.session.commit()

    body = client.get("/redemptions/old-device").get_json()

    assert len(body) == 1
    assert body[0]["reward_id"] == "founder-sugarplum-bars"
    assert body[0]["reward_name"] == "founder-sugarplum-bars"
    assert body[0]["sponsor_name"] is None
    assert body[0]["code"] == "TM-OLDCODE1"
    assert Redemption.query.count() == 1
