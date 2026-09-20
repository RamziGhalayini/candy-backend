"""Guards for the 2026-09-16 hardening pass.

Each test here was written to FAIL against the code as it stood before that
pass. They are the reason to believe the fixes do what the commit says.
"""

import io
import uuid

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def _clear_rate_buckets():
    """The limiter is process-global, so one test's calls would otherwise
    count against the next one's budget."""
    app_module._RATE_BUCKETS.clear()
    yield
    app_module._RATE_BUCKETS.clear()


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


# --------------------------------------------------------------------------
# 2. /register-stop requires an owner
# --------------------------------------------------------------------------

def test_register_stop_rejects_missing_device_id(client):
    response = client.post(
        "/register-stop",
        json={"name": "No Owner", "type": "house", "latitude": 28.75, "longitude": -81.33},
    )
    assert response.status_code == 400
    assert "device_id" in response.get_json()["error"]


def test_register_stop_rejects_blank_device_id(client):
    response = client.post(
        "/register-stop",
        json={
            "name": "No Owner",
            "type": "house",
            "latitude": 28.75,
            "longitude": -81.33,
            "device_id": "   ",
        },
    )
    assert response.status_code == 400


def test_register_stop_still_works_with_a_device_id(client):
    response = _register(client)
    assert response.status_code == 201
    assert response.get_json()["name"] == "Test House"


def test_registered_stop_always_has_an_owner(client):
    """The point of the requirement: no route can now produce a stop that
    nobody can ever edit, correct, or attach a greeting to."""
    stop_id = _register(client, device_id="  dev-with-space  ").get_json()["id"]
    with app_module.app.app_context():
        stop = app_module.db.session.get(app_module.Stop, stop_id)
        assert stop.registrant_device_id == "dev-with-space"


# --------------------------------------------------------------------------
# 5. CheckIn retention
# --------------------------------------------------------------------------

def test_expired_check_ins_are_purged_on_the_next_write(client):
    from datetime import date, timedelta

    with app_module.app.app_context():
        stale = app_module.CheckIn(
            device_id="ancient-device",
            stop_id=1,
            check_in_date=date.today() - timedelta(days=app_module.CHECKIN_RETENTION_DAYS + 5),
        )
        fresh = app_module.CheckIn(
            device_id="recent-device",
            stop_id=1,
            check_in_date=date.today() - timedelta(days=1),
        )
        app_module.db.session.add_all([stale, fresh])
        app_module.db.session.commit()
        assert app_module.CheckIn.query.count() == 2

        app_module._purge_expired_check_ins()
        app_module.db.session.commit()

        remaining = app_module.CheckIn.query.all()
        assert len(remaining) == 1
        assert remaining[0].device_id == "recent-device"


def test_a_failing_purge_does_not_discard_the_check_in(client, monkeypatch):
    """A failure in retention must never cost a child their check-in.

    The purge runs inside the CALLER'S transaction, after the CheckIn row,
    the points award and the candy_count decrement have been staged and
    before the commit. So the property that matters is not "the purge
    swallowed the error" -- it is "the check-in still landed".

    This test previously asserted only that _purge_expired_check_ins() did
    not raise. An `except Exception: db.session.rollback()` satisfies that
    perfectly while discarding everything the caller had staged, so the
    route returned {"status": "accepted", "points_awarded": 5} having
    persisted nothing at all. That is precisely the bug this test existed to
    catch, and it sailed through. Assert the outcome, not the absence of an
    exception.

    Driven through _apply_check_in rather than calling the purge directly,
    because the bug only exists in relation to the transaction the purge is
    borrowing -- in isolation there is nothing to lose.
    """
    from datetime import date

    import sqlalchemy.orm

    owner = "purge-owner"
    stop_id = _register(
        client, device_id=owner, lat=28.7700, lon=-81.3400, name="Purge House"
    ).get_json()["id"]
    # A neighbouring registration is what marks the stop verified; check-ins
    # against an unverified stop are rejected before the purge is ever reached.
    _register(
        client, device_id="a-neighbour", lat=28.77002, lon=-81.34002, name="Next Door"
    )

    def _failing_delete(self, *args, **kwargs):
        raise RuntimeError("DELETE failed")

    with app_module.app.app_context():
        stop = app_module.db.session.get(app_module.Stop, stop_id)
        stop.candy_count = 10
        app_module.db.session.commit()

        # The purge holds the only .delete() call in app.py, so failing it
        # here fails exactly one statement and nothing else in the path.
        monkeypatch.setattr(sqlalchemy.orm.Query, "delete", _failing_delete)

        result = app_module._apply_check_in(
            "trick-or-treater", stop_id, 28.7700, -81.3400, date.today()
        )

        assert result["status"] == "accepted"

        # Everything the route claimed it did must actually be on disk.
        assert (
            app_module.CheckIn.query.filter_by(device_id="trick-or-treater").count() == 1
        ), "the check-in was rolled back by the failing purge"

        household = app_module.Household.query.filter_by(
            device_id="trick-or-treater"
        ).first()
        assert household is not None, "the points award was rolled back"
        assert household.points == app_module.CHECKIN_POINTS

        assert (
            app_module.db.session.get(app_module.Stop, stop_id).candy_count == 9
        ), "the candy_count decrement was rolled back"


# --------------------------------------------------------------------------
# 6. /my-stops still reports verification correctly after the N+1 fix
# --------------------------------------------------------------------------

