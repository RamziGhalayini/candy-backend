# Migration note: report_count and is_hidden below are additive columns on
# the existing "stop" table. db.create_all() (used at startup) only creates
# tables that don't exist yet -- it will NOT alter a table that's already
# present, so existing local SQLite and production Postgres databases won't
# pick these columns up automatically. _run_lightweight_migrations(), called
# alongside db.create_all() at startup, ALTER TABLEs them in for any stop
# table that predates this change; existing rows get the column defaults
# (report_count=0, is_hidden=false). No data migration/backfill is needed.

import json
import math
import os
import secrets
from datetime import date, datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

app = Flask(__name__)
CORS(app)

database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Render (and some other providers) hand out "postgres://", but
    # SQLAlchemy's psycopg2 dialect requires "postgresql://".
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    basedir = os.path.abspath(os.path.dirname(__file__))
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "candy.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# How close two stops' lat/lon must be (in degrees) to be considered the
# same physical address, since we don't have real addresses yet.
VERIFICATION_TOLERANCE_DEGREES = 0.0005
REPORT_HIDE_THRESHOLD = 3

# Points economy. Households are anonymous, keyed only by an app-generated
# device_id -- no accounts, no personal info.
CHECKIN_POINTS = 5
VERIFICATION_BONUS_POINTS = 10
TRIVIA_POINTS = 10
CHECKIN_RADIUS_METERS = 75

basedir = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(basedir, "trivia_questions.json")) as f:
    TRIVIA_QUESTIONS = json.load(f)
with open(os.path.join(basedir, "rewards_catalog.json")) as f:
    REWARDS_CATALOG = json.load(f)


class Stop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    type = db.Column(db.String, nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    candy_available = db.Column(db.Boolean, nullable=False, default=True)
    report_count = db.Column(db.Integer, nullable=False, default=0)
    is_hidden = db.Column(db.Boolean, nullable=False, default=False)
    registrant_device_id = db.Column(db.String, nullable=True)
    verification_bonus_awarded = db.Column(db.Boolean, nullable=False, default=False)

    def is_verified(self):
        """True once at least 2 registrations share (roughly) this address."""
        matches = Stop.query.filter(
            Stop.latitude.between(
                self.latitude - VERIFICATION_TOLERANCE_DEGREES,
                self.latitude + VERIFICATION_TOLERANCE_DEGREES,
            ),
            Stop.longitude.between(
                self.longitude - VERIFICATION_TOLERANCE_DEGREES,
                self.longitude + VERIFICATION_TOLERANCE_DEGREES,
            ),
        ).count()
        return matches >= 2

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "candy_available": self.candy_available,
            "report_count": self.report_count,
            "is_hidden": self.is_hidden,
            "verified": self.is_verified(),
        }


class Household(db.Model):
    """An anonymous household, keyed by an app-generated device_id. No accounts,
    no personal info -- just a points balance."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, unique=True, nullable=False, index=True)
    points = db.Column(db.Integer, nullable=False, default=0)


class CheckIn(db.Model):
    """Records that a device checked in at a stop on a given day. The unique
    constraint enforces "max one check-in per stop per device per day"."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, nullable=False, index=True)
    stop_id = db.Column(db.Integer, db.ForeignKey("stop.id"), nullable=False, index=True)
    check_in_date = db.Column(db.Date, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("device_id", "stop_id", "check_in_date", name="uq_checkin_device_stop_date"),
    )


