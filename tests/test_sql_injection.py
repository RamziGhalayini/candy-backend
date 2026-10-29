"""SQL-injection guard tests (2026-10-25).

Every request value that reaches the database in app.py does so through the
ORM (filter_by / filter on model columns / session.get / model constructors)
or through pydantic-validated fields; the only raw SQL is the fixed ALTER
TABLE strings in _run_lightweight_migrations. These tests pin that:

1. classic injection strings in every request field get the same answer any
   other wrong (or merely unusual) value gets, never a 5xx;
2. every table still exists and has the row count it had before, except
   where a route legitimately writes one row, in which case the payload is
   stored as literal text;
3. the SQL the engine actually executes never contains the payload in its
   statement text -- only in the bound-parameter tuple;
4. no raw SQL creeps into app.py outside the migration function.

OBSERVED: the suite's database is SQLite (tests/conftest.py sets
DATABASE_URL to a sqlite:/// copy of candy.db). Production is Postgres via
psycopg2. Parameter binding is the driver's job in both, so the injection
conclusions carry over; where SQLite and Postgres are known to differ in
how they treat a bad VALUE (NUL bytes, a non-integer where an integer is
expected) the test says so and the Postgres statement is REASONED.
"""

import ast
import os
from unittest.mock import patch
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from sqlalchemy import event

import app as app_module
from app import Business, Stop, db
from apple_verification import AppleVerificationError

INJECTIONS = [
    "' OR '1'='1",
    "'; DROP TABLE stop; --",
    "1 OR 1=1",
    '" OR ""="',
    "x'); DELETE FROM stop; --",
    "' UNION SELECT 1,2,3 --",
]

SEARCH_ORIGIN = {"lat": 42.0, "lon": -71.0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _snapshot():
    """{table name: row count} for every mapped table, read straight from the
    engine. A dropped table makes this raise, which is the point."""
    counts = {}
    for table in db.metadata.sorted_tables:
        counts[table.name] = db.session.execute(
            sa.select(sa.func.count()).select_from(table)
        ).scalar()
    existing = set(sa.inspect(db.engine).get_table_names())
    assert set(counts) <= existing, "a mapped table is missing from the database"
    return counts


def _path(value):
    return quote(value, safe="")


def _seed(client):
    """One of everything the routes read, so an injected read has rows it
    could have leaked and an injected write has rows it could have deleted.

    Two stops at one address verify each other, which lets a check-in land
    (CheckIn + Household rows). A mocked mint gives a LegendUnlock row."""
    stop_payload = {
        "name": "Seed House",
        "type": "house",
        "latitude": 42.0,
        "longitude": -71.0,
        "device_id": "seed-owner",
    }
    owner_stop = client.post("/register-stop", json=stop_payload).get_json()
    client.post("/register-stop", json={**stop_payload, "name": "Seed Neighbour", "device_id": "seed-neighbour"})
    client.post(
        "/register-business",
        json={
            "name": "Seed Shop",
            "latitude": 42.0,
            "longitude": -71.0,
            "category": "shop",
            "description": "A shop",
            "contact_email": "owner@example.com",
            "reward_offer": "Free thing",
            "device_id": "seed-business",
        },
    )
    checked_in = client.post(
        "/check-in",
        json={"device_id": "seed-walker", "stop_id": owner_stop["id"], "latitude": 42.0, "longitude": -71.0},
    )
    assert checked_in.status_code == 200, checked_in.get_json()
    client.post("/trivia/answer", json={"device_id": "seed-trivia", "answer": 0})
    with patch("app.verify_transaction") as mocked:
        mocked.return_value = None
        minted = client.post("/legend-mode/mint", json={"device_id": "seed-legend", "transaction_id": "seed-txn"})
    assert minted.status_code == 200, minted.get_json()
    return owner_stop


@pytest.fixture
def seeded(client):
    return _seed(client)


@pytest.fixture
def executed_sql():
    """Every (statement, parameters) pair the engine runs during the test.
    Injection is, by definition, the payload ending up in the statement
    string; a bound parameter is the opposite of that."""
    captured = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        yield captured
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)


