# Migration note: report_count and is_hidden below are additive columns on
# the existing "stop" table. db.create_all() (used at startup) only creates
# tables that don't exist yet -- it will NOT alter a table that's already
# present, so existing local SQLite and production Postgres databases won't
# pick these columns up automatically. _run_lightweight_migrations(), called
# alongside db.create_all() at startup, ALTER TABLEs them in for any stop
# table that predates this change; existing rows get the column defaults
# (report_count=0, is_hidden=false). No data migration/backfill is needed.
#
# Wholly new tables (e.g. "business") need no entry here at all -- create_all()
# already creates any table that doesn't exist yet, on both fresh and
# pre-existing databases. _run_lightweight_migrations() only earns its keep
# for columns bolted onto a table create_all() has already made.

import hashlib
import hmac
import json
import math
import os
import secrets
import uuid
from datetime import date, datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

import legend_token
from apple_verification import AppleVerificationConfigError, AppleVerificationError, verify_transaction

# Must match the client's LEGEND_MODE_SKU (constants/legendMode.ts) exactly --
# the two live in separate repos with no shared source of truth, so a drift
# here would silently make every mint call fail its product-id check.
LEGEND_MODE_PRODUCT_ID = "com.treatmapapp.candystops.legendmode"

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

# House Greetings audio storage (Cloudflare R2, S3-compatible). Render's free
# tier has no persistent disk -- files written locally get wiped on every
# redeploy -- so greeting clips live in R2 and Stop.greeting_audio_url just
# stores the public URL.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    r2_client = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
else:
    r2_client = None

MAX_GREETING_BYTES = 2 * 1024 * 1024
ALLOWED_GREETING_CONTENT_TYPES = {
    "audio/m4a",
    "audio/mp4",
    "audio/x-m4a",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/aac",
}
ALLOWED_GREETING_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm", ".aac", ".caf"}

# Hard backstop above MAX_GREETING_BYTES (accounts for multipart overhead) --
# rejects oversized request bodies before Flask even buffers them into memory.
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024

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
CHECKIN_MILESTONE_INTERVAL = 20

# Business rewards are business-set (Business.points_cost, entered at
# registration) so an owner can price their own offer; this is the fallback
# for businesses that leave it blank, roughly matched to the existing
# static catalog's local_business entry (30 points).
BUSINESS_REWARD_DEFAULT_POINTS_COST = 25

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
    candy_count = db.Column(db.Integer, nullable=True)
    greeting_audio_url = db.Column(db.String, nullable=True)

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
            "candy_count": self.candy_count,
            "greeting_audio_url": self.greeting_audio_url,
        }