class TriviaAnswer(db.Model):
    """Records a device's one scored trivia attempt for a given day."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, nullable=False, index=True)
    answer_date = db.Column(db.Date, nullable=False)
    question_id = db.Column(db.String, nullable=False)
    was_correct = db.Column(db.Boolean, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("device_id", "answer_date", name="uq_trivia_device_date"),
    )


class Redemption(db.Model):
    """Audit trail of reward redemptions and the codes handed out for them."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, nullable=False, index=True)
    reward_id = db.Column(db.String, nullable=False)
    points_spent = db.Column(db.Integer, nullable=False)
    code = db.Column(db.String, nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


def _run_lightweight_migrations():
    """ALTER TABLE in any additive columns missing from a pre-existing stop table."""
    inspector = inspect(db.engine)
    if "stop" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("stop")}
    with db.engine.begin() as connection:
        if "report_count" not in existing_columns:
            connection.execute(
                text("ALTER TABLE stop ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0")
            )
        if "is_hidden" not in existing_columns:
            connection.execute(
                text("ALTER TABLE stop ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT FALSE")
            )
        if "registrant_device_id" not in existing_columns:
            connection.execute(
                text("ALTER TABLE stop ADD COLUMN registrant_device_id VARCHAR")
            )
        if "verification_bonus_awarded" not in existing_columns:
            connection.execute(
                text(
                    "ALTER TABLE stop ADD COLUMN verification_bonus_awarded "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )


with app.app_context():
    db.create_all()
    _run_lightweight_migrations()


def haversine_distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in kilometers."""
    earth_radius_km = 6371

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return earth_radius_km * c


def _get_or_create_household(device_id):
    household = Household.query.filter_by(device_id=device_id).first()
    if household is None:
        household = Household(device_id=device_id, points=0)
        db.session.add(household)
    return household


def _award_points(device_id, amount):
    household = _get_or_create_household(device_id)
    household.points += amount
    return household


def _maybe_award_verification_bonus(stop):
    """If this stop's registration just pushed its address group to >= 2
    matches for the first time, pay the one-time bonus to whichever device
    registered the earliest (original) stop in that group."""
    matches = (
        Stop.query.filter(
            Stop.latitude.between(
                stop.latitude - VERIFICATION_TOLERANCE_DEGREES,
                stop.latitude + VERIFICATION_TOLERANCE_DEGREES,
            ),
            Stop.longitude.between(
                stop.longitude - VERIFICATION_TOLERANCE_DEGREES,
                stop.longitude + VERIFICATION_TOLERANCE_DEGREES,
            ),
        )
        .order_by(Stop.id.asc())
        .all()
    )
    if len(matches) < 2:
        return

    original = matches[0]
    if original.verification_bonus_awarded:
        return

    original.verification_bonus_awarded = True
    if original.registrant_device_id:
        _award_points(original.registrant_device_id, VERIFICATION_BONUS_POINTS)
    db.session.commit()


@app.route("/register-stop", methods=["POST"])
def register_stop():
    data = request.get_json(silent=True) or {}

    name = data.get("name")
    stop_type = data.get("type")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    device_id = data.get("device_id")

    if name is None or stop_type is None or latitude is None or longitude is None:
        return jsonify({"error": "name, type, latitude, and longitude are required"}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    stop = Stop(
        name=name,
        type=stop_type,
        latitude=latitude,
        longitude=longitude,
        candy_available=True,
        registrant_device_id=device_id,
    )
    db.session.add(stop)
    db.session.commit()

    _maybe_award_verification_bonus(stop)

    return jsonify(stop.to_dict()), 201


@app.route("/nearby-stops", methods=["GET"])
def nearby_stops():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    radius = request.args.get("radius", default=1.0, type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon query params are required"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except ValueError:
        return jsonify({"error": "lat and lon must be numbers"}), 400

    nearby = []
    for stop in Stop.query.filter_by(is_hidden=False).all():
        distance = haversine_distance_km(lat, lon, stop.latitude, stop.longitude)
        if distance <= radius:
            nearby.append({**stop.to_dict(), "distance_km": round(distance, 3)})

    nearby.sort(key=lambda s: s["distance_km"])

    return jsonify(nearby)


@app.route("/report-stop/<int:stop_id>", methods=["POST"])
def report_stop(stop_id):
    data = request.get_json(silent=True) or {}

    reason = data.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        return jsonify({"error": "reason must be a string"}), 400

    stop = db.session.get(Stop, stop_id)
    if stop is None:
        return jsonify({"error": "stop not found"}), 404

    stop.report_count += 1
    if stop.report_count >= REPORT_HIDE_THRESHOLD:
        stop.is_hidden = True
    db.session.commit()

    return jsonify(stop.to_dict())


@app.route("/update-candy-status/<int:stop_id>", methods=["PUT"])
def update_candy_status(stop_id):
    data = request.get_json(silent=True) or {}

    available = data.get("available")
    if available is None or not isinstance(available, bool):
        return jsonify({"error": "available (boolean) is required"}), 400

    stop = db.session.get(Stop, stop_id)
    if stop is None:
        return jsonify({"error": "stop not found"}), 404

    stop.candy_available = available
    db.session.commit()

    return jsonify(stop.to_dict())


@app.route("/check-in", methods=["POST"])
def check_in():
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    stop_id = data.get("stop_id")
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not device_id or stop_id is None or latitude is None or longitude is None:
        return jsonify({"error": "device_id, stop_id, latitude, and longitude are required"}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    stop = db.session.get(Stop, stop_id)
    if stop is None or stop.is_hidden:
        return jsonify({"error": "stop not found"}), 404

    if not stop.is_verified():
        return jsonify({"error": "this stop isn't verified yet"}), 400

    distance_meters = haversine_distance_km(latitude, longitude, stop.latitude, stop.longitude) * 1000
    if distance_meters > CHECKIN_RADIUS_METERS:
        return jsonify({"error": "you're too far from this stop to check in"}), 400

    today = date.today()
    existing = CheckIn.query.filter_by(device_id=device_id, stop_id=stop_id, check_in_date=today).first()
    if existing is not None:
        return jsonify({"error": "already checked in at this stop today"}), 400

    db.session.add(CheckIn(device_id=device_id, stop_id=stop_id, check_in_date=today))
    household = _award_points(device_id, CHECKIN_POINTS)
    db.session.commit()

    return jsonify({"points_awarded": CHECKIN_POINTS, "points_total": household.points})


@app.route("/trivia/today", methods=["GET"])
def trivia_today():
    today = date.today()
    question = TRIVIA_QUESTIONS[today.toordinal() % len(TRIVIA_QUESTIONS)]

    result = {
        "id": question["id"],
        "question": question["question"],
        "choices": question["choices"],
    }

    device_id = request.args.get("device_id")
    if device_id:
        answered = TriviaAnswer.query.filter_by(device_id=device_id, answer_date=today).first()
        result["already_answered"] = answered is not None
        result["was_correct"] = answered.was_correct if answered is not None else None

    return jsonify(result)


@app.route("/trivia/answer", methods=["POST"])
def trivia_answer():
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    answer = data.get("answer")

    if not device_id or answer is None:
        return jsonify({"error": "device_id and answer are required"}), 400

    today = date.today()
    if TriviaAnswer.query.filter_by(device_id=device_id, answer_date=today).first() is not None:
        return jsonify({"error": "already answered today's trivia question"}), 400

    question = TRIVIA_QUESTIONS[today.toordinal() % len(TRIVIA_QUESTIONS)]
    correct = answer == question["correct_index"]

    db.session.add(
        TriviaAnswer(
            device_id=device_id,
            answer_date=today,
            question_id=question["id"],
            was_correct=correct,
        )
    )

    points_awarded = 0
    if correct:
        household = _award_points(device_id, TRIVIA_POINTS)
        points_awarded = TRIVIA_POINTS
    else:
        household = _get_or_create_household(device_id)

    db.session.commit()

    return jsonify({"correct": correct, "points_awarded": points_awarded, "points_total": household.points})


@app.route("/rewards-catalog", methods=["GET"])
def rewards_catalog():
    return jsonify(REWARDS_CATALOG)


@app.route("/redeem-reward/<reward_id>", methods=["POST"])
def redeem_reward(reward_id):
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    reward = next((r for r in REWARDS_CATALOG if r["id"] == reward_id), None)
    if reward is None:
        return jsonify({"error": "reward not found"}), 404

    household = Household.query.filter_by(device_id=device_id).first()
    if household is None or household.points < reward["points_cost"]:
        return jsonify({"error": "not enough points for this reward"}), 400

    household.points -= reward["points_cost"]
    code = "TM-" + secrets.token_hex(4).upper()

    db.session.add(
        Redemption(
            device_id=device_id,
            reward_id=reward_id,
            points_spent=reward["points_cost"],
            code=code,
        )
    )
    db.session.commit()

    return jsonify({"code": code, "points_remaining": household.points})


@app.route("/points/<device_id>", methods=["GET"])
def get_points(device_id):
    household = Household.query.filter_by(device_id=device_id).first()
    points = household.points if household is not None else 0
    return jsonify({"device_id": device_id, "points": points})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