def _assert_payload_never_in_statement_text(executed_sql, payload):
    statements_with_payload = [s for s, _ in executed_sql if payload in s]
    assert statements_with_payload == [], statements_with_payload
    bound = [p for _, p in executed_sql if payload in repr(p)]
    assert bound, "expected the payload to reach the engine as a bound parameter"


# ---------------------------------------------------------------------------
# 1. device_id in the URL path (the four GET routes that still take it)
# ---------------------------------------------------------------------------


def test_injected_device_id_in_the_path_reads_nothing_and_changes_nothing(client, seeded):
    before = _snapshot()

    for payload in INJECTIONS:
        encoded = _path(payload)

        my_stops = client.get(f"/my-stops/{encoded}")
        assert my_stops.status_code == 200, payload
        assert my_stops.get_json() == [], payload

        points = client.get(f"/points/{encoded}")
        assert points.status_code == 200, payload
        assert points.get_json()["points"] == 0, payload

        redemptions = client.get(f"/redemptions/{encoded}")
        assert redemptions.status_code == 200, payload
        assert redemptions.get_json() == [], payload

        ledger = client.get(f"/night-ledger/{encoded}")
        assert ledger.status_code == 200, payload

    assert _snapshot() == before


def test_injected_device_id_in_the_path_is_a_bound_parameter(client, seeded, executed_sql):
    payload = INJECTIONS[1]

    client.get(f"/my-stops/{_path(payload)}")

    _assert_payload_never_in_statement_text(executed_sql, payload)


# ---------------------------------------------------------------------------
# 2. device_id in a JSON body or a form field
# ---------------------------------------------------------------------------


def test_injected_device_id_in_a_body_gets_the_wrong_owner_answer(client, seeded):
    stop_id = seeded["id"]
    before = _snapshot()

    for payload in INJECTIONS:
        lookup = client.post("/my-stops", json={"device_id": payload})
        assert lookup.status_code == 200, payload
        assert lookup.get_json() == [], payload

        edit = client.patch(f"/update-stop/{stop_id}", json={"device_id": payload, "candy_count": 1})
        assert edit.status_code == 403, payload

        # multipart form field, checked before the file is even looked at
        greeting = client.post(f"/upload-greeting/{stop_id}", data={"device_id": payload})
        assert greeting.status_code == 403, payload

        redeem = client.post("/redeem-reward/founder-1", json={"device_id": payload})
        assert redeem.status_code in (400, 404), payload

    assert _snapshot() == before
    assert db.session.get(Stop, stop_id).candy_count is None


def test_injected_device_id_in_a_body_is_a_bound_parameter(client, seeded, executed_sql):
    payload = INJECTIONS[0]

    client.post("/my-stops", json={"device_id": payload})
    client.patch(f"/update-stop/{seeded['id']}", json={"device_id": payload, "candy_count": 1})

    _assert_payload_never_in_statement_text(executed_sql, payload)


# ---------------------------------------------------------------------------
# 3. the routes that legitimately WRITE a caller-chosen string: it lands as
#    literal text, one row, nothing else touched
# ---------------------------------------------------------------------------


