"""PUT /update-candy-status was removed: it wrote candy_available with no
ownership check, letting any passer-by mark any household out of candy.

These tests pin the removal (so the route is not quietly reintroduced) and
pin the replacement path that owners are meant to use instead."""

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


def test_update_candy_status_route_no_longer_exists(client):
    stop = _register_stop(client)

    response = client.put(
        f"/update-candy-status/{stop['id']}",
        json={"available": False},
    )

    assert response.status_code == 404


def test_removed_route_cannot_change_candy_available(client):
    """The behavioural half: the field is untouched, not merely unrouted."""
    stop = _register_stop(client)
    assert db.session.get(Stop, stop["id"]).candy_available is True

    client.put(f"/update-candy-status/{stop['id']}", json={"available": False})

    assert db.session.get(Stop, stop["id"]).candy_available is True


def test_owner_can_still_set_candy_available_through_update_stop(client):
    """The supported replacement path, which does check ownership."""
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "owner-1", "candy_available": False},
    )

    assert response.status_code == 200
    assert db.session.get(Stop, stop["id"]).candy_available is False


def test_a_stranger_still_cannot_set_candy_available(client):
    """The whole point of the removal: no remaining route lets a non-owner
    write this field."""
    stop = _register_stop(client)

    response = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": "not-the-owner", "candy_available": False},
    )

    assert response.status_code == 403
    assert db.session.get(Stop, stop["id"]).candy_available is True