class Household(db.Model):
    """An anonymous household, keyed by an app-generated device_id. No accounts,
    no personal info -- just a points balance."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, unique=True, nullable=False, index=True)
    points = db.Column(db.Integer, nullable=False, default=0)
    greetings_unlocked = db.Column(db.Boolean, nullable=False, default=False)


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


class LegendUnlock(db.Model):
    """A restore code tied to the device whose StoreKit purchase unlocked
    Legend Mode -- lets that device (or a reinstall of it) recover
    legendModeActive locally without going through the App Store again.
    No accounts, so the code itself is the credential: whoever holds it can
    redeem it, same tradeoff as Redemption.code above. One code per
    device_id, minted once and returned again on repeat mint calls.

    HASH-BASED STORAGE (added alongside real Apple purchase verification --
    see mint_legend_code): `code` is the ORIGINAL raw-code column, kept only
    for rows minted before this change (grandfathered -- redeem still
    honors them by hash match, and their `code` value is what code_hash was
    backfilled from; see _run_lightweight_migrations). `code` remains
    NOT NULL at the database level -- SQLite cannot relax an existing
    column's NOT NULL constraint without a full table rebuild (this
    project's own _run_lightweight_migrations pattern only ever ADDS
    columns for exactly this reason), so a real schema change to make it
    nullable was out of scope for this pass. For NEW rows minted under the
    hash-only scheme, `code` is set to the SAME value as `code_hash` --
    the actual raw code is never computed into this column for new rows,
    only its hash, duplicated to satisfy the pre-existing NOT NULL+UNIQUE
    constraints without any risk to the constraint itself (a hash has
    negligible collision probability, same as any unique token already
    stored here). `transaction_id` is null for grandfathered rows and set
    for every row minted under the new scheme; enforced unique at the
    application layer (an explicit query before insert in mint_legend_code)
    rather than a DB-level constraint, since retrofitting a UNIQUE index
    via ALTER TABLE onto SQLite hits the same rebuild limitation."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, nullable=False, unique=True, index=True)
    code = db.Column(db.String, nullable=False, unique=True, index=True)
    transaction_id = db.Column(db.String, nullable=True, index=True)
    code_hash = db.Column(db.String, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class Business(db.Model):
    """A locally registered business offering a reward, anonymous like Stop
    -- keyed by registrant device_id, no accounts. Deliberately separate from
    Stop: businesses aren't candy stops, and this flow has no verification/
    hiding/candy-count machinery (yet)."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    address = db.Column(db.String, nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    category = db.Column(db.String, nullable=False)
    description = db.Column(db.String, nullable=False)
    contact_email = db.Column(db.String, nullable=False)
    reward_offer = db.Column(db.String, nullable=False)
    device_id = db.Column(db.String, nullable=True)
    points_cost = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def resolved_points_cost(self):
        return self.points_cost if self.points_cost is not None else BUSINESS_REWARD_DEFAULT_POINTS_COST

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "category": self.category,
            "description": self.description,
            "contact_email": self.contact_email,
            "reward_offer": self.reward_offer,
            "points_cost": self.resolved_points_cost(),
            "created_at": self.created_at.isoformat(),
        }

    def reward_catalog_entry(self):
        return {
            "id": f"business-{self.id}",
            "name": self.reward_offer,
            "points_cost": self.resolved_points_cost(),
            "sponsor_name": self.name,
            "sponsor_type": "local_business",
            "image_url": "",
        }


def _run_lightweight_migrations():
    """ALTER TABLE in any additive columns missing from pre-existing tables."""
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()

    if "stop" in table_names:
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
            if "candy_count" not in existing_columns:
                connection.execute(
                    text("ALTER TABLE stop ADD COLUMN candy_count INTEGER")
                )
            if "greeting_audio_url" not in existing_columns:
                connection.execute(
                    text("ALTER TABLE stop ADD COLUMN greeting_audio_url VARCHAR")
                )

    if "household" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("household")}
        with db.engine.begin() as connection:
            if "greetings_unlocked" not in existing_columns:
                connection.execute(
                    text(
                        "ALTER TABLE household ADD COLUMN greetings_unlocked "
                        "BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                )

    if "business" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("business")}
        with db.engine.begin() as connection:
            if "points_cost" not in existing_columns:
                connection.execute(
                    text("ALTER TABLE business ADD COLUMN points_cost INTEGER")
                )

    if "legend_unlock" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("legend_unlock")}
        with db.engine.begin() as connection:
            if "transaction_id" not in existing_columns:
                connection.execute(
                    text("ALTER TABLE legend_unlock ADD COLUMN transaction_id VARCHAR")
                )
            if "code_hash" not in existing_columns:
                connection.execute(
                    text("ALTER TABLE legend_unlock ADD COLUMN code_hash VARCHAR")
                )
        # Backfill, not just a static default: every row minted before this
        # change has a real `code` already sitting there (nothing discarded,
        # nothing guessed) -- this derives code_hash from it directly, once,
        # for any row that doesn't have one yet. Safe to run on every
        # startup: rows that already have code_hash are excluded by the
        # filter, so repeat runs touch zero rows.
        rows_needing_backfill = LegendUnlock.query.filter(LegendUnlock.code_hash.is_(None)).all()
        for row in rows_needing_backfill:
            row.code_hash = hashlib.sha256(row.code.encode()).hexdigest()
        if rows_needing_backfill:
            db.session.commit()


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
    candy_count = data.get("candy_count")

    if name is None or stop_type is None or latitude is None or longitude is None:
        return jsonify({"error": "name, type, latitude, and longitude are required"}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    if candy_count is not None:
        if isinstance(candy_count, bool) or not isinstance(candy_count, int) or candy_count < 0:
            return jsonify({"error": "candy_count must be a non-negative integer"}), 400

    stop = Stop(
        name=name,
        type=stop_type,
        latitude=latitude,
        longitude=longitude,
        candy_available=True,
        registrant_device_id=device_id,
        candy_count=candy_count,
    )
    db.session.add(stop)
    db.session.commit()

    _maybe_award_verification_bonus(stop)

    return jsonify(stop.to_dict()), 201


class _InvalidRadius(ValueError):
    pass


def _parse_radius_km(args):
    """Flask's request.args.get(..., type=float) silently falls back to the
    default on a parse failure instead of erroring -- a malformed radius
    (empty string, a comma, etc.) would otherwise quietly search 1km instead
    of what the caller asked for. Parse it explicitly so bad input 400s."""
    raw = args.get("radius")
    if raw is None:
        return 1.0
    try:
        return float(raw)
    except ValueError:
        raise _InvalidRadius(raw)


@app.route("/nearby-stops", methods=["GET"])
def nearby_stops():
    lat = request.args.get("lat")
    lon = request.args.get("lon")

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon query params are required"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except ValueError:
        return jsonify({"error": "lat and lon must be numbers"}), 400

    try:
        radius = _parse_radius_km(request.args)
    except _InvalidRadius:
        return jsonify({"error": "radius must be a number"}), 400

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


@app.route("/upload-greeting/<int:stop_id>", methods=["POST"])
def upload_greeting(stop_id):
    stop = db.session.get(Stop, stop_id)
    if stop is None:
        return jsonify({"error": "stop not found"}), 404

    if r2_client is None:
        return jsonify({"error": "greeting storage is not configured"}), 500

    audio_file = request.files.get("audio")
    if audio_file is None or audio_file.filename == "":
        return jsonify({"error": "an audio file is required"}), 400

    extension = os.path.splitext(audio_file.filename)[1].lower()
    content_type = (audio_file.mimetype or "").lower()
    if content_type not in ALLOWED_GREETING_CONTENT_TYPES and extension not in ALLOWED_GREETING_EXTENSIONS:
        return jsonify({"error": "file must be an audio recording"}), 400

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "audio file is empty"}), 400
    if len(audio_bytes) > MAX_GREETING_BYTES:
        max_mb = MAX_GREETING_BYTES // (1024 * 1024)
        return jsonify({"error": f"audio file must be under {max_mb}MB"}), 400

    key = f"greetings/{stop_id}-{uuid.uuid4().hex}{extension or '.m4a'}"

    try:
        r2_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=audio_bytes,
            ContentType=content_type or "audio/m4a",
        )
    except (BotoCoreError, ClientError):
        return jsonify({"error": "could not upload audio, please try again"}), 502

    stop.greeting_audio_url = f"{R2_PUBLIC_BASE_URL}/{key}"
    db.session.commit()

    return jsonify(stop.to_dict())


@app.route("/my-stops/<device_id>", methods=["GET"])
def my_stops(device_id):
    stops = Stop.query.filter_by(registrant_device_id=device_id).all()
    return jsonify([stop.to_dict() for stop in stops])


@app.route("/update-stop/<int:stop_id>", methods=["PATCH"])
def update_stop(stop_id):
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    stop = db.session.get(Stop, stop_id)
    if stop is None:
        return jsonify({"error": "stop not found"}), 404

    if stop.registrant_device_id != device_id:
        return jsonify({"error": "you can only edit stops you registered"}), 403

    if "candy_count" in data:
        candy_count = data["candy_count"]
        if candy_count is not None:
            if isinstance(candy_count, bool) or not isinstance(candy_count, int) or candy_count < 0:
                return jsonify({"error": "candy_count must be a non-negative integer"}), 400
        stop.candy_count = candy_count

    if "candy_available" in data:
        candy_available = data["candy_available"]
        if not isinstance(candy_available, bool):
            return jsonify({"error": "candy_available must be a boolean"}), 400
        stop.candy_available = candy_available

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

    if stop.candy_count is not None:
        stop.candy_count = max(stop.candy_count - 1, 0)
        if stop.candy_count == 0:
            stop.is_hidden = True

    db.session.commit()

    total_checkins = CheckIn.query.filter_by(device_id=device_id).count()

    result = {"points_awarded": CHECKIN_POINTS, "points_total": household.points}
    if total_checkins % CHECKIN_MILESTONE_INTERVAL == 0:
        result["milestone"] = True
        result["message"] = (
            f"🎉 Milestone! You've checked in at {total_checkins} stops — "
            "ask for an extra piece, you've earned it!"
        )

    return jsonify(result)


@app.route("/night-ledger/<device_id>", methods=["GET"])
def night_ledger(device_id):
    """A simple end-of-night recap: what this household checked into today,
    and how many of the greetings they encountered they actually got to
    hear. Deliberately scoped to today's CheckIn rows rather than tracking
    anything new -- no separate "nearby but not visited" data model yet."""
    today = date.today()
    checkins_today = CheckIn.query.filter_by(device_id=device_id, check_in_date=today).all()
    stop_ids = [checkin.stop_id for checkin in checkins_today]
    stops = Stop.query.filter(Stop.id.in_(stop_ids)).all() if stop_ids else []

    household = Household.query.filter_by(device_id=device_id).first()
    greetings_unlocked = household.greetings_unlocked if household is not None else False

    greetings_encountered = sum(1 for stop in stops if stop.greeting_audio_url)
    # Check-in auto-plays every encountered greeting when unlocked, and plays
    # none when locked -- so "heard" is just "encountered" gated on the flag,
    # not tracked per-stop.
    greetings_heard = greetings_encountered if greetings_unlocked else 0

    return jsonify(
        {
            "device_id": device_id,
            "date": today.isoformat(),
            "total_checkins": len(checkins_today),
            "verified_stops_checked_in": sum(1 for stop in stops if stop.is_verified()),
            "candy_available_count": sum(1 for stop in stops if stop.candy_available),
            "greetings_encountered": greetings_encountered,
            "greetings_heard": greetings_heard,
            "greetings_unlocked": greetings_unlocked,
        }
    )


@app.route("/register-business", methods=["POST"])
def register_business():
    data = request.get_json(silent=True) or {}

    name = data.get("name")
    address = data.get("address")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    category = data.get("category")
    description = data.get("description")
    contact_email = data.get("contact_email")
    reward_offer = data.get("reward_offer")
    device_id = data.get("device_id")
    points_cost = data.get("points_cost")

    required = {
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "category": category,
        "description": description,
        "contact_email": contact_email,
        "reward_offer": reward_offer,
        "device_id": device_id,
    }
    missing = [field for field, value in required.items() if value is None]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    if points_cost is not None:
        if isinstance(points_cost, bool) or not isinstance(points_cost, int) or points_cost < 0:
            return jsonify({"error": "points_cost must be a non-negative integer"}), 400

    business = Business(
        name=name,
        address=address,
        latitude=latitude,
        longitude=longitude,
        category=category,
        description=description,
        contact_email=contact_email,
        reward_offer=reward_offer,
        device_id=device_id,
        points_cost=points_cost,
    )
    db.session.add(business)
    db.session.commit()

    return jsonify(business.to_dict()), 201


@app.route("/nearby-businesses", methods=["GET"])
def nearby_businesses():
    lat = request.args.get("lat")
    lon = request.args.get("lon")

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon query params are required"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except ValueError:
        return jsonify({"error": "lat and lon must be numbers"}), 400

    try:
        radius = _parse_radius_km(request.args)
    except _InvalidRadius:
        return jsonify({"error": "radius must be a number"}), 400

    nearby = []
    for business in Business.query.all():
        distance = haversine_distance_km(lat, lon, business.latitude, business.longitude)
        if distance <= radius:
            nearby.append({**business.to_dict(), "distance_km": round(distance, 3)})

    nearby.sort(key=lambda b: b["distance_km"])

    return jsonify(nearby)


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
    business_entries = [business.reward_catalog_entry() for business in Business.query.all()]
    return jsonify(REWARDS_CATALOG + business_entries)


def _find_reward(reward_id):
    """Look up a reward by id from either the static catalog or a
    registered business's reward_offer (id shaped "business-<Business.id>")."""
    reward = next((r for r in REWARDS_CATALOG if r["id"] == reward_id), None)
    if reward is not None:
        return reward

    if reward_id.startswith("business-"):
        try:
            business_id = int(reward_id[len("business-") :])
        except ValueError:
            return None
        business = db.session.get(Business, business_id)
        if business is not None:
            return business.reward_catalog_entry()

    return None


@app.route("/redeem-reward/<reward_id>", methods=["POST"])
def redeem_reward(reward_id):
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    reward = _find_reward(reward_id)
    if reward is None:
        return jsonify({"error": "reward not found"}), 404

    already_redeemed = Redemption.query.filter_by(device_id=device_id, reward_id=reward_id).first()
    if already_redeemed is not None:
        return jsonify({"error": "you've already redeemed this reward"}), 400

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


@app.route("/redemptions/<device_id>", methods=["GET"])
def get_redemptions(device_id):
    redemptions = (
        Redemption.query.filter_by(device_id=device_id).order_by(Redemption.created_at.desc()).all()
    )

    result = []
    for redemption in redemptions:
        reward = _find_reward(redemption.reward_id)
        result.append(
            {
                "id": redemption.id,
                "reward_id": redemption.reward_id,
                "reward_name": reward["name"] if reward else redemption.reward_id,
                "sponsor_name": reward["sponsor_name"] if reward else None,
                "points_spent": redemption.points_spent,
                "code": redemption.code,
                "created_at": redemption.created_at.isoformat(),
            }
        )

    return jsonify(result)


@app.route("/points/<device_id>", methods=["GET"])
def get_points(device_id):
    household = Household.query.filter_by(device_id=device_id).first()
    points = household.points if household is not None else 0
    greetings_unlocked = household.greetings_unlocked if household is not None else False
    return jsonify({"device_id": device_id, "points": points, "greetings_unlocked": greetings_unlocked})


@app.route("/legend-mode/mint", methods=["POST"])
def mint_legend_code():
    """Called by the client after a StoreKit purchase completes. Requires
    real proof of that purchase -- transaction_id, verified against
    Apple's App Store Server API via apple_verification.verify_transaction
    -- rather than trusting the client's bare say-so (the previous,
    deliberately-low-security version of this endpoint took only
    device_id). Idempotent per device_id: a device that already has a code
    gets the SAME code back. For a row minted under this scheme, that code
    is RECOMPUTED from its stored transaction_id (see legend_token --
    deterministic HMAC, not a stored raw value); for a grandfathered row
    from before this change, its original `code` is returned as before."""
    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    transaction_id = data.get("transaction_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    if not transaction_id:
        return jsonify({"error": "transaction_id is required"}), 400

    existing = LegendUnlock.query.filter_by(device_id=device_id).first()
    if existing is not None:
        if existing.transaction_id:
            return jsonify(
                {"code": legend_token.generate_legend_code(existing.device_id, existing.transaction_id)}
            )
        return jsonify({"code": existing.code})

    # One transaction can only ever mint one device's code -- otherwise a
    # single real purchase's transaction_id could be replayed to mint
    # codes for arbitrary other device_ids. Application-layer check (see
    # LegendUnlock's own doc comment for why this isn't a DB constraint).
    if LegendUnlock.query.filter_by(transaction_id=transaction_id).first() is not None:
        return jsonify({"error": "that transaction has already been used to unlock Legend Mode"}), 409

    try:
        verify_transaction(transaction_id, LEGEND_MODE_PRODUCT_ID)
    except AppleVerificationConfigError as error:
        # Server misconfiguration (missing credentials), not the client's
        # fault -- the real reason goes to the log, not the response body.
        app.logger.error("Apple verification not configured: %s", error)
        return jsonify({"error": "purchase verification is temporarily unavailable"}), 500
    except AppleVerificationError as error:
        # Generic to the client, detailed in the log -- same
        # no-system-state-leakage principle as redeem's error responses.
        app.logger.warning("Purchase verification failed for transaction %s: %s", transaction_id, error)
        return jsonify({"error": "could not verify that purchase"}), 403

    code = legend_token.generate_legend_code(device_id, transaction_id)
    code_hash = legend_token.hash_code(code)
    db.session.add(
        LegendUnlock(
            device_id=device_id,
            # NOT the raw code -- see LegendUnlock's own doc comment for
            # why this column still exists and still can't be null.
            code=code_hash,
            transaction_id=transaction_id,
            code_hash=code_hash,
        )
    )
    db.session.commit()

    return jsonify({"code": code})


@app.route("/legend-mode/redeem", methods=["POST"])
def redeem_legend_code():
    """Validates a restore code and reports back whether it's real. The
    client is the one that actually sets legendModeActive locally -- this
    endpoint has no per-device state to update, since redemption doesn't
    consume or transfer anything server-side.

    External behavior (request/response shape, status codes) is unchanged
    from before this pass. Internally, this now looks up by SHA-256 hash
    rather than the raw code -- hashing the submitted guess FIRST already
    defeats the character-by-character partial-match timing signal
    hmac.compare_digest exists to prevent (a near-miss guess produces a
    completely different hash, not a partial match), so that lookup is
    doing the real work; the explicit compare_digest call below is real
    defense-in-depth on the one row actually returned, not the sole
    mechanism."""
    data = request.get_json(silent=True) or {}

    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "code is required"}), 400

    submitted_hash = legend_token.hash_code(code)
    unlock = LegendUnlock.query.filter_by(code_hash=submitted_hash).first()
    if unlock is None or not hmac.compare_digest(unlock.code_hash, submitted_hash):
        return jsonify({"error": "that code is not valid"}), 404

    return jsonify({"legend_mode_active": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