def test_my_stops_reports_verified_using_a_neighbour_not_owned_by_the_caller(client):
    """The universe for verification is geographic, not per-device. If the fix
    had filtered by owner, an owner's genuinely verified stop would report as
    unverified -- and check-ins would then be rejected at that address."""
    owner = "owner-device"
    _register(client, device_id=owner, lat=28.7600, lon=-81.3300, name="Mine")
    _register(client, device_id="a-neighbour", lat=28.76002, lon=-81.33002, name="Theirs")

    rows = client.get(f"/my-stops/{owner}").get_json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Mine"
    assert rows[0]["verified"] is True


def test_my_stops_empty_for_unknown_device(client):
    assert client.get("/my-stops/nobody-here").get_json() == []


# --------------------------------------------------------------------------
# 3. greeting uploads are checked by content, not by caller-chosen strings
# --------------------------------------------------------------------------

_M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64
_NOT_AUDIO = b"<!DOCTYPE html><html><body>not audio at all</body></html>" + b"\x00" * 64


def _upload(client, stop_id, payload, filename="greeting.m4a", content_type="audio/m4a"):
    return client.post(
        f"/upload-greeting/{stop_id}",
        data={
            "device_id": "dev-owner",
            "audio": (io.BytesIO(payload), filename, content_type),
        },
        content_type="multipart/form-data",
    )


def test_upload_rejects_non_audio_bytes_even_with_a_valid_name_and_type(client, monkeypatch):
    """The exact attack: correct extension, correct content-type, junk inside."""
    stop_id = _register(client).get_json()["id"]

    class _FakeR2:
        def put_object(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("non-audio content reached object storage")

    monkeypatch.setattr(app_module, "r2_client", _FakeR2())

    response = _upload(client, stop_id, _NOT_AUDIO)
    assert response.status_code == 400
    assert "audio" in response.get_json()["error"]


def test_upload_accepts_a_real_m4a_container(client, monkeypatch):
    stop_id = _register(client).get_json()["id"]

    captured = {}

    class _FakeR2:
        def put_object(self, **kwargs):
            captured.update(kwargs)
            return {}

    monkeypatch.setattr(app_module, "r2_client", _FakeR2())

    response = _upload(client, stop_id, _M4A)
    assert response.status_code == 200, response.get_json()
    assert captured["Body"] == _M4A


def test_looks_like_audio_signatures():
    ok = [
        b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 8,
        b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00",
        b"\xff\xfb\x90\x00" + b"\x00" * 8,
        b"RIFF\x24\x08\x00\x00WAVE",
        b"OggS\x00\x02\x00\x00\x00\x00\x00\x00",
        b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x00",
    ]
    for payload in ok:
        assert app_module._looks_like_audio(payload), payload[:8]

    bad = [
        b"",
        b"short",
        b"<!DOCTYPE html><html>",
        b"%PDF-1.7\x00\x00\x00\x00",
        b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00",
    ]
    for payload in bad:
        assert not app_module._looks_like_audio(payload), payload[:8]


# --------------------------------------------------------------------------
# 4. per-IP rate limiting
# --------------------------------------------------------------------------

def test_report_stop_is_rate_limited_per_ip(client):
    stop_id = _register(client).get_json()["id"]

    max_calls, _window = app_module._RATE_LIMITS["report_stop"]
    headers = {"X-Forwarded-For": "203.0.113.10"}

    for _ in range(max_calls):
        client.post(f"/report-stop/{stop_id}", json={"reason": "x"}, headers=headers)

    blocked = client.post(f"/report-stop/{stop_id}", json={"reason": "x"}, headers=headers)
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")


def test_rate_limit_is_scoped_to_the_caller_not_global(client):
    """A second address must not inherit the first one's exhausted budget."""
    stop_id = _register(client).get_json()["id"]
    max_calls, _window = app_module._RATE_LIMITS["report_stop"]

    for _ in range(max_calls):
        client.post(
            f"/report-stop/{stop_id}",
            json={"reason": "x"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )

    other = client.post(
        f"/report-stop/{stop_id}",
        json={"reason": "x"},
        headers={"X-Forwarded-For": "198.51.100.77"},
    )
    assert other.status_code != 429


def test_rate_limit_is_scoped_per_endpoint(client):
    """Exhausting one route must not lock a caller out of an unrelated one."""
    max_calls, _window = app_module._RATE_LIMITS["register_stop"]
    headers = {"X-Forwarded-For": "203.0.113.20"}

    for i in range(max_calls):
        client.post(
            "/register-stop",
            json={
                "name": f"H{i}",
                "type": "house",
                "latitude": 28.75,
                "longitude": -81.33,
                "device_id": f"dev-{uuid.uuid4().hex}",
            },
            headers=headers,
        )

    assert client.get("/nearby-stops?lat=28.75&lon=-81.33&radius=1", headers=headers).status_code == 200


def test_unlisted_endpoints_are_not_limited(client):
    headers = {"X-Forwarded-For": "203.0.113.30"}
    for _ in range(50):
        assert client.get("/version", headers=headers).status_code == 200


def test_limiter_fails_open_if_it_raises(client, monkeypatch):
    """A bug in the limiter must never take the app down."""
    def _boom():
        raise RuntimeError("limiter exploded")

    monkeypatch.setattr(app_module, "_client_ip", _boom)
    assert client.get("/nearby-stops?lat=28.75&lon=-81.33&radius=1").status_code == 200
