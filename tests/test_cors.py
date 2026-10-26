"""The API is consumed only by the native candy-app build, which sends no
Origin header and does not enforce CORS. These tests pin that: no
Access-Control-Allow-Origin is handed out, so a page in someone's browser
cannot read a response from this API on a visitor's behalf.

If a real web front end ever ships, these are the tests to change -- scope
them to that origin rather than deleting them."""


def test_no_allow_origin_header_for_a_browser_request(client):
    response = client.get("/version", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_no_allow_origin_header_on_a_data_route(client):
    """/version is cheap to call; the routes worth protecting are the ones
    that return rows."""
    response = client.get(
        "/nearby-stops?lat=42.0&lon=-71.0&radius=1",
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_the_native_client_is_unaffected(client):
    """No Origin header -- what a React Native fetch() actually sends."""
    response = client.get("/nearby-stops?lat=42.0&lon=-71.0&radius=1")

    assert response.status_code == 200
    assert response.get_json() == []


def test_no_allow_origin_header_on_the_post_search_route(client):
    """The POST form of the search (2026-10-25) follows the same policy as
    the GET form: no Access-Control-Allow-Origin, so a browser page cannot
    read the answer. A JSON POST from another origin would be preflighted
    first; that preflight is pinned below."""
    response = client.post(
        "/nearby-stops",
        json={"lat": 42.0, "lon": -71.0, "radius": 1},
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_for_a_json_post_is_not_approved(client):
    """What a browser sends before a cross-origin JSON POST. Flask answers
    OPTIONS itself (Allow lists the methods), but with no
    Access-Control-Allow-Origin or -Allow-Headers the browser stops there,
    exactly as it does for a GET it cannot read. No web client exists, so
    this is the intended outcome; if one ships, scope CORS to its origin
    here rather than deleting this test."""
    response = client.options(
        "/nearby-stops",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers.get("Allow", "")
    assert "Access-Control-Allow-Origin" not in response.headers
    assert "Access-Control-Allow-Headers" not in response.headers
