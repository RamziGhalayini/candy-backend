"""Guards for the 2026-09-16 hardening pass.

Each test here was written to FAIL against the code as it stood before that
pass. They are the reason to believe the fixes do what the commit says.
"""

import pytest

import app as app_module


def _register(client, device_id="dev-owner", lat=28.75, lon=-81.33, name="Test House"):
    return client.post(
        "/register-stop",
        json={
            "name": name,
            "type": "house",
            "latitude": lat,
            "longitude": lon,
            "device_id": device_id,
        },
    )


# --------------------------------------------------------------------------
# 1. contact_email is not in the public business payload
# --------------------------------------------------------------------------

def _register_business(client):
    return client.post(
        "/register-business",
        json={
            "name": "Sugarplum Confections",
            "address": "1 Main St",
            "latitude": 28.75,
            "longitude": -81.33,
            "category": "candy",
            "description": "Sweets",
            "contact_email": "owner@sugarplum.test",
            "reward_offer": "Free lollipop",
            "device_id": "biz-dev",
        },
    )


def test_nearby_businesses_does_not_leak_contact_email(client):
    assert _register_business(client).status_code == 201

    response = client.get("/nearby-businesses?lat=28.75&lon=-81.33&radius=5")
    assert response.status_code == 200
    rows = response.get_json()
    assert rows, "expected the business to be in range"

    for row in rows:
        assert "contact_email" not in row
    # and the address really is absent from the raw bytes, not just the keys
    assert b"owner@sugarplum.test" not in response.data


def test_registrant_still_gets_their_own_contact_email_back(client):
    response = _register_business(client)
    assert response.status_code == 201
    assert response.get_json()["contact_email"] == "owner@sugarplum.test"


