# Migration note: report_count and is_hidden below are additive columns on
# the existing "stop" table. db.create_all() (used at startup) only creates
# tables that don't exist yet -- it will NOT alter a table that's already
# present, so existing local SQLite and production Postgres databases won't
# pick these columns up automatically. _run_lightweight_migrations(), called
# alongside db.create_all() at startup, ALTER TABLEs them in for any stop
# table that predates this change; existing rows get the column defaults
# (report_count=0, is_hidden=false). No data migration/backfill is needed.

import math
import os

from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

app = Flask(__name__)

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


class Stop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    type = db.Column(db.String, nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    candy_available = db.Column(db.Boolean, nullable=False, default=True)
    report_count = db.Column(db.Integer, nullable=False, default=0)
    is_hidden = db.Column(db.Boolean, nullable=False, default=False)

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


@app.route("/register-stop", methods=["POST"])
def register_stop():
    data = request.get_json(silent=True) or {}

    name = data.get("name")
    stop_type = data.get("type")
    latitude = data.get("latitude")
    longitude = data.get("longitude")

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
    )
    db.session.add(stop)
    db.session.commit()

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
