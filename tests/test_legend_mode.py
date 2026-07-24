from unittest.mock import patch

from app import LegendUnlock, db
from apple_verification import AppleVerificationConfigError, AppleVerificationError
from legend_token import generate_legend_code, hash_code

# Real transaction verification is mocked throughout -- there's no live
# Apple credential/network dependency in these tests, matching this
# project's existing "everything gets built and unit-tested" plan. A fake
# JWSTransactionDecodedPayload-shaped return isn't needed since
# mint_legend_code only checks that verify_transaction didn't raise; it
# never reads a return value from it.


def _mint(client, device_id="device-1", transaction_id="txn-1"):
    with patch("app.verify_transaction") as mocked:
        mocked.return_value = None
        return client.post(
            "/legend-mode/mint", json={"device_id": device_id, "transaction_id": transaction_id}
        )


def test_mint_creates_a_code(client):
    response = _mint(client)

    assert response.status_code == 200
    body = response.get_json()
    assert body["code"].startswith("LM-")
    assert LegendUnlock.query.filter_by(device_id="device-1").count() == 1


def test_mint_requires_device_id(client):
    response = client.post("/legend-mode/mint", json={"transaction_id": "txn-1"})

    assert response.status_code == 400


def test_mint_requires_transaction_id(client):
    response = client.post("/legend-mode/mint", json={"device_id": "device-1"})

    assert response.status_code == 400


def test_mint_is_idempotent_per_device(client):
    first = _mint(client)
    second = _mint(client)

    assert first.get_json()["code"] == second.get_json()["code"]
    assert LegendUnlock.query.filter_by(device_id="device-1").count() == 1


def test_mint_code_matches_deterministic_token_engine(client):
    response = _mint(client, device_id="device-1", transaction_id="txn-1")

    expected = generate_legend_code("device-1", "txn-1")
    assert response.get_json()["code"] == expected


def test_mint_never_stores_the_raw_code(client):
    response = _mint(client)
    code = response.get_json()["code"]

    unlock = LegendUnlock.query.filter_by(device_id="device-1").first()
    assert unlock.code != code
    assert unlock.code == hash_code(code)
    assert unlock.code_hash == hash_code(code)


def test_mint_rejects_a_reused_transaction_id_for_a_different_device(client):
    _mint(client, device_id="device-1", transaction_id="shared-txn")
    response = _mint(client, device_id="device-2", transaction_id="shared-txn")

    assert response.status_code == 409
    assert LegendUnlock.query.filter_by(device_id="device-2").count() == 0


def test_mint_returns_403_when_apple_rejects_the_transaction(client):
    with patch("app.verify_transaction") as mocked:
        mocked.side_effect = AppleVerificationError("revoked")
        response = client.post(
            "/legend-mode/mint", json={"device_id": "device-1", "transaction_id": "txn-1"}
        )

    assert response.status_code == 403
    assert LegendUnlock.query.filter_by(device_id="device-1").count() == 0


def test_mint_returns_500_when_apple_credentials_are_not_configured(client):
    with patch("app.verify_transaction") as mocked:
        mocked.side_effect = AppleVerificationConfigError("Missing required Apple App Store Server API env var(s): APPLE_ISSUER_ID")
        response = client.post(
            "/legend-mode/mint", json={"device_id": "device-1", "transaction_id": "txn-1"}
        )

    assert response.status_code == 500
    assert LegendUnlock.query.filter_by(device_id="device-1").count() == 0


def test_redeem_accepts_a_valid_code(client):
    minted = _mint(client)
    code = minted.get_json()["code"]

    response = client.post("/legend-mode/redeem", json={"code": code})

    assert response.status_code == 200
    assert response.get_json() == {"legend_mode_active": True}


def test_redeem_is_case_and_whitespace_insensitive(client):
    minted = _mint(client)
    code = minted.get_json()["code"]

    response = client.post("/legend-mode/redeem", json={"code": f"  {code.lower()}  "})

    assert response.status_code == 200


def test_redeem_rejects_an_unknown_code(client):
    response = client.post("/legend-mode/redeem", json={"code": "LM-DEADBEEF"})

    assert response.status_code == 404


def test_redeem_requires_a_code(client):
    response = client.post("/legend-mode/redeem", json={})

    assert response.status_code == 400


def test_redeem_honors_a_grandfathered_pre_migration_code(client):
    """Simulates a row minted before this change: raw `code` stored
    directly (no transaction_id), exactly like the old mint_legend_code
    used to write. _run_lightweight_migrations (already run once by the
    db_session fixture before this test body executes) only backfills
    code_hash for rows that existed at THAT point -- so this row is
    inserted after that pass and needs its own code_hash set explicitly to
    accurately simulate "a row that predates this deploy, already
    backfilled," rather than a row created after migrations already ran."""
    legacy_code = "LM-DEADBEEF"
    db.session.add(
        LegendUnlock(
            device_id="legacy-device",
            code=legacy_code,
            transaction_id=None,
            code_hash=hash_code(legacy_code),
        )
    )
    db.session.commit()

    response = client.post("/legend-mode/redeem", json={"code": legacy_code})

    assert response.status_code == 200
    assert response.get_json() == {"legend_mode_active": True}
