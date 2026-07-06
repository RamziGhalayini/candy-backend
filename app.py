import math
import os

from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy

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


class Stop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    type = db.Column(db.String, nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    candy_available = db.Column(db.Boolean, nullable=False, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "candy_available": self.candy_available,
        }


with app.app_context():
    db.create_all()


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
    for stop in Stop.query.all():
        distance = haversine_distance_km(lat, lon, stop.latitude, stop.longitude)
        if distance <= radius:
            nearby.append({**stop.to_dict(), "distance_km": round(distance, 3)})

    nearby.sort(key=lambda s: s["distance_km"])

    return jsonify(nearby)


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
