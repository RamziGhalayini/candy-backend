"""Which local night does a check-in belong to?

The client sends the UTC offset it had at the moment the check-in happened, and
the server uses it to pick check_in_date. Nothing is stored beyond
check_in_date, which already existed -- these tests exercise a change to how one
expression is computed, not a schema change.

A note on what these tests can and cannot show: everything here runs against a
clock the test controls (the batch route) or against relationships that hold at
any instant (the live route). NONE of it observes a real daylight-saving
rollover on a real device -- that is not testable here, only reasoned about and
then checked on hardware. See the DST test below for exactly what it does and
does not prove.
"""

from datetime import date, datetime, timedelta, timezone

from app import _local_date_for, _parse_utc_offset_minutes, CheckIn, Stop, db


def _make_verified_stop(client, device_id="registrant-1", lat=42.0, lon=-71.0, name="Original House"):
    """Same two-registrations-at-one-spot pattern as test_check_in_batch.py, so
    the verification-bonus side effect fires exactly like production."""
    original_resp = client.post(
        "/register-stop",
        json={"name": name, "type": "house", "latitude": lat, "longitude": lon, "device_id": device_id},
    )
    original_id = original_resp.get_json()["id"]

    client.post(
        "/register-stop",
        json={
            "name": f"Confirming {name}",
            "type": "house",
            "latitude": lat,
            "longitude": lon,
            "device_id": "registrant-2",
        },
    )

    return db.session.get(Stop, original_id)


# Minutes east of UTC. EDT is UTC-4, EST is UTC-5 -- the two sides of the
# rollover that happens at 2am on the night of Halloween 2026.
EDT = -240
EST = -300

# The extremes of the accepted range, 26 hours apart, so two devices checking in
# at the SAME INSTANT under these two offsets always land on different calendar
# dates -- true at every instant, so a test using them doesn't depend on when it
# is run.
FAR_WEST = -12 * 60
FAR_EAST = 14 * 60


# --- the offset parser ------------------------------------------------------


def test_parser_accepts_plausible_offsets_including_string_form():
    assert _parse_utc_offset_minutes(EDT) == -240
    assert _parse_utc_offset_minutes("330") == 330  # India, +5:30, and a query param arrives as a string
    assert _parse_utc_offset_minutes(FAR_WEST) == FAR_WEST
    assert _parse_utc_offset_minutes(FAR_EAST) == FAR_EAST


def test_parser_rejects_out_of_range_and_junk():
    # None means "fall back to the old behaviour", never "assume UTC".
    assert _parse_utc_offset_minutes(FAR_WEST - 1) is None
    assert _parse_utc_offset_minutes(FAR_EAST + 1) is None
    assert _parse_utc_offset_minutes(99999) is None
    assert _parse_utc_offset_minutes("not-a-number") is None
    assert _parse_utc_offset_minutes(None) is None
    assert _parse_utc_offset_minutes({"minutes": 60}) is None


def test_parser_rejects_bools_despite_int_subclassing():
    # bool is an int in Python; unguarded, True would read as a +1m offset.
    assert _parse_utc_offset_minutes(True) is None
    assert _parse_utc_offset_minutes(False) is None


# --- the boundary itself ----------------------------------------------------


def test_local_date_uses_the_sign_convention_the_client_sends():
    # 9:00pm EDT on Halloween is already Nov 1 in UTC. This is the bug: the
    # check-in belongs to Oct 31, the night the child was actually out.
    nine_pm_edt = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)

    assert nine_pm_edt.date() == date(2026, 11, 1)  # what the old code stored
    assert _local_date_for(nine_pm_edt, EDT) == date(2026, 10, 31)  # what it means


def test_boundary_is_local_midnight_with_no_rollover_hour():
    # Deliberately matching the client's own plain-local-date rule in
    # utils/checkedInToday.ts. 11:59pm local is still tonight; 12:05am local is
    # honestly the next date -- an accepted, documented consequence, not a bug.
    assert _local_date_for(datetime(2026, 11, 1, 3, 59, tzinfo=timezone.utc), EDT) == date(2026, 10, 31)
    assert _local_date_for(datetime(2026, 11, 1, 4, 5, tzinfo=timezone.utc), EDT) == date(2026, 11, 1)


def test_local_date_reads_a_naive_moment_as_utc():
    naive = datetime(2026, 11, 1, 1, 0)
    assert _local_date_for(naive, EDT) == date(2026, 10, 31)


# --- the live route ---------------------------------------------------------


