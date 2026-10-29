"""Input bounds on the five live stop routes (2026-10-29, JOB-13, built from
the JOB-09 audit's findings F1, F2, F5, F6, F7, F9, F10, F11).

Every rejection pinned here is a 400 with a short {"error": ...} body, and
every one refuses only a value no build of candy-app sends. The happy paths
below use the exact request shapes the current app sends, so the contract
is written down in one place. OBSERVED in candy-app on 2026-10-29:

app/porch-setup.tsx:103-114 -- Go Live -> POST /register-stop
      const deviceId = await getDeviceId();
      const response = await fetchWithTimeout(`${API_BASE_URL}/register-stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim(),
          type: 'house',
          latitude: location.latitude,
          longitude: location.longitude,
          device_id: deviceId,
          candy_count: candyCountValue,
        }),
      });

app/my-stops.tsx:43-51 -- load -> POST /my-stops
      const id = await getDeviceId();
      setDeviceId(id);
      // POST with the id in the body rather than GET /my-stops/<id>: the
      // path form put the device id into the server's access log on every
      // visit (see contexts/NearbyDoorsContext.tsx for the same reasoning).
      const response = await fetchWithTimeout(`${API_BASE_URL}/my-stops`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: id }),

app/my-stops.tsx:159-166 -- save -> PATCH /update-stop/<id>
      const response = await fetchWithTimeout(`${API_BASE_URL}/update-stop/${stop.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: deviceId,
          candy_count: candyCountValue,
          candy_available: candyAvailable,
        }),

app/report.tsx:43-51 -- submit -> POST /report-stop/<id>
    if (!stopId || !issue) {
      return;
    }
    setSending(true);
    try {
      const reason = details.trim() ? `${issue}: ${details.trim()}` : issue;
      const response = await fetchWithTimeout(`${API_BASE_URL}/report-stop/${stopId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),

contexts/NearbyDoorsContext.tsx:92-96 -- search -> POST /nearby-stops
      const response = await fetchWithTimeout(`${API_BASE_URL}/nearby-stops`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: latitude, lon: longitude, radius }),
      });

Also OBSERVED: candyCountValue is a Number.isInteger value >= 0, or null when
the field is blank (porch-setup.tsx:48-54, my-stops.tsx:147-155); device ids
are Crypto.randomUUID() (utils/deviceId.ts:12); types/stop.ts:4-6 documents
'business' as the only value other than 'house' the server has ever stored.
"""

import json
from urllib.parse import quote

import app as app_module
from app import MAX_CANDY_COUNT, MAX_REPORT_REASON_CODEPOINTS, MAX_STOP_NAME_CODEPOINTS, Stop, db

# What getDeviceId() produces: a UUID, no padding.
DEVICE_ID = "7b1a0e8e-5a3c-4f0f-9c1d-2f6e8a9b0c1d"

# JSON bodies that are valid JSON but not an object. Each one used to raise
# AttributeError inside the handler (data.get on a list/str/int/bool) and
# come back as a 500.
NON_OBJECT_BODIES = [[1], "x", 5, True]


def _app_registration(**overrides):
    """The porch-setup.tsx body, field for field."""
    payload = {
        "name": "The Pumpkin House",
        "type": "house",
        "latitude": 28.75,
        "longitude": -81.33,
        "device_id": DEVICE_ID,
        "candy_count": 40,
    }
    payload.update(overrides)
    return payload


def _register(client, **overrides):
    return client.post("/register-stop", json=_app_registration(**overrides))


def _stop_count():
    return Stop.query.count()


# ---------------------------------------------------------------------------
# the app's own requests, end to end
# ---------------------------------------------------------------------------


