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


def test_nearby_stops_rejects_radius_above_the_cap(client):
    """Was test_nearby_stops_accepts_valid_large_radius, which asserted that
    radius=99999 returned a house 333 km away. That radius is now over
    MAX_RADIUS_KM and 400s, so the same request asserts the opposite. Kept as
    a case rather than dropped: this exact request is the one that proves the
    ceiling is enforced, and it is the one a client sending a stale radius
    will make."""
    _register_stop(client, name="Far House", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=99999")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_stops_accepts_the_largest_radius_the_client_can_send(client):
    """The other half of the cap: 10 miles is candy-app's largest preset
    (16.09 km), so it has to keep working. Guards against a future cap being
    tightened to a value that silently breaks 'giving' mode."""
    _register_stop(client, name="Ten Miles Out", latitude=42.14, longitude=-71.0)

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=16.1")

    assert response.status_code == 200
    assert any(s["name"] == "Ten Miles Out" for s in response.get_json())


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


def test_nearby_businesses_rejects_radius_above_the_cap(client):
    """The businesses twin of the stops case above. Both routes share
    _parse_radius_km, so the cap arrives on both at once."""
    _register_business(client, name="Far Shop", latitude=45.0, longitude=-71.0)

    response = client.get("/nearby-businesses?lat=42.0&lon=-71.0&radius=99999")

    assert response.status_code == 400
    assert "radius" in response.get_json()["error"]


def test_nearby_searches_reject_non_finite_radius(client):
    """float() accepts these; they are not radii."""
    for value in ("inf", "-inf", "nan"):
        response = client.get(f"/nearby-stops?lat=42.0&lon=-71.0&radius={value}")

        assert response.status_code == 400, value
        assert "radius" in response.get_json()["error"], value


def test_nearby_searches_reject_non_finite_coordinates(client):
    """math.sin() raises on infinity, so this reached the haversine and came
    back a 500 before the origin was validated."""
    response = client.get("/nearby-stops?lat=inf&lon=-71.0")

    assert response.status_code == 400
    assert "lat" in response.get_json()["error"]


def test_nearby_searches_reject_out_of_range_coordinates(client):
    response = client.get("/nearby-stops?lat=91.0&lon=-71.0")

    assert response.status_code == 400
    assert "lat" in response.get_json()["error"]


def test_verification_counts_a_neighbour_outside_the_search_radius(client):
    """The search no longer measures every row -- it fetches a bounding box
    first -- and that box also feeds each result's `verified` flag. So the box
    is widened by VERIFICATION_TOLERANCE_DEGREES, because a stop INSIDE the
    radius can be verified by a twin just OUTSIDE it.

    Twin is 0.0004 degrees from the house (inside the 0.0005 tolerance) but
    0.1446 km from the search origin (outside the 0.12 km radius), so it is
    never returned -- it only has to be counted."""
    _register_stop(client, name="House", latitude=42.0009, longitude=-71.0)
    _register_stop(client, name="Twin", latitude=42.0013, longitude=-71.0)

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=0.12")

    assert response.status_code == 200
    body = response.get_json()
    assert [s["name"] for s in body] == ["House"], "the twin is out of range"
    assert body[0]["verified"] is True, "but it still verifies the house"


def test_nearby_stops_returns_at_most_the_result_cap(client):
    """MAX_NEARBY_RESULTS bounds the response independently of the radius,
    keeping the nearest results."""
    from app import MAX_NEARBY_RESULTS, Stop, db

    # Straight to the session: this needs more rows than the cap, and the
    # registration route runs a verification-bonus query per call.
    for index in range(MAX_NEARBY_RESULTS + 5):
        db.session.add(
            Stop(
                name=f"House {index:03d}",
                type="house",
                latitude=42.0 + (index + 1) * 0.0001,
                longitude=-71.0,
                candy_available=True,
            )
        )
    db.session.commit()

    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=25")

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == MAX_NEARBY_RESULTS
    assert body[0]["name"] == "House 000", "nearest first"
    assert body[-1]["name"] == f"House {MAX_NEARBY_RESULTS - 1:03d}", "furthest dropped"


def test_report_count_is_not_in_the_response(client):
    """Internal moderation bookkeeping; nothing in candy-app renders it."""
    stop = _register_stop(client).get_json()

    assert "report_count" not in stop

    listed = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=1").get_json()
    assert listed
    assert all("report_count" not in s for s in listed)