def test_injected_names_and_device_ids_are_stored_as_literal_text(client, seeded):
    """Until 2026-10-29 this also sent the payload as `type` and expected it
    stored; /register-stop now accepts only 'house' or 'business' there
    (tests/test_live_route_bounds.py), so an injected type gets the 400 any
    other wrong value gets -- checked at the end -- and the name and device
    id still land as literal text."""
    before = _snapshot()

    for index, payload in enumerate(INJECTIONS):
        created = client.post(
            "/register-stop",
            json={
                "name": payload,
                "type": "house",
                "latitude": 10.0 + index,
                "longitude": 10.0,
                "device_id": payload,
            },
        )
        assert created.status_code == 201, payload
        assert created.get_json()["name"] == payload
        assert created.get_json()["type"] == "house"

        mine = client.post("/my-stops", json={"device_id": payload}).get_json()
        assert [s["name"] for s in mine] == [payload], payload

        business = client.post(
            "/register-business",
            json={
                "name": payload,
                "address": payload,
                "latitude": 10.0 + index,
                "longitude": 10.0,
                "category": payload,
                "description": payload,
                "contact_email": payload,
                "reward_offer": payload,
                "device_id": payload,
            },
        )
        assert business.status_code == 201, payload
        assert business.get_json()["name"] == payload

    after = _snapshot()
    expected = dict(before)
    expected["stop"] += len(INJECTIONS)
    expected["business"] += len(INJECTIONS)
    assert after == expected
    assert db.session.execute(sa.select(sa.func.count()).select_from(Business.__table__)).scalar() == expected["business"]

    for payload in INJECTIONS:
        rejected = client.post(
            "/register-stop",
            json={"name": "Typed", "type": payload, "latitude": 20.0, "longitude": 20.0, "device_id": "typed"},
        )
        assert rejected.status_code == 400, payload
        assert rejected.get_json() == {"error": "type must be house or business"}, payload
    assert _snapshot() == after


def test_injected_report_reason_counts_one_report_and_nothing_else(client, seeded):
    stop_id = seeded["id"]
    before = _snapshot()

    for payload in INJECTIONS:
        response = client.post(f"/report-stop/{stop_id}", json={"reason": payload})
        assert response.status_code == 200, payload
        assert response.get_json() == {"status": "reported"}

    assert _snapshot() == before
    assert db.session.get(Stop, stop_id).report_count == len(INJECTIONS)


def test_injected_trivia_and_checkin_device_ids_create_rows_for_that_literal_id(client, seeded):
    """A new device_id IS a new household on these routes, so a row is the
    right outcome. What matters is that the row carries the literal string
    and nothing else moved."""
    stop_id = seeded["id"]
    payload = INJECTIONS[1]
    before = _snapshot()

    trivia = client.post("/trivia/answer", json={"device_id": payload, "answer": 0})
    assert trivia.status_code == 200

    checked_in = client.post(
        "/check-in", json={"device_id": payload, "stop_id": stop_id, "latitude": 42.0, "longitude": -71.0}
    )
    assert checked_in.status_code == 200, checked_in.get_json()

    after = _snapshot()
    expected = dict(before)
    expected["trivia_answer"] += 1
    expected["check_in"] += 1
    expected["household"] += 1
    assert after == expected
    assert client.get(f"/points/{_path(payload)}").get_json()["points"] > 0


# ---------------------------------------------------------------------------
# 4. stop_id, reward_id and the legend-code fields
# ---------------------------------------------------------------------------


def test_injected_stop_id_in_the_path_never_reaches_a_handler(client, seeded):
    """<int:stop_id> is matched by the router; a non-integer is a 404 from
    Werkzeug before any view code runs."""
    before = _snapshot()

    for payload in INJECTIONS:
        encoded = _path(payload)
        assert client.post(f"/report-stop/{encoded}", json={"reason": "x"}).status_code == 404, payload
        assert client.patch(f"/update-stop/{encoded}", json={"device_id": "seed-owner"}).status_code == 404, payload
        assert client.post(f"/upload-greeting/{encoded}", data={"device_id": "seed-owner"}).status_code == 404, payload

    assert _snapshot() == before


def test_injected_stop_id_in_a_checkin_body_is_a_bound_parameter(client, seeded, executed_sql):
    """/check-in takes stop_id from JSON with no type check and hands it to
    db.session.get(Stop, ...). The string is bound, not interpolated.

    OBSERVED on SQLite: no row matches a text id, so the route answers 404.
    REASONED for Postgres: psycopg2 binds the string and Postgres refuses
    to compare an integer column with 'x'); DELETE FROM stop; --' (invalid
    input syntax for type integer), which SQLAlchemy raises as DataError
    and Flask turns into a 500. Not an injection -- the statement is still
    parameterised -- but a type check on stop_id (the batch route already
    has one, via pydantic) would make the two databases agree. Not changed
    in this pass."""
    before = _snapshot()

    for payload in INJECTIONS:
        response = client.post(
            "/check-in", json={"device_id": "seed-walker", "stop_id": payload, "latitude": 42.0, "longitude": -71.0}
        )
        assert response.status_code == 404, payload
        _assert_payload_never_in_statement_text(executed_sql, payload)

    assert _snapshot() == before