def test_the_five_app_requests_still_succeed_end_to_end(client):
    created = _register(client)
    assert created.status_code == 201
    stop = created.get_json()
    assert stop["name"] == "The Pumpkin House"
    assert stop["type"] == "house"
    assert stop["candy_count"] == 40

    mine = client.post("/my-stops", json={"device_id": DEVICE_ID})
    assert mine.status_code == 200
    assert [s["id"] for s in mine.get_json()] == [stop["id"]]

    saved = client.patch(
        f"/update-stop/{stop['id']}",
        json={"device_id": DEVICE_ID, "candy_count": 12, "candy_available": False},
    )
    assert saved.status_code == 200
    assert saved.get_json()["candy_count"] == 12
    assert saved.get_json()["candy_available"] is False

    reported = client.post(f"/report-stop/{stop['id']}", json={"reason": "Wrong location: it is next door"})
    assert reported.status_code == 200
    assert reported.get_json() == {"status": "reported"}

    found = client.post("/nearby-stops", json={"lat": 28.75, "lon": -81.33, "radius": 1})
    assert found.status_code == 200
    assert stop["id"] in [s["id"] for s in found.get_json()]


def test_register_stop_accepts_a_blank_candy_count_as_the_app_sends_it(client):
    """parseCount returns null for a blank field and the app posts it."""
    response = _register(client, candy_count=None)

    assert response.status_code == 201
    assert response.get_json()["candy_count"] is None


# ---------------------------------------------------------------------------
# F5: a JSON body that is not an object is a 400, never a 500
# ---------------------------------------------------------------------------


def test_non_object_body_on_register_stop_gets_the_missing_body_400(client):
    expected = client.post("/register-stop").get_json()
    assert expected == {"error": "name, type, latitude, and longitude are required"}
    before = _stop_count()

    for body in NON_OBJECT_BODIES:
        response = client.post("/register-stop", json=body)

        assert response.status_code == 400, body
        assert response.get_json() == expected, body

    assert _stop_count() == before


def test_non_object_body_on_update_stop_gets_the_missing_device_id_400(client):
    stop = _register(client).get_json()

    for body in NON_OBJECT_BODIES:
        response = client.patch(f"/update-stop/{stop['id']}", json=body)

        assert response.status_code == 400, body
        assert response.get_json() == {"error": "device_id is required"}, body

    assert db.session.get(Stop, stop["id"]).candy_count == 40


def test_non_object_body_on_report_stop_is_refused_and_not_counted(client):
    """This route has no required field, so reading `[1]` as {} would have
    COUNTED it as a report. It is refused instead. A missing body is still
    a report with no reason, exactly as before."""
    stop = _register(client).get_json()

    for body in NON_OBJECT_BODIES + [[]]:
        response = client.post(f"/report-stop/{stop['id']}", json=body)

        assert response.status_code == 400, body
        assert response.get_json() == {"error": "request body must be a JSON object"}, body

    assert db.session.get(Stop, stop["id"]).report_count == 0

    no_body = client.post(f"/report-stop/{stop['id']}")
    assert no_body.status_code == 200
    assert no_body.get_json() == {"status": "reported"}
    assert db.session.get(Stop, stop["id"]).report_count == 1


# ---------------------------------------------------------------------------
# F1: name and type on /register-stop
# ---------------------------------------------------------------------------


def test_register_stop_accepts_a_business_type_and_a_name_at_the_ceiling(client):
    name = "n" * MAX_STOP_NAME_CODEPOINTS

    response = _register(client, name=name, type="business")

    assert response.status_code == 201
    assert response.get_json()["name"] == name
    assert response.get_json()["type"] == "business"


def test_register_stop_rejects_a_non_string_name_or_type(client):
    before = _stop_count()

    for field, value in (
        ("name", {"a": 1}),
        ("name", [1]),
        ("name", 12345),
        ("name", True),
        ("type", 5),
        ("type", ["house"]),
        ("type", {"type": "house"}),
    ):
        response = _register(client, **{field: value})

        assert response.status_code == 400, (field, value)
        assert response.get_json() == {"error": "name and type must be strings"}, (field, value)

    assert _stop_count() == before


def test_register_stop_rejects_an_empty_or_blank_name(client):
    before = _stop_count()

    for name in ("", "   ", "\t\n"):
        response = _register(client, name=name)

        assert response.status_code == 400, repr(name)
        assert response.get_json() == {"error": "name must not be empty"}, repr(name)

    assert _stop_count() == before


def test_register_stop_strips_the_name_as_the_app_already_does(client):
    response = _register(client, name="  The Pumpkin House  ")

    assert response.status_code == 201
    assert response.get_json()["name"] == "The Pumpkin House"


