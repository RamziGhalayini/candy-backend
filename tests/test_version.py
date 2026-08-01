import os


def test_version_reports_the_render_commit_sha(client, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "69fb640b2d1f209c574c9118bd2f792c5e8537d0")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.get_json() == {"commit": "69fb640b2d1f209c574c9118bd2f792c5e8537d0"}


def test_version_degrades_to_null_when_the_variable_is_absent(client, monkeypatch):
    """Local runs have no RENDER_GIT_COMMIT. That is not an error -- the route
    must answer, not 500, so the same request works against a dev machine and
    against production."""
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)

    response = client.get("/version")

    assert response.status_code == 200
    assert response.get_json() == {"commit": None}


def test_version_treats_an_empty_variable_as_absent(client, monkeypatch):
    """An env var set to "" is Render telling us nothing, not a commit named
    empty string. Reporting "" would look like a real answer to a caller
    comparing SHAs."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.get_json() == {"commit": None}


def test_version_leaks_nothing_else_from_the_render_environment(client, monkeypatch):
    """The whole basis for making this endpoint public and unauthenticated is
    that a commit SHA on a public repo reveals nothing the repo does not. That
    holds ONLY while the response stays SHA-only. These other RENDER_* values
    live in the same environment and must never appear here."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "deadbeefcafe")
    monkeypatch.setenv("RENDER_INSTANCE_ID", "srv-instance-should-not-leak")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-service-should-not-leak")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "internal-host-should-not-leak")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "name-should-not-leak")
    monkeypatch.setenv("RENDER_GIT_BRANCH", "branch-should-not-leak")

    response = client.get("/version")
    body = response.get_json()

    assert list(body.keys()) == ["commit"]

    raw = response.get_data(as_text=True)
    for secretish in (
        "srv-instance-should-not-leak",
        "srv-service-should-not-leak",
        "internal-host-should-not-leak",
        "name-should-not-leak",
        "branch-should-not-leak",
    ):
        assert secretish not in raw


def test_version_does_not_expose_the_database_url(client, monkeypatch):
    """DATABASE_URL is in the same process environment and has already leaked
    once in this project's history. Pin it explicitly."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "deadbeefcafe")

    raw = client.get("/version").get_data(as_text=True)

    assert os.environ.get("DATABASE_URL", "sentinel-never-matches") not in raw


def test_version_is_read_only_and_needs_no_body(client, monkeypatch):
    """A confirmation probe must be safe to call against production repeatedly.
    GET only -- POST is not accepted."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "deadbeefcafe")

    assert client.get("/version").status_code == 200
    assert client.post("/version").status_code == 405