def _check_in(client, stop, device_id, **extra):
    return client.post(
        "/check-in",
        json={
            "device_id": device_id,
            "stop_id": stop.id,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
            **extra,
        },
    )


def test_live_check_in_honours_the_offset_it_is_sent(client):
    """Clock-independent: the two offsets are 26 hours apart, so whenever this
    runs, the same instant must resolve to two different dates."""
    stop = _make_verified_stop(client)

    assert _check_in(client, stop, "west", utc_offset_minutes=FAR_WEST).status_code == 200
    assert _check_in(client, stop, "east", utc_offset_minutes=FAR_EAST).status_code == 200

    west = CheckIn.query.filter_by(device_id="west").one()
    east = CheckIn.query.filter_by(device_id="east").one()
    assert west.check_in_date != east.check_in_date
    assert (east.check_in_date - west.check_in_date).days == 1


def test_live_check_in_without_an_offset_is_unchanged(client):
    """The compatibility requirement: an older client sends nothing and gets
    exactly the previous date.today() behaviour."""
    stop = _make_verified_stop(client)

    assert _check_in(client, stop, "legacy-client").status_code == 200

    assert CheckIn.query.filter_by(device_id="legacy-client").one().check_in_date == date.today()


def test_live_check_in_falls_back_when_the_offset_is_junk(client):
    stop = _make_verified_stop(client)

    assert _check_in(client, stop, "junk", utc_offset_minutes="tuesday").status_code == 200
    assert _check_in(client, stop, "wild", utc_offset_minutes=99999).status_code == 200

    assert CheckIn.query.filter_by(device_id="junk").one().check_in_date == date.today()
    assert CheckIn.query.filter_by(device_id="wild").one().check_in_date == date.today()


def test_duplicate_live_check_in_still_rejected_under_an_offset(client):
    """_apply_check_in's (device_id, stop_id, check_in_date) guard is untouched
    by this change and must keep working."""
    stop = _make_verified_stop(client)

    assert _check_in(client, stop, "dupe", utc_offset_minutes=EDT).status_code == 200
    second = _check_in(client, stop, "dupe", utc_offset_minutes=EDT)

    assert second.status_code == 400
    assert second.get_json()["error"] == "already checked in at this stop today"
    assert CheckIn.query.filter_by(device_id="dupe").count() == 1


# --- the batch (replay) route -----------------------------------------------


def _batch(client, device_id, items):
    return client.post("/check-in/batch", json={"device_id": device_id, "checkins": items})


def _item(stop, captured_at, **extra):
    return {
        "stop_id": stop.id,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "checked_in_at": captured_at.isoformat(),
        **extra,
    }


def test_replay_uses_the_offset_carried_on_the_item(client):
    """The core of requirement 1. A check-in captured at 9pm EDT on Halloween
    is replayed here; its date must come from the offset that travelled WITH it,
    not from anything about now."""
    stop = _make_verified_stop(client)
    nine_pm_edt = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)

    response = _batch(client, "replayer", [_item(stop, nine_pm_edt, utc_offset_minutes=EDT)])

    assert response.status_code == 200
    assert response.get_json()["results"][0]["status"] == "accepted"
    assert CheckIn.query.filter_by(device_id="replayer").one().check_in_date == date(2026, 10, 31)


def test_replay_across_the_dst_change_keeps_each_item_on_its_own_night(client):
    """Halloween 2026 is Saturday Oct 31; US DST ends at 2am on Sunday Nov 1 --
    the same night. A queue drained the next morning can hold items captured on
    both sides of that change, so the offset is stored PER ITEM.

    What this proves: given two items carrying different offsets, each resolves
    by its own. What it does NOT prove: that a real device reports these two
    offsets across a real rollover. That needs hardware and an actual DST
    transition -- see this module's docstring.
    """
    early = _make_verified_stop(client, lat=42.0, lon=-71.0, name="Before The Change")
    late = _make_verified_stop(client, lat=43.0, lon=-72.0, name="After The Change")

    # 9:00pm EDT Oct 31 -- still Oct 31 locally, already Nov 1 in UTC.
    captured_before = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    # 1:30am EST Nov 1, after the clocks went back -- honestly Nov 1 locally.
    captured_after = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    response = _batch(
        client,
        "dst-device",
        [
            _item(early, captured_before, utc_offset_minutes=EDT),
            _item(late, captured_after, utc_offset_minutes=EST),
        ],
    )

    assert response.status_code == 200
    assert [r["status"] for r in response.get_json()["results"]] == ["accepted", "accepted"]

    by_stop = {c.stop_id: c.check_in_date for c in CheckIn.query.filter_by(device_id="dst-device").all()}
    assert by_stop[early.id] == date(2026, 10, 31)
    assert by_stop[late.id] == date(2026, 11, 1)