def test_register_stop_rejects_a_name_over_the_code_point_ceiling(client):
    """The unit is code points (Python len): a family emoji is seven of
    them, so 143 of them are 1,001 and are refused, even though they are
    143 glyphs on screen."""
    before = _stop_count()
    family = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
    assert len(family) == 7

    for name in ("n" * (MAX_STOP_NAME_CODEPOINTS + 1), family * 143):
        response = _register(client, name=name)

        assert response.status_code == 400, len(name)
        assert response.get_json() == {"error": f"name must be {MAX_STOP_NAME_CODEPOINTS} characters or fewer"}

    assert _stop_count() == before


def test_register_stop_rejects_a_type_outside_the_two_stored_values(client):
    before = _stop_count()

    for stop_type in ("x", "House", "porch", "", "house "):
        response = _register(client, type=stop_type)

        assert response.status_code == 400, repr(stop_type)
        assert response.get_json() == {"error": "type must be house or business"}, repr(stop_type)

    assert _stop_count() == before


def test_a_refused_huge_name_never_reaches_a_nearby_stops_response(client):
    """The reason for the ceiling: a stored name is echoed verbatim into
    every search that includes the stop. 200,000 characters is well under
    MAX_CONTENT_LENGTH, so it is this route's own rule that refuses it."""
    assert _register(client).status_code == 201
    huge = _register(client, name="x" * 200_000, device_id="someone-else")
    assert huge.status_code == 400

    found = client.post("/nearby-stops", json={"lat": 28.75, "lon": -81.33, "radius": 1})

    assert found.status_code == 200
    assert len(found.get_json()) == 1
    assert len(found.data) < 2048


# ---------------------------------------------------------------------------
# F2: coordinates on /register-stop
# ---------------------------------------------------------------------------

OUT_OF_RANGE = {"error": "latitude must be between -90 and 90, longitude between -180 and 180"}
NOT_NUMBERS = {"error": "latitude and longitude must be numbers"}


def test_register_stop_rejects_out_of_range_coordinates(client):
    before = _stop_count()

    for latitude, longitude in ((91, -81.33), (-91, -81.33), (28.75, 181), (28.75, -181), (90.0001, 0), (0, -180.0001)):
        response = _register(client, latitude=latitude, longitude=longitude)

        assert response.status_code == 400, (latitude, longitude)
        assert response.get_json() == OUT_OF_RANGE, (latitude, longitude)

    assert _stop_count() == before


def test_register_stop_rejects_non_finite_coordinates_as_strings(client):
    """float() accepts every one of these; "1e400" overflows to inf."""
    before = _stop_count()

    for value in ("nan", "inf", "-inf", "Infinity", "1e400"):
        as_latitude = _register(client, latitude=value)
        as_longitude = _register(client, longitude=value)

        assert as_latitude.status_code == 400, value
        assert as_latitude.get_json() == NOT_NUMBERS, value
        assert as_longitude.status_code == 400, value
        assert as_longitude.get_json() == NOT_NUMBERS, value

    assert _stop_count() == before


