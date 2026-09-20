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
