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

# Same reasoning as the R2 dummies above: legend_token.generate_legend_code
# needs SOME pepper to run at all (it's real code, not mocked -- only the
# Apple network call is mocked in tests/test_legend_mode.py), and a fixed
# test value keeps mint's output reproducible across test runs.
os.environ.setdefault("LEGEND_CODE_PEPPER", "test-pepper-do-not-use-in-prod")

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from app import _run_lightweight_migrations, app as flask_app, db  # noqa: E402


# ---------------------------------------------------------------------------
# Frozen clock
#
# app.py reads the calendar through the module global `date` (date.today() in
# /check-in, /night-ledger, /trivia/*, and the retention purge) and the batch
# route takes each item's date from its own checked_in_at timestamp. Two tests
# used to mix a UTC timestamp from the test with date.today() from the server:
# after 20:00 US-Eastern the UTC calendar day is already tomorrow, so the two
# dates disagreed and the suite went red for four hours every evening.
#
# The fixtures below pin BOTH clocks to one chosen instant so the result no
# longer depends on when the suite runs. The server's "today" is modelled as
# the UTC calendar day of that instant, which is what date.today() returns on
# a server whose process timezone is UTC. Both instants fall on Halloween
# night 2026, one on each side of the 20:00 Eastern rollover, so the case that
# used to fail by luck is now exercised on purpose every run.
#
# Note on mechanism: datetime.date is a C type and its `today` cannot be
# patched in place, so the fixture swaps app.py's module-level name `date` for
# a subclass whose today() answers with the frozen day. Nothing in app.py
# calls date(...) directly, and today() hands back a plain datetime.date, so
# everything downstream (SQLAlchemy Date columns, isoformat, toordinal) sees
# the same type it always did.
# ---------------------------------------------------------------------------

FROZEN_INSTANTS = {
    # 18:30 EDT on 2026-10-31 -- the UTC day and the Eastern day agree.
    "before-8pm-eastern": datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc),
    # 21:30 EDT on 2026-10-31 -- the UTC day has already rolled to 11-01.
    "after-8pm-eastern": datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc),
}


class FrozenClock:
    """What a test may know about time: the instant, and the server's day."""

    def __init__(self, now_utc):
        self.now_utc = now_utc
        self.today = now_utc.date()

    def utc(self, **delta):
        """The frozen instant shifted by a timedelta, still timezone-aware."""
        return self.now_utc + timedelta(**delta)

    def iso(self, **delta):
        """The shifted instant as the ISO-8601 string a client would send."""
        return self.utc(**delta).isoformat()


def _freeze_clock(monkeypatch, now_utc):
    frozen_today = now_utc.date()

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return frozen_today

    monkeypatch.setattr(app_module, "date", _FrozenDate)
    return FrozenClock(now_utc)


@pytest.fixture(params=list(FROZEN_INSTANTS.values()), ids=list(FROZEN_INSTANTS))
def frozen_clock(request, monkeypatch):
    """Runs the test twice, once per instant in FROZEN_INSTANTS. Use it in
    any test that sends a timestamp to the server AND cares what calendar
    day the server files it under."""
    return _freeze_clock(monkeypatch, request.param)


@pytest.fixture
def frozen_today(monkeypatch):
    """A single frozen server day (the after-8pm instant, the one that used
    to bite), for tests that only read date.today() through the app and
    would otherwise straddle midnight by accident."""
    return _freeze_clock(monkeypatch, FROZEN_INSTANTS["after-8pm-eastern"]).today


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The per-IP limiter's counters are process-global, so without this one
    test's requests spend the next test's budget and the failure shows up as
    an unrelated KeyError several files later. Reset between tests."""
    from app import _RATE_BUCKETS

    _RATE_BUCKETS.clear()
    yield
    _RATE_BUCKETS.clear()


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