def test_register_stop_rejects_the_bare_nan_and_infinity_json_tokens(client):
    """Python's json module writes AND reads the non-standard tokens NaN,
    Infinity and -Infinity, and Flask's loader is plain json.loads, so
    these arrive as floats rather than as a parse error."""
    before = _stop_count()

    for value, token in ((float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity")):
        raw = json.dumps(_app_registration(latitude=value))
        assert token in raw

        response = client.post("/register-stop", data=raw, content_type="application/json")

        assert response.status_code == 400, token
        assert response.get_json() == NOT_NUMBERS, token

    assert _stop_count() == before


def test_register_stop_accepts_the_boundary_coordinates(client):
    for latitude, longitude in ((90, 180), (-90, -180), (0, 0)):
        response = _register(client, latitude=latitude, longitude=longitude)

        assert response.status_code == 201, (latitude, longitude)


def test_nearby_stops_messages_are_unchanged_by_the_shared_rule(client):
    """_parse_search_origin now calls the shared helper; its two messages
    must still read exactly as the installed build has always seen them."""
    out_of_range = client.get("/nearby-stops?lat=91&lon=0")
    not_finite = client.get("/nearby-stops?lat=inf&lon=0")
    via_post = client.post("/nearby-stops", json={"lat": 0, "lon": 181})

    assert out_of_range.status_code == 400
    assert out_of_range.get_json() == {"error": "lat must be between -90 and 90, lon between -180 and 180"}
    assert not_finite.status_code == 400
    assert not_finite.get_json() == {"error": "lat and lon must be numbers"}
    assert via_post.get_json() == out_of_range.get_json()


def test_full_scan_search_still_answers_after_bad_coordinates_were_refused(client):
    """A stored NaN or Infinity row would make math.floor raise inside
    _verified_stop_ids on any search that falls back to a full table scan.
    A 25 km search at lon 179.9 crosses the antimeridian, which is the
    full-scan path, so this is the search that would have 500'd."""
    assert _register(client, latitude="nan").status_code == 400
    assert _register(client, latitude="inf").status_code == 400
    assert _register(client, name="Real", latitude=0.0, longitude=179.9).status_code == 201

    response = client.get("/nearby-stops?lat=0&lon=179.9&radius=25")

    assert response.status_code == 200
    assert [s["name"] for s in response.get_json()] == ["Real"]


# ---------------------------------------------------------------------------
# F6: candy_count ceiling on both routes
# ---------------------------------------------------------------------------

TOO_MANY = {"error": f"candy_count must be {MAX_CANDY_COUNT} or less"}


def test_candy_count_above_the_integer_ceiling_is_refused_on_both_routes(client):
    assert MAX_CANDY_COUNT == 2**31 - 1
    before = _stop_count()

    for count in (MAX_CANDY_COUNT + 1, 2**40):
        response = _register(client, candy_count=count)

        assert response.status_code == 400, count
        assert response.get_json() == TOO_MANY, count

    assert _stop_count() == before

    stop = _register(client).get_json()
    response = client.patch(f"/update-stop/{stop['id']}", json={"device_id": DEVICE_ID, "candy_count": 2**31})

    assert response.status_code == 400
    assert response.get_json() == TOO_MANY
    assert db.session.get(Stop, stop["id"]).candy_count == 40


def test_candy_count_at_the_integer_ceiling_is_accepted_on_both_routes(client):
    created = _register(client, candy_count=MAX_CANDY_COUNT)
    assert created.status_code == 201
    assert created.get_json()["candy_count"] == MAX_CANDY_COUNT

    stop = _register(client, device_id="other").get_json()
    saved = client.patch(f"/update-stop/{stop['id']}", json={"device_id": "other", "candy_count": MAX_CANDY_COUNT})

    assert saved.status_code == 200
    assert saved.get_json()["candy_count"] == MAX_CANDY_COUNT


# ---------------------------------------------------------------------------
# F7 (rate limits only): /my-stops, both forms, and /update-stop
# ---------------------------------------------------------------------------


def test_my_stops_and_update_stop_share_the_normal_use_limit(client):
    for endpoint in ("my_stops", "my_stops_lookup", "update_stop"):
        assert app_module._RATE_LIMITS[endpoint] == (2000, 3600), endpoint
        assert app_module._RATE_LIMITS[endpoint] == app_module._RATE_LIMITS["nearby_stops"], endpoint


def test_my_stops_get_form_is_rate_limited_per_ip(client, monkeypatch):
    """Lowered budget, as the nearby-stops limiter test does: spending the
    real 2000 proves nothing extra."""
    monkeypatch.setitem(app_module._RATE_LIMITS, "my_stops", (3, 3600))
    headers = {"X-Forwarded-For": "203.0.113.60"}

    for _ in range(3):
        assert client.get(f"/my-stops/{DEVICE_ID}", headers=headers).status_code == 200
    blocked = client.get(f"/my-stops/{DEVICE_ID}", headers=headers)
    other = client.get(f"/my-stops/{DEVICE_ID}", headers={"X-Forwarded-For": "198.51.100.60"})

    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    assert other.status_code == 200


def test_my_stops_post_form_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setitem(app_module._RATE_LIMITS, "my_stops_lookup", (3, 3600))
    headers = {"X-Forwarded-For": "203.0.113.61"}
    body = {"device_id": DEVICE_ID}

    for _ in range(3):
        assert client.post("/my-stops", json=body, headers=headers).status_code == 200
    blocked = client.post("/my-stops", json=body, headers=headers)
    other = client.post("/my-stops", json=body, headers={"X-Forwarded-For": "198.51.100.61"})

    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    assert other.status_code == 200


def test_the_two_my_stops_forms_are_separate_buckets(client, monkeypatch):
    """Keyed the way the limiter already keys, on request.endpoint: the GET
    and POST forms are two view functions, so one exhausted form does not
    lock the other. Written down so the choice is visible, not hidden."""
    monkeypatch.setitem(app_module._RATE_LIMITS, "my_stops", (2, 3600))
    monkeypatch.setitem(app_module._RATE_LIMITS, "my_stops_lookup", (2, 3600))
    headers = {"X-Forwarded-For": "203.0.113.62"}

    for _ in range(2):
        assert client.get(f"/my-stops/{DEVICE_ID}", headers=headers).status_code == 200
    assert client.get(f"/my-stops/{DEVICE_ID}", headers=headers).status_code == 429

    assert client.post("/my-stops", json={"device_id": DEVICE_ID}, headers=headers).status_code == 200


def test_update_stop_is_rate_limited_per_ip(client, monkeypatch):
    stop = _register(client).get_json()
    monkeypatch.setitem(app_module._RATE_LIMITS, "update_stop", (3, 3600))
    headers = {"X-Forwarded-For": "203.0.113.63"}
    body = {"device_id": DEVICE_ID, "candy_count": 5}

    for _ in range(3):
        assert client.patch(f"/update-stop/{stop['id']}", json=body, headers=headers).status_code == 200
    blocked = client.patch(f"/update-stop/{stop['id']}", json=body, headers=headers)
    other = client.patch(f"/update-stop/{stop['id']}", json=body, headers={"X-Forwarded-For": "198.51.100.63"})

    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    assert other.status_code == 200


def test_update_stop_wrong_owner_and_missing_stop_answers_are_unchanged(client):
    """F7's 404-for-403 change is Ramzi's call and was not taken."""
    stop = _register(client).get_json()

    wrong_owner = client.patch(f"/update-stop/{stop['id']}", json={"device_id": "not-the-owner", "candy_count": 5})
    missing = client.patch("/update-stop/999999", json={"device_id": DEVICE_ID, "candy_count": 5})

    assert wrong_owner.status_code == 403
    assert wrong_owner.get_json() == {"error": "you can only edit stops you registered"}
    assert missing.status_code == 404
    assert missing.get_json() == {"error": "stop not found"}
    assert db.session.get(Stop, stop["id"]).candy_count == 40


# ---------------------------------------------------------------------------
# F9 (narrowed): control characters and bidi overrides in the name
# ---------------------------------------------------------------------------


def test_register_stop_rejects_control_and_bidi_characters_in_the_name(client):
    before = _stop_count()

    for name in (
        "a\x01b",  # C0
        "a\x1bb",  # ESC
        "a\x1fb",  # last C0
        "a\x7fb",  # DEL
        "a\x85b",  # C1 NEL
        "a\x9fb",  # last C1
        "a‪b",  # LEFT-TO-RIGHT EMBEDDING
        "a‮b",  # RIGHT-TO-LEFT OVERRIDE
        "a⁦b",  # LEFT-TO-RIGHT ISOLATE
        "a⁩b",  # POP DIRECTIONAL ISOLATE
    ):
        response = _register(client, name=name)

        assert response.status_code == 400, repr(name)
        assert response.get_json() == {"error": "name contains unsupported characters"}, repr(name)

    assert _stop_count() == before


def test_register_stop_keeps_tabs_newlines_and_real_text_in_the_name(client):
    """Tab, newline and carriage return stay allowed: a pasted value can
    carry one. Accents, emoji, a ZWJ sequence and right-to-left script are
    ordinary text."""
    for name in (
        "Café ☕",
        "a\tb",
        "line one\nline two",
        "a\r\nb",
        "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466 house",
        "שלום",
    ):
        response = _register(client, name=name, device_id=f"dev-{len(name)}")

        assert response.status_code == 201, repr(name)
        assert response.get_json()["name"] == name, repr(name)


def test_a_nul_in_the_name_keeps_its_own_older_message(client):
    """NUL is a control character too, but _reject_nul_bytes runs first on
    every route, so the message pinned in test_sql_injection.py stays."""
    response = _register(client, name="a\x00b")

    assert response.status_code == 400
    assert response.get_json() == {"error": "name must not contain NUL characters"}


# ---------------------------------------------------------------------------
# F10: device_id stripped on the two lookup routes as on /register-stop
# ---------------------------------------------------------------------------


def test_padded_device_id_matches_a_stripped_registration_on_the_body_routes(client):
    stop = _register(client, device_id=f"  {DEVICE_ID}  ").get_json()

    for sent in (DEVICE_ID, f"  {DEVICE_ID}  ", f"\t{DEVICE_ID}\n"):
        mine = client.post("/my-stops", json={"device_id": sent})
        assert mine.status_code == 200, repr(sent)
        assert [s["id"] for s in mine.get_json()] == [stop["id"]], repr(sent)

        edit = client.patch(f"/update-stop/{stop['id']}", json={"device_id": sent, "candy_count": 7})
        assert edit.status_code == 200, repr(sent)
        assert edit.get_json()["candy_count"] == 7, repr(sent)


def test_the_get_form_of_my_stops_still_does_not_strip(client):
    """Kept exactly as it was for the installed build: a padded id in the
    path matches nothing, as before."""
    stop = _register(client).get_json()

    exact = client.get(f"/my-stops/{DEVICE_ID}")
    padded = client.get("/my-stops/" + quote(f"  {DEVICE_ID}  ", safe=""))

    assert [s["id"] for s in exact.get_json()] == [stop["id"]]
    assert padded.status_code == 200
    assert padded.get_json() == []


def test_update_stop_non_string_device_id_is_a_400_like_my_stops(client):
    """Before 2026-10-29 an int, list, dict or `true` here passed the
    `if not device_id` check and fell through to a 403."""
    stop = _register(client).get_json()

    for device_id in (123, ["x"], {"id": "x"}, True, "   "):
        response = client.patch(f"/update-stop/{stop['id']}", json={"device_id": device_id, "candy_count": 5})

        assert response.status_code == 400, repr(device_id)
        assert response.get_json() == {"error": "device_id is required"}, repr(device_id)

    assert db.session.get(Stop, stop["id"]).candy_count == 40


# ---------------------------------------------------------------------------
# F11: reason on /report-stop
# ---------------------------------------------------------------------------


def test_report_reason_accepts_what_report_tsx_builds(client):
    """A chip alone, or `${issue}: ${details}`."""
    stop = _register(client).get_json()

    for reason in ("Wrong location", "Inappropriate: the sign is gone", "Closed", "Other: " + "d" * 500):
        response = client.post(f"/report-stop/{stop['id']}", json={"reason": reason})

        assert response.status_code == 200, reason[:20]
        assert response.get_json() == {"status": "reported"}

    assert db.session.get(Stop, stop["id"]).report_count == 4


def test_report_reason_over_the_ceiling_is_refused_and_not_counted(client):
    stop = _register(client).get_json()

    too_long = client.post(f"/report-stop/{stop['id']}", json={"reason": "r" * (MAX_REPORT_REASON_CODEPOINTS + 1)})

    assert too_long.status_code == 400
    assert too_long.get_json() == {"error": f"reason must be {MAX_REPORT_REASON_CODEPOINTS} characters or fewer"}
    assert db.session.get(Stop, stop["id"]).report_count == 0

    at_ceiling = client.post(f"/report-stop/{stop['id']}", json={"reason": "r" * MAX_REPORT_REASON_CODEPOINTS})

    assert at_ceiling.status_code == 200
    assert db.session.get(Stop, stop["id"]).report_count == 1