def test_injected_batch_stop_id_is_rejected_by_validation(client, seeded):
    before = _snapshot()

    for payload in INJECTIONS:
        response = client.post(
            "/check-in/batch",
            json={
                "device_id": payload,
                "checkins": [
                    {"stop_id": payload, "latitude": 42.0, "longitude": -71.0, "checked_in_at": "2026-10-25T20:00:00Z"}
                ],
            },
        )
        assert response.status_code == 400, payload
        assert response.get_json()["error"] == "invalid batch payload"

    assert _snapshot() == before


def test_injected_reward_id_is_not_found(client, seeded):
    before = _snapshot()

    for payload in INJECTIONS:
        response = client.post(f"/redeem-reward/{_path(payload)}", json={"device_id": "seed-walker"})
        assert response.status_code == 404, payload
        business_shaped = client.post(f"/redeem-reward/business-{_path(payload)}", json={"device_id": "seed-walker"})
        assert business_shaped.status_code == 404, payload

    assert _snapshot() == before


def test_injected_legend_fields_are_refused_like_any_bad_code(client, seeded, executed_sql):
    before = _snapshot()

    for payload in INJECTIONS:
        with patch("app.verify_transaction") as mocked:
            mocked.side_effect = AppleVerificationError("not a real purchase")
            mint = client.post("/legend-mode/mint", json={"device_id": payload, "transaction_id": payload})
        assert mint.status_code == 403, payload

        redeem = client.post("/legend-mode/redeem", json={"code": payload})
        assert redeem.status_code == 404, payload

        _assert_payload_never_in_statement_text(executed_sql, payload)

    assert _snapshot() == before


# ---------------------------------------------------------------------------
# 5. lat / lon / radius, by GET and by POST
# ---------------------------------------------------------------------------


def test_injected_coordinates_and_radius_are_400_on_both_methods(client, seeded):
    before = _snapshot()

    for payload in INJECTIONS:
        for route in ("/nearby-stops", "/nearby-businesses"):
            encoded = _path(payload)
            as_lat_get = client.get(f"{route}?lat={encoded}&lon=-71.0")
            as_lat_post = client.post(route, json={"lat": payload, "lon": -71.0})
            as_radius_get = client.get(f"{route}?lat=42.0&lon=-71.0&radius={encoded}")
            as_radius_post = client.post(route, json={**SEARCH_ORIGIN, "radius": payload})

            assert as_lat_get.status_code == 400, (route, payload)
            assert as_lat_post.get_json() == as_lat_get.get_json(), (route, payload)
            assert as_radius_get.status_code == 400, (route, payload)
            assert as_radius_post.get_json() == as_radius_get.get_json(), (route, payload)

    assert _snapshot() == before


def test_trivia_today_query_string_device_id_is_a_bound_parameter(client, seeded, executed_sql):
    payload = INJECTIONS[0]

    response = client.get(f"/trivia/today?device_id={_path(payload)}")

    assert response.status_code == 200
    assert response.get_json()["already_answered"] is False
    _assert_payload_never_in_statement_text(executed_sql, payload)


# ---------------------------------------------------------------------------
# 6. NUL bytes
# ---------------------------------------------------------------------------


NUL = "\u0000"


def _nul_message(field):
    return f"{field} must not contain NUL characters"


