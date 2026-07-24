"""Apple App Store Server API verification for Legend Mode purchases.

Uses Apple's own official app-store-server-library (NOT hand-rolled JWT/JWS
crypto) -- every name/signature below was verified directly against the
INSTALLED package source (venv/Lib/site-packages/appstoreserverlibrary),
not just Apple's published docs, which turned out to disagree with the
real 3.1.2 source on two points: APIError lives in api_client, not a
separate models.APIError module, and the client constructor's private-key
parameter is named signing_key, not private_key.

Credentials are read from environment variables, matching this app's
existing DATABASE_URL pattern (never committed; set as real secrets on
Render):

  APPLE_ISSUER_ID     -- Issuer ID from App Store Connect (Users and
                         Access > Integrations > App Store Server API)
  APPLE_KEY_ID        -- Key ID for the generated App Store Server API key
  APPLE_PRIVATE_KEY   -- The downloaded .p8 private key's contents (PEM text)
  APPLE_BUNDLE_ID     -- This app's bundle id
  APPLE_APP_APPLE_ID  -- Numeric App Store app id. Required by the library
                         whenever verifying against Environment.PRODUCTION
                         (SignedDataVerifier raises ValueError without it);
                         not needed for sandbox-only verification.
  APPLE_ROOT_CERT_DIR -- OPTIONAL. Directory containing Apple's root CA
                         certificate file(s) (.cer, DER-encoded), downloaded
                         from https://www.apple.com/certificateauthority/.
                         These are PUBLIC certificates, not secrets -- unlike
                         everything else in this list, they're meant to be
                         committed to the repo (in ./certs, next to this
                         file) so they deploy automatically with the code,
                         the same way Render deploys everything else here
                         from git. Defaults to a `certs` directory next to
                         THIS file (resolved via __file__, matching how
                         app.py's own SQLite fallback path is computed) --
                         only set this env var to point somewhere else.
  LEGEND_CODE_PEPPER  -- Server-side secret pepper for legend_token's HMAC.
                         Not an Apple credential -- generate this yourself
                         (e.g. `python -c "import secrets; print(secrets.
                         token_hex(32))"`), it's this app's own secret.

None of the above exist in this environment yet. Calling verify_transaction
before they're set raises AppleVerificationConfigError naming exactly which
variable is missing, rather than a confusing crash deep inside the Apple
library or a silent wrong answer.
"""

import glob
import os

from appstoreserverlibrary.api_client import APIError, APIException, AppStoreServerAPIClient
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import JWSTransactionDecodedPayload
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier, VerificationException

_REQUIRED_ENV_VARS = ("APPLE_ISSUER_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY", "APPLE_BUNDLE_ID")


class AppleVerificationConfigError(RuntimeError):
    """Raised when a required Apple credential/env var isn't set yet."""


class AppleVerificationError(RuntimeError):
    """Raised when a transaction can't be verified as a genuine, matching,
    non-revoked Legend Mode purchase -- covers Apple-side rejection,
    signature/chain verification failure, and 'valid but wrong product'."""


def _read_config():
    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise AppleVerificationConfigError(
            "Missing required Apple App Store Server API env var(s): " + ", ".join(missing)
        )
    app_apple_id_raw = os.environ.get("APPLE_APP_APPLE_ID")
    return {
        "issuer_id": os.environ["APPLE_ISSUER_ID"],
        "key_id": os.environ["APPLE_KEY_ID"],
        # signing_key is the real parameter name the installed library
        # uses (AppStoreServerAPIClient.__init__ -> BaseAppStoreServerAPIClient
        # .__init__), confirmed against source -- Apple's own docs call
        # this "private_key", which does NOT match the real signature.
        "signing_key": os.environ["APPLE_PRIVATE_KEY"].encode(),
        "bundle_id": os.environ["APPLE_BUNDLE_ID"],
        "app_apple_id": int(app_apple_id_raw) if app_apple_id_raw else None,
    }


_DEFAULT_CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")


def _load_root_certificates():
    # Defaults to ./certs next to this file -- committed to the repo (public
    # certs, not secrets), so this "just works" on Render without needing
    # APPLE_ROOT_CERT_DIR set at all, the same way app.py's SQLite fallback
    # needs no env var either. Only set the env var to override this.
    cert_dir = os.environ.get("APPLE_ROOT_CERT_DIR") or _DEFAULT_CERT_DIR
    cert_paths = sorted(glob.glob(os.path.join(cert_dir, "*.cer")))
    if not cert_paths:
        raise AppleVerificationConfigError(
            f"APPLE_ROOT_CERT_DIR ({cert_dir!r}) contains no .cer files -- download Apple's "
            "current root certificate(s) from https://www.apple.com/certificateauthority/ "
            "and place them there."
        )
    certificates = []
    for path in cert_paths:
        with open(path, "rb") as cert_file:
            certificates.append(cert_file.read())
    return certificates


def _client_for(environment, config):
    return AppStoreServerAPIClient(
        config["signing_key"],
        config["key_id"],
        config["issuer_id"],
        config["bundle_id"],
        environment,
    )


def _verifier_for(environment, config):
    root_certificates = _load_root_certificates()
    return SignedDataVerifier(
        root_certificates,
        True,  # enable_online_checks
        environment,
        config["bundle_id"],
        config["app_apple_id"],
    )


def verify_transaction(transaction_id: str, expected_product_id: str) -> JWSTransactionDecodedPayload:
    """Verifies `transaction_id` against Apple's servers and returns the
    decoded JWSTransactionDecodedPayload if it's a genuine, non-revoked
    purchase of `expected_product_id`.

    Tries PRODUCTION first, falls back to SANDBOX only on Apple's specific
    "transaction not found in this environment" signal (APIError.
    TRANSACTION_ID_NOT_FOUND with http_status_code 404) -- Apple's own
    documented pattern for supporting sandbox/TestFlight purchases against
    a production-configured server.

    bundleId and environment are already validated by SignedDataVerifier
    itself (confirmed in its source: verify_and_decode_signed_transaction
    raises VerificationException on either mismatch) -- this function only
    adds the two checks the library does NOT do: does the product id match
    what Legend Mode expects, and has the transaction been revoked/refunded.

    Raises AppleVerificationConfigError if credentials aren't set up yet,
    or AppleVerificationError for any invalid/revoked/mismatched/unfindable
    transaction.
    """
    config = _read_config()  # raise early, before any network call

    last_error = None
    for environment in (Environment.PRODUCTION, Environment.SANDBOX):
        client = _client_for(environment, config)
        try:
            response = client.get_transaction_info(transaction_id)
        except APIException as error:
            if error.api_error == APIError.TRANSACTION_ID_NOT_FOUND and error.http_status_code == 404:
                last_error = error
                continue
            raise AppleVerificationError(f"Apple API error verifying transaction: {error}") from error

        if not response.signedTransactionInfo:
            raise AppleVerificationError("Apple returned no signed transaction info")

        verifier = _verifier_for(environment, config)
        try:
            payload = verifier.verify_and_decode_signed_transaction(response.signedTransactionInfo)
        except VerificationException as error:
            raise AppleVerificationError(f"Signature verification failed: {error}") from error

        if payload.productId != expected_product_id:
            raise AppleVerificationError(
                f"Transaction product id {payload.productId!r} does not match "
                f"the expected product ({expected_product_id!r})"
            )
        if payload.revocationDate is not None:
            raise AppleVerificationError("This transaction has been revoked/refunded")

        return payload

    raise AppleVerificationError(
        f"Transaction {transaction_id!r} was not found in production or sandbox"
    ) from last_error
