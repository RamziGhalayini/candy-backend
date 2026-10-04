"""verify_transaction's environment fall-through. Everything below the
function -- config, Apple client, signature verifier -- is replaced with
fakes so no credential or network is involved."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from appstoreserverlibrary.api_client import APIException
from appstoreserverlibrary.models.Environment import Environment

import apple_verification
from apple_verification import AppleVerificationError, verify_transaction

PRODUCT_ID = "com.treatmapapp.candystops.legendmode"
TXN = "txn-1"
NOT_FOUND = APIException(404, 4040010, "Transaction id not found.")
# Apple's production host answers a bare 401 with no JSON body, which the
# library surfaces as APIException(401) with no api_error (OBSERVED 2026-10-04).
BARE_401 = APIException(401)


class _FakeClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def get_transaction_info(self, transaction_id):
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return SimpleNamespace(signedTransactionInfo="signed-jws")


class _FakeVerifier:
    def verify_and_decode_signed_transaction(self, signed):
        return SimpleNamespace(productId=PRODUCT_ID, revocationDate=None)


@pytest.fixture
def hosts():
    """Yields a dict the test fills with per-environment fake clients, and
    patches the module so verify_transaction talks only to those fakes."""
    clients = {}

    def client_for(environment, config):
        if environment not in clients:
            raise AssertionError(f"unexpected call to {environment.value} host")
        return clients[environment]

    with patch.object(apple_verification, "_read_config", return_value={}), patch.object(
        apple_verification, "_client_for", side_effect=client_for
    ), patch.object(apple_verification, "_verifier_for", return_value=_FakeVerifier()):
        yield clients


def test_production_401_falls_through_to_sandbox_and_succeeds(hosts, caplog):
    hosts[Environment.PRODUCTION] = _FakeClient(BARE_401)
    hosts[Environment.SANDBOX] = _FakeClient("ok")

    with caplog.at_level(logging.WARNING, logger="apple_verification"):
        payload = verify_transaction(TXN, PRODUCT_ID)

    assert payload.productId == PRODUCT_ID
    assert hosts[Environment.PRODUCTION].calls == 1
    assert hosts[Environment.SANDBOX].calls == 1
    assert any(
        record.levelno == logging.WARNING and "401" in record.getMessage() and "sandbox" in record.getMessage()
        for record in caplog.records
    )


def test_production_401_then_sandbox_401_is_a_hard_failure(hosts):
    hosts[Environment.PRODUCTION] = _FakeClient(BARE_401)
    hosts[Environment.SANDBOX] = _FakeClient(BARE_401)

    with pytest.raises(AppleVerificationError, match="401"):
        verify_transaction(TXN, PRODUCT_ID)

    assert hosts[Environment.SANDBOX].calls == 1


def test_production_200_never_calls_sandbox(hosts):
    hosts[Environment.PRODUCTION] = _FakeClient("ok")
    # No sandbox entry: _client_for raises AssertionError if it's ever asked.

    payload = verify_transaction(TXN, PRODUCT_ID)

    assert payload.productId == PRODUCT_ID
    assert hosts[Environment.PRODUCTION].calls == 1


def test_sandbox_401_after_production_not_found_is_a_hard_failure(hosts):
    hosts[Environment.PRODUCTION] = _FakeClient(NOT_FOUND)
    hosts[Environment.SANDBOX] = _FakeClient(BARE_401)

    with pytest.raises(AppleVerificationError, match="401"):
        verify_transaction(TXN, PRODUCT_ID)


def test_production_not_found_still_falls_through_to_sandbox(hosts):
    hosts[Environment.PRODUCTION] = _FakeClient(NOT_FOUND)
    hosts[Environment.SANDBOX] = _FakeClient("ok")

    payload = verify_transaction(TXN, PRODUCT_ID)

    assert payload.productId == PRODUCT_ID
