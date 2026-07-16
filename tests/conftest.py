import os
import shutil
import sys
import tempfile

# Point the app at a throwaway copy of the real local candy.db (never the
# live file) before app.py is imported anywhere, since app.py connects and
# runs its migrations at import time.
_here = os.path.abspath(os.path.dirname(__file__))
_repo_root = os.path.dirname(_here)
sys.path.insert(0, _repo_root)

_tmp_dir = tempfile.mkdtemp(prefix="candy_backend_test_")
_test_db_path = os.path.join(_tmp_dir, "test_copy.db")

_source_db = os.path.join(_repo_root, "candy.db")
if os.path.exists(_source_db):
    shutil.copy2(_source_db, _test_db_path)

os.environ["DATABASE_URL"] = "sqlite:///" + _test_db_path

# Dummy R2 config so app.py builds a real (fake-credentialed) r2_client at
# import time instead of leaving it None -- tests that exercise the upload
# path monkeypatch app.r2_client itself, so no real network call is ever
# made against these fake credentials.
os.environ.setdefault("R2_ACCOUNT_ID", "test-account")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test-access-key")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test-secret-key")
os.environ.setdefault("R2_BUCKET_NAME", "test-bucket")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://pub-test.r2.dev")

import pytest  # noqa: E402

from app import _run_lightweight_migrations, app as flask_app, db  # noqa: E402


@pytest.fixture(autouse=True)
def db_session():
    """Reset the schema before every test so each test starts from a clean,
    isolated slate within the copied database file."""
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        _run_lightweight_migrations()
        yield
        db.session.remove()


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()