def test_replay_without_an_offset_keeps_its_existing_date_stability(client):
    """The pre-existing property -- date comes from the item's own captured
    time, never from sync time -- must survive this change untouched."""
    stop = _make_verified_stop(client)
    captured = datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc)

    assert _batch(client, "legacy-queue", [_item(stop, captured)]).status_code == 200

    assert CheckIn.query.filter_by(device_id="legacy-queue").one().check_in_date == date(2026, 10, 31)


def test_one_junk_offset_does_not_fail_the_whole_batch(client):
    """A malformed offset normalises to "fall back", so it can't take the other
    check-ins in the batch down with it."""
    bad = _make_verified_stop(client, lat=42.0, lon=-71.0, name="Junk Offset")
    good = _make_verified_stop(client, lat=43.0, lon=-72.0, name="Good Offset")
    captured = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)

    response = _batch(
        client,
        "mixed",
        [
            _item(bad, captured, utc_offset_minutes="not-a-number"),
            _item(good, captured + timedelta(minutes=1), utc_offset_minutes=EDT),
        ],
    )

    assert response.status_code == 200
    assert [r["status"] for r in response.get_json()["results"]] == ["accepted", "accepted"]

    by_stop = {c.stop_id: c.check_in_date for c in CheckIn.query.filter_by(device_id="mixed").all()}
    assert by_stop[bad.id] == date(2026, 11, 1)  # fell back to the UTC-derived date
    assert by_stop[good.id] == date(2026, 10, 31)  # used its offset


def test_live_then_replayed_check_in_is_not_double_credited(client):
    """The whole point of the idempotency key. The live check-in and the queued
    copy of the SAME check-in carry the same offset, so they resolve to the same
    check_in_date and the replay is a no-op."""
    stop = _make_verified_stop(client)

    live = _check_in(client, stop, "same-device", utc_offset_minutes=FAR_EAST)
    assert live.status_code == 200
    points_after_live = live.get_json()["points_total"]

    # Replayed with the offset it was captured with, and a captured time that
    # resolves to the same local date the live route just computed.
    captured = datetime.now(timezone.utc)
    replay = _batch(client, "same-device", [_item(stop, captured, utc_offset_minutes=FAR_EAST)])

    assert replay.get_json()["results"][0]["status"] == "already_recorded"
    assert CheckIn.query.filter_by(device_id="same-device").count() == 1

    after = _check_in(client, stop, "same-device", utc_offset_minutes=FAR_EAST)
    assert after.status_code == 400  # still one check-in, no extra points awarded
    assert points_after_live == 5


# --- the read path ----------------------------------------------------------


def test_night_ledger_resolves_tonight_with_the_offset_it_is_given(client):
    """If only the write path learned about the offset, the ledger would still
    split at UTC midnight and the user-visible half of the bug would survive."""
    stop = _make_verified_stop(client)
    assert _check_in(client, stop, "ledger-device", utc_offset_minutes=FAR_EAST).status_code == 200

    matching = client.get(f"/night-ledger/ledger-device?utc_offset_minutes={FAR_EAST}")
    assert matching.status_code == 200
    assert matching.get_json()["total_checkins"] == 1

    # 26 hours away, so guaranteed to be a different local date whenever this
    # runs -- that night has no check-ins.
    elsewhere = client.get(f"/night-ledger/ledger-device?utc_offset_minutes={FAR_WEST}")
    assert elsewhere.get_json()["total_checkins"] == 0


def test_night_ledger_without_an_offset_is_unchanged(client):
    stop = _make_verified_stop(client)
    assert _check_in(client, stop, "legacy-ledger").status_code == 200

    response = client.get("/night-ledger/legacy-ledger")

    assert response.status_code == 200
    assert response.get_json()["date"] == date.today().isoformat()
    assert response.get_json()["total_checkins"] == 1


def test_night_ledger_falls_back_when_the_offset_is_junk(client):
    stop = _make_verified_stop(client)
    assert _check_in(client, stop, "junk-ledger").status_code == 200

    response = client.get("/night-ledger/junk-ledger?utc_offset_minutes=banana")

    assert response.get_json()["date"] == date.today().isoformat()
    assert response.get_json()["total_checkins"] == 1