def test_nul_byte_in_string_fields(client, seeded):
    """History: before 2026-10-28 this test accepted 201 or 400, because the
    two databases disagreed. OBSERVED on SQLite: the sqlite3 driver stores
    and compares a str that contains U+0000 like any other, so the
    registration landed. REASONED for Postgres: psycopg2 refuses a str with
    a NUL before the query is sent ("A string literal cannot contain NUL
    (0x00) characters"), so /register-stop 500'd there (seen in production
    2026-10-26). Nothing was ever executed as SQL either way. app.py now
    rejects a NUL in every request string before the first query, so both
    databases answer 400 and this test asserts exactly that."""
    before = _snapshot()
    name = f"Seed{NUL}House"
    device_id = f"nul{NUL}device"

    created = client.post(
        "/register-stop",
        json={"name": name, "type": "house", "latitude": 11.0, "longitude": 11.0, "device_id": device_id},
    )
    assert created.status_code == 400, created.status_code
    assert created.get_json() == {"error": _nul_message("name")}

    mine = client.post("/my-stops", json={"device_id": device_id})
    assert mine.status_code == 400
    assert mine.get_json() == {"error": _nul_message("device_id")}

    assert _snapshot() == before


def test_nul_in_each_register_stop_field_is_400_and_writes_nothing(client, seeded):
    before = _snapshot()
    clean = {"name": "Clean", "type": "house", "latitude": 12.0, "longitude": 12.0, "device_id": "clean-device"}

    for field in ("name", "type", "device_id"):
        response = client.post("/register-stop", json={**clean, field: f"a{NUL}b"})
        assert response.status_code == 400, field
        assert response.get_json() == {"error": _nul_message(field)}, field

    assert _snapshot() == before


def test_nul_in_each_register_business_field_is_400_and_writes_nothing(client, seeded):
    before = _snapshot()
    clean = {
        "name": "Clean Shop",
        "address": "1 Clean St",
        "latitude": 12.0,
        "longitude": 12.0,
        "category": "shop",
        "description": "A shop",
        "contact_email": "owner@example.com",
        "reward_offer": "Free thing",
        "device_id": "clean-business",
    }

    for field in ("name", "address", "category", "description", "contact_email", "reward_offer", "device_id"):
        response = client.post("/register-business", json={**clean, field: f"a{NUL}b"})
        assert response.status_code == 400, field
        assert response.get_json() == {"error": _nul_message(field)}, field

    assert _snapshot() == before


def test_nul_in_report_reason_is_400_and_counts_no_report(client, seeded):
    stop_id = seeded["id"]
    before = _snapshot()

    response = client.post(f"/report-stop/{stop_id}", json={"reason": f"spam{NUL}"})

    assert response.status_code == 400
    assert response.get_json() == {"error": _nul_message("reason")}
    assert _snapshot() == before
    assert db.session.get(Stop, stop_id).report_count == 0


def test_nul_in_a_body_device_id_is_400_on_every_route_that_takes_one(client, seeded):
    """Every JSON-body route that reads device_id, including the two whose
    lookups would otherwise write a row for a brand-new id (trivia and
    check-in) and the one that compares in Python rather than SQL
    (update-stop, kept consistent with its siblings on purpose)."""
    stop_id = seeded["id"]
    device_id = f"nul{NUL}device"
    before = _snapshot()
    expected = {"error": _nul_message("device_id")}

    assert client.post("/my-stops", json={"device_id": device_id}).get_json() == expected
    assert client.patch(f"/update-stop/{stop_id}", json={"device_id": device_id, "candy_count": 1}).get_json() == expected
    assert (
        client.post("/check-in", json={"device_id": device_id, "stop_id": stop_id, "latitude": 42.0, "longitude": -71.0}).get_json()
        == expected
    )
    assert client.post("/trivia/answer", json={"device_id": device_id, "answer": 0}).get_json() == expected
    assert client.post("/redeem-reward/founder-1", json={"device_id": device_id}).get_json() == expected

    with patch("app.verify_transaction") as mocked:
        mocked.return_value = None
        mint = client.post("/legend-mode/mint", json={"device_id": device_id, "transaction_id": "clean-txn"})
        assert mint.get_json() == expected
        mint = client.post("/legend-mode/mint", json={"device_id": "clean-legend", "transaction_id": f"txn{NUL}"})
        assert mint.get_json() == {"error": _nul_message("transaction_id")}
        assert mocked.call_count == 0, "a NUL must be refused before Apple is asked anything"

    for route, payload in (
        ("/my-stops", {"device_id": device_id}),
        ("/check-in", {"device_id": device_id, "stop_id": stop_id, "latitude": 42.0, "longitude": -71.0}),
        ("/trivia/answer", {"device_id": device_id, "answer": 0}),
        ("/redeem-reward/founder-1", {"device_id": device_id}),
    ):
        assert client.post(route, json=payload).status_code == 400, route
    assert client.patch(f"/update-stop/{stop_id}", json={"device_id": device_id, "candy_count": 1}).status_code == 400

    assert _snapshot() == before
    assert db.session.get(Stop, stop_id).candy_count is None


