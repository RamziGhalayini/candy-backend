import io
from unittest.mock import MagicMock

from botocore.exceptions import BotoCoreError

import app as app_module
from app import Stop, db


def _register_stop(client, **overrides):
    payload = {
        "name": "Greeting House",
        "type": "house",
        "latitude": 42.0,
        "longitude": -71.0,
        "device_id": "greeter-1",
    }
    payload.update(overrides)
    response = client.post("/register-stop", json=payload)
    return response.get_json()["id"]


def _audio_file(name="greeting.m4a", content_type="audio/m4a", body=b"fake-audio-bytes"):
    return {"audio": (io.BytesIO(body), name, content_type)}


def test_upload_greeting_succeeds_and_sets_url(client, monkeypatch):
    stop_id = _register_stop(client)
    mock_r2 = MagicMock()
    monkeypatch.setattr(app_module, "r2_client", mock_r2)

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["greeting_audio_url"].startswith(app_module.R2_PUBLIC_BASE_URL + "/greetings/")

    mock_r2.put_object.assert_called_once()
    call_kwargs = mock_r2.put_object.call_args.kwargs
    assert call_kwargs["Bucket"] == app_module.R2_BUCKET_NAME
    assert call_kwargs["ContentType"] == "audio/m4a"
    assert call_kwargs["Body"] == b"fake-audio-bytes"

    stored = db.session.get(Stop, stop_id)
    assert stored.greeting_audio_url == body["greeting_audio_url"]


def test_upload_greeting_rejects_non_audio_file(client, monkeypatch):
    stop_id = _register_stop(client)
    monkeypatch.setattr(app_module, "r2_client", MagicMock())

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(name="notes.txt", content_type="text/plain"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "audio" in response.get_json()["error"]


def test_upload_greeting_rejects_oversized_file(client, monkeypatch):
    stop_id = _register_stop(client)
    monkeypatch.setattr(app_module, "r2_client", MagicMock())

    big_body = b"a" * (2 * 1024 * 1024 + 1)
    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(body=big_body),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "MB" in response.get_json()["error"]


def test_upload_greeting_rejects_empty_file(client, monkeypatch):
    stop_id = _register_stop(client)
    monkeypatch.setattr(app_module, "r2_client", MagicMock())

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(body=b""),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "empty" in response.get_json()["error"]


def test_upload_greeting_requires_a_file(client, monkeypatch):
    stop_id = _register_stop(client)
    monkeypatch.setattr(app_module, "r2_client", MagicMock())

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data={},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400


def test_upload_greeting_missing_stop_returns_404(client, monkeypatch):
    monkeypatch.setattr(app_module, "r2_client", MagicMock())

    response = client.post(
        "/upload-greeting/999999",
        data=_audio_file(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 404


def test_upload_greeting_returns_502_when_r2_upload_fails(client, monkeypatch):
    stop_id = _register_stop(client)
    mock_r2 = MagicMock()
    mock_r2.put_object.side_effect = BotoCoreError()
    monkeypatch.setattr(app_module, "r2_client", mock_r2)

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 502
    stored = db.session.get(Stop, stop_id)
    assert stored.greeting_audio_url is None


def test_upload_greeting_not_configured_returns_500(client, monkeypatch):
    stop_id = _register_stop(client)
    monkeypatch.setattr(app_module, "r2_client", None)

    response = client.post(
        f"/upload-greeting/{stop_id}",
        data=_audio_file(),
        content_type="multipart/form-data",
    )

    assert response.status_code == 500