def test_nul_in_the_batch_device_id_is_rejected_by_validation(client, seeded):
    before = _snapshot()

    response = client.post(
        "/check-in/batch",
        json={
            "device_id": f"nul{NUL}device",
            "checkins": [
                {"stop_id": seeded["id"], "latitude": 42.0, "longitude": -71.0, "checked_in_at": "2026-10-25T20:00:00Z"}
            ],
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "invalid batch payload"
    assert any(_nul_message("device_id") in item["msg"] for item in body["detail"]), body["detail"]
    assert _snapshot() == before


def test_nul_in_the_form_device_id_is_400_before_the_file_is_read(client, seeded):
    before = _snapshot()

    response = client.post(f"/upload-greeting/{seeded['id']}", data={"device_id": f"nul{NUL}device"})

    assert response.status_code == 400
    assert response.get_json() == {"error": _nul_message("device_id")}
    assert _snapshot() == before


def test_nul_in_a_path_or_query_device_id_is_400(client, seeded):
    """Werkzeug decodes %00 in a path segment to U+0000 and hands it to the
    view, so these four GET routes (and trivia/today's query string) would
    have sent the NUL to the database."""
    encoded = _path(f"nul{NUL}device")
    before = _snapshot()
    expected = {"error": _nul_message("device_id")}

    for route in (f"/my-stops/{encoded}", f"/night-ledger/{encoded}", f"/redemptions/{encoded}", f"/points/{encoded}"):
        response = client.get(route)
        assert response.status_code == 400, route
        assert response.get_json() == expected, route

    trivia = client.get(f"/trivia/today?device_id={encoded}")
    assert trivia.status_code == 400
    assert trivia.get_json() == expected

    assert _snapshot() == before


def test_nul_rejection_never_reaches_the_engine(client, seeded, executed_sql):
    """The whole point: the 500 was the driver refusing the parameter, so the
    fix has to stop the request BEFORE a statement is issued with it."""
    device_id = f"nul{NUL}device"

    # Positive control first: a clean id on the same route DOES show up as a
    # bound parameter, so an empty list below means "refused before the
    # query", not "the listener saw nothing".
    client.post("/my-stops", json={"device_id": "seed-owner"})
    assert any("seed-owner" in repr(p) for _, p in executed_sql)

    client.post("/register-stop", json={"name": "x", "type": "house", "latitude": 1.0, "longitude": 1.0, "device_id": device_id})
    client.post("/my-stops", json={"device_id": device_id})
    client.get(f"/points/{_path(device_id)}")

    # repr() of a str holding U+0000 spells it as the four characters \x00,
    # so that is the needle; the raw character would never match.
    bound = [p for _, p in executed_sql if "\\x00" in repr(p)]
    assert bound == [], bound


# ---------------------------------------------------------------------------
# 7. guard: no raw SQL outside the migration function
# ---------------------------------------------------------------------------


def test_app_py_has_no_raw_sql_outside_the_migration_function():
    """Parses app.py and allows text(...) and Connection.execute(...) only
    inside _run_lightweight_migrations, and exec/eval nowhere. AST-based,
    so comments and docstrings that mention text( do not trip it. It does
    not inspect the strings themselves; that is what the tests above are
    for."""
    source_path = os.path.join(os.path.dirname(app_module.__file__), "app.py")
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    migration = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_lightweight_migrations"
    )
    allowed = range(migration.lineno, migration.end_lineno + 1)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name in ("exec", "eval"):
            offenders.append((node.lineno, name))
        elif name in ("text", "execute") and node.lineno not in allowed:
            offenders.append((node.lineno, name))

    assert offenders == [], offenders
