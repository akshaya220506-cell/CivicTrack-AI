from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename


# Keep application data inside this project folder.
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("CIVICTRACK_DATABASE_PATH", str(BASE_DIR / "civictrack.db")))
UPLOAD_FOLDER = Path(os.getenv("CIVICTRACK_UPLOAD_FOLDER", str(BASE_DIR / "uploads")))
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
COMPLAINT_CATEGORIES = ("Damaged Roads", "Garbage", "Water Leakage", "Drainage", "Streetlights", "Other")
DASHBOARD_SEVERITIES = ("High", "Medium", "Low")
DASHBOARD_STATUSES = ("Submitted", "Pending", "Under Review", "In Progress", "Resolved", "Rejected")
MANAGEABLE_STATUSES = ("Submitted", "Under Review", "In Progress", "Resolved", "Rejected")
LOCATION_METHODS = ("GPS", "Map", "Manual")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEPARTMENT_BY_CATEGORY = {
    "Damaged Roads": "GHMC Roads and Maintenance",
    "Garbage": "GHMC Sanitation",
    "Water Leakage": "Hyderabad Metropolitan Water Supply and Sewerage Board",
    "Drainage": "GHMC Storm Water Drainage",
    "Streetlights": "GHMC Electrical Department",
    "Other": "GHMC Civic Services",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # Includes form and multipart overhead.
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = 1800
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# Load a local .env file when python-dotenv is installed. Environment variables
# already supplied by the host remain supported if the optional loader is absent.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# Resolve these after loading .env so the configured values apply on startup.
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or app.config["SECRET_KEY"]
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"


def get_db_connection():
    """Open a connection to the project's SQLite database."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database_connection():
    """Commit successful work, roll back errors, and always close the connection."""
    connection = get_db_connection()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    """Create the table and safely add optional fields to older databases."""
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                complaint_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT NOT NULL,
                image_filename TEXT,
                severity TEXT NOT NULL,
                suggested_department TEXT NOT NULL,
                status TEXT NOT NULL,
                submission_date TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_analysis (
                complaint_id TEXT PRIMARY KEY,
                predicted_category TEXT NOT NULL,
                suggested_severity TEXT NOT NULL,
                recommended_department TEXT NOT NULL,
                short_summary TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                analysis_source TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_status_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO complaint_status_history (
                complaint_id, from_status, to_status, note, changed_at, changed_by
            )
            SELECT c.complaint_id, NULL, c.status,
                   'Status when tracking history was enabled.', c.submission_date, 'System'
            FROM complaints AS c
            WHERE NOT EXISTS (
                SELECT 1 FROM complaint_status_history AS h
                WHERE h.complaint_id = c.complaint_id
            )
            """
        )

        # Add new columns in place; this keeps old complaint rows and IDs intact.
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(complaints)")
        }
        new_columns = {
            "citizen_name": "TEXT",
            "citizen_phone": "TEXT",
            "phone_verified": "INTEGER NOT NULL DEFAULT 0",
            "address": "TEXT",
            "latitude": "REAL",
            "longitude": "REAL",
            "location_method": "TEXT",
            "landmark": "TEXT",
        }
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE complaints ADD COLUMN {column_name} {column_type}"
                )

        # Backfill the new address field from the old location string.
        connection.execute(
            "UPDATE complaints SET address = location "
            "WHERE address IS NULL AND location IS NOT NULL"
        )

        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
        if admin_username and admin_password_hash:
            connection.execute(
                "UPDATE admin_users SET is_active = 0 WHERE username != ?",
                (admin_username,),
            )
            connection.execute(
                """
                INSERT INTO admin_users (username, password_hash, is_active, created_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_active = 1
                """,
                (admin_username, admin_password_hash, datetime.now().isoformat(timespec="seconds")),
            )


def normalize_indian_mobile(phone_number):
    """Validate a 10-digit Indian mobile number and store it with +91."""
    if not re.fullmatch(r"[6-9][0-9]{9}", phone_number):
        return None
    return f"+91{phone_number}"


def classify_complaint(category, title, description):
    """Apply simple keyword rules and recommend the team for the selected category."""
    text = f"{title} {description}".lower()
    high_severity_words = (
        "dangerous", "accident", "injury", "exposed wire", "open manhole",
        "burst pipe", "flooding", "overflowing sewage", "urgent", "collapse",
    )
    medium_severity_words = (
        "blocked", "overflow", "waterlogging", "large pothole", "major leak",
        "no water", "dark street", "several days", "uncollected",
    )

    if any(word in text for word in high_severity_words):
        severity = "High"
    elif any(word in text for word in medium_severity_words):
        severity = "Medium"
    else:
        severity = "Low"

    return severity, DEPARTMENT_BY_CATEGORY[category]


def rule_based_analysis(category, title, description, reason):
    """Build an honestly labelled fallback using the existing keyword classifier."""
    severity, department = classify_complaint(category, title, description)
    return {
        "predicted_category": category,
        "suggested_severity": severity,
        "recommended_department": department,
        "short_summary": f"Civic issue report submitted under {category}.",
        "reasoning": (
            "The existing keyword rules reviewed the title and description for urgency terms "
            "and selected a department from the submitted category. " + reason
        ),
        "analysis_source": "Rule-based fallback",
    }


def validate_gemini_analysis(response_text):
    """Parse Gemini JSON and reject values outside our civic classifications."""
    result = json.loads(response_text)
    required_fields = {
        "predicted_category",
        "suggested_severity",
        "recommended_department",
        "short_summary",
        "reasoning",
    }
    if not isinstance(result, dict) or set(result) != required_fields:
        raise ValueError("Gemini returned an unexpected JSON shape")

    category = result["predicted_category"]
    severity = result["suggested_severity"]
    department = result["recommended_department"]
    if category not in COMPLAINT_CATEGORIES:
        raise ValueError("Gemini returned an unknown category")
    if severity not in DASHBOARD_SEVERITIES:
        raise ValueError("Gemini returned an unknown severity")
    if department != DEPARTMENT_BY_CATEGORY[category]:
        raise ValueError("Gemini returned a department that does not match its category")

    summary = result["short_summary"]
    reasoning = result["reasoning"]
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 240:
        raise ValueError("Gemini returned an invalid summary")
    if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning.strip()) > 600:
        raise ValueError("Gemini returned invalid reasoning")

    return {
        "predicted_category": category,
        "suggested_severity": severity,
        "recommended_department": department,
        "short_summary": summary.strip(),
        "reasoning": reasoning.strip(),
        "analysis_source": "Gemini AI",
    }


def sanitize_description_for_gemini(description, private_values=()):
    """Remove known form details and common contact/address patterns before sending text."""
    safe_description = description
    for private_value in sorted((value for value in private_values if value), key=len, reverse=True):
        safe_description = re.sub(re.escape(private_value), "[REDACTED]", safe_description, flags=re.IGNORECASE)
    safe_description = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED EMAIL]", safe_description)
    safe_description = re.sub(r"(?<!\w)(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}(?!\w)", "[REDACTED PHONE]", safe_description)
    safe_description = re.sub(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)", "[REDACTED PHONE]", safe_description)
    safe_description = re.sub(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED ID]", safe_description)
    safe_description = re.sub(
        r"\b(?:house|h\.?\s*no\.?|flat|plot|door)\s*(?:no\.?\s*)?\w[\w/-]*[^,.;\n]*",
        "[REDACTED ADDRESS]", safe_description, flags=re.IGNORECASE,
    )
    return safe_description


def description_is_safe_for_gemini(description, private_values=()):
    """Conservatively reject free text that still looks like it contains PII or a precise place."""
    if re.search(r"\d", description):
        return False
    sensitive_patterns = (
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"(?<!\w)(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}(?!\w)",
        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
        r"\b(?:my name is|i am called|contact(?:ed)? by|reporter is)\s+[A-Z][a-z]+",
        r"\b(?:mr|mrs|ms|miss|dr)\.?\s+[A-Z][a-z]+",
        r"\b(?:house|h\.?\s*no\.?|flat|plot|door|address|landmark|near|opposite|beside|next to|colony|nagar|cross|pin code)\b",
        r"\b\d{1,5}\s+[A-Za-z][\w -]{0,40}\b(?:road|street|lane|colony|nagar)\b",
        r"\b(?:with|from|by|named|called|reporter|resident|contact(?:ed)?|mr|mrs|ms|dr)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
    )
    if any(re.search(pattern, description) for pattern in sensitive_patterns):
        return False
    description_words = {word.casefold() for word in re.findall(r"[A-Za-z]+", description)}
    for private_value in private_values:
        common_location_words = {"road", "roads", "street", "lane", "area", "ward", "hyderabad"}
        private_words = {
            word.casefold() for word in re.findall(r"[A-Za-z]+", private_value)
            if len(word) >= 4 and word.casefold() not in common_location_words
        }
        if private_words.intersection(description_words):
            return False
    safe_sentence_starters = {
        "a", "an", "the", "there", "this", "it", "i", "we", "water", "garbage",
        "drain", "drainage", "streetlight", "streetlights", "road", "roads", "pothole",
        "potholes", "broken", "damaged", "blocked", "leaking", "overflowing", "sewage",
        "pipe", "signal", "light", "lights", "trash", "waste", "several", "one", "two",
    }
    for sentence in re.split(r"[.!?\n]+", description.strip()):
        words = re.findall(r"[A-Za-z]+", sentence)
        if words and words[0].casefold() not in safe_sentence_starters:
            return False
        remaining = sentence[len(words[0]):] if words else ""
        if re.search(r"\b[A-Z][a-z]{2,}\b", remaining):
            return False
    # Only send descriptions made entirely from a small civic vocabulary and
    # ordinary grammar words. Unknown words can be names or precise places.
    approved_words = set("""
        a an the this that there it i we they is are was were be been being has have had
        do does did can could may might will would should to of on in at for by with from
        and or but if after before during because when while as into onto through over under
        very more most less many some several one two three issue problem report complaint
        civic local public damaged damage damaging broken break crack cracked surface road street
        roads pothole potholes footpath sidewalk pavement crossing junction traffic vehicle
        vehicles car cars bike bikes walk walking pedestrian pedestrians swerve slow slowing
        unsafe danger dangerous safety hazard collapsed collapse deep large small repair
        repairs urgent needs needed affecting affect affected access blocked block blocking
        water leak leakage leaking pipe pipes burst supply puddle puddles flood flooding
        waterlogging tap valve drain drainage drains clogged clogging sewage sewer manhole
        overflow overflowing stagnant dirty clean smell foul garbage trash waste rubbish
        pile piles bin bins collection collected uncollected sanitation pickup streetlight
        streetlights light lights lamp lamps dark darkness electrical electricity wire wires
        exposed flicker flickering outage signal signals working stopped damaged fallen
        broken fallen tree branch debris day days night week weeks month months keeps
        continue continues continuously continuously reported creating causing causes caused
        forcing forces force cars surface has been after rain heavy monsoon near
    """.split())
    words_in_description = {word.casefold() for word in re.findall(r"[A-Za-z]+", description)}
    return words_in_description.issubset(approved_words)


def request_gemini_analysis(category, description, api_key):
    """Call the official SDK with only category and sanitized complaint description."""
    from google import genai
    from google.genai import types

    schema = {
        "type": "object",
        "properties": {
            "predicted_category": {"type": "string", "enum": list(COMPLAINT_CATEGORIES)},
            "suggested_severity": {"type": "string", "enum": list(DASHBOARD_SEVERITIES)},
            "recommended_department": {
                "type": "string",
                "enum": list(DEPARTMENT_BY_CATEGORY.values()),
            },
            "short_summary": {"type": "string"},
            "reasoning": {"type": "string"},
        },
        "required": [
            "predicted_category",
            "suggested_severity",
            "recommended_department",
            "short_summary",
            "reasoning",
        ],
    }
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        contents=(
            "Analyze this civic issue and provide an advisory classification only. "
            "Do not determine resolution, enforcement, or any final government action.\n"
            f"Selected category: {category}\n"
            f"Complaint description: {description}"
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0.2,
            max_output_tokens=400,
        ),
    )
    if not response.text:
        raise ValueError("Gemini returned no text")
    return response.text


def analyze_complaint(category, title, description, private_values=()):
    """Use Gemini when configured and valid; otherwise use existing keyword rules."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return rule_based_analysis(
            category, title, description,
            "Gemini was not used because GEMINI_API_KEY is not configured.",
        )

    try:
        safe_description = sanitize_description_for_gemini(description, private_values)
        if not description_is_safe_for_gemini(safe_description, private_values):
            return rule_based_analysis(
                category, title, description,
                "Gemini was not used because the description may contain personal or precise location information.",
            )
        response_text = request_gemini_analysis(category, safe_description, api_key)
        return validate_gemini_analysis(response_text)
    except Exception:
        # Never log request text, keys, or provider error payloads; keep complaints usable.
        return rule_based_analysis(
            category, title, description,
            "Gemini could not provide a valid analysis, so it was not used.",
        )


def allowed_image(filename):
    """Return True only for the image file extensions accepted by the form."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def valid_image_contents(image_bytes, filename):
    """Verify the bytes are a complete image whose actual format matches the extension."""
    expected_formats = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format != expected_formats.get(extension):
                return False
            if image.width * image.height > 25_000_000:
                return False
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return False


def make_complaint_id(connection):
    """Create a readable ID and check the database to avoid duplicate IDs."""
    year = datetime.now().year
    while True:
        complaint_id = f"CTA-{year}-{secrets.token_hex(3).upper()}"
        existing = connection.execute(
            "SELECT 1 FROM complaints WHERE complaint_id = ?", (complaint_id,)
        ).fetchone()
        if existing is None:
            return complaint_id


def render_report_form(errors=None, form_data=None, status_code=200):
    """Render the report page, preserving typed values when validation fails."""
    return render_template(
        "report.html",
        errors=errors or {},
        form_data=form_data or {},
        categories=("Damaged Roads", "Garbage", "Water Leakage", "Drainage", "Streetlights", "Other"),
    ), status_code


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/report", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def report():
    """Show the complaint form and save valid submissions."""
    if request.method == "GET":
        return render_report_form()

    form_data = request.form.to_dict()
    errors = {}
    title = form_data.get("title", "").strip()
    category = form_data.get("category", "").strip()
    description = form_data.get("description", "").strip()
    citizen_name = form_data.get("citizen_name", "").strip()
    phone_number = form_data.get("phone_number", "").strip()
    citizen_phone = normalize_indian_mobile(phone_number)
    address = form_data.get("address", "").strip()
    landmark = form_data.get("landmark", "").strip()
    location_method = form_data.get("location_method", "").strip()
    latitude_value = form_data.get("latitude", "").strip()
    longitude_value = form_data.get("longitude", "").strip()
    valid_categories = {"Damaged Roads", "Garbage", "Water Leakage", "Drainage", "Streetlights", "Other"}

    if not citizen_name:
        errors["citizen_name"] = "Enter your full name."
    elif len(citizen_name) > 120:
        errors["citizen_name"] = "Keep your name to 120 characters or fewer."
    if not phone_number:
        errors["phone_number"] = "Enter your 10-digit Indian mobile number."
    elif citizen_phone is None:
        errors["phone_number"] = "Enter a valid 10-digit Indian mobile number beginning with 6, 7, 8, or 9."
    if form_data.get("contact_consent") != "yes":
        errors["contact_consent"] = "Please confirm that we may use your contact details for this report."
    if not title:
        errors["title"] = "Enter a short title for the issue."
    elif len(title) > 120:
        errors["title"] = "Keep the title to 120 characters or fewer."
    if category not in valid_categories:
        errors["category"] = "Choose one of the listed issue categories."
    if not description:
        errors["description"] = "Describe the issue so the civic team can understand it."
    elif len(description) > 3000:
        errors["description"] = "Keep the description to 3,000 characters or fewer."
    if not address:
        errors["address"] = "Enter the address or a clear description of where the issue is."
    elif len(address) > 500:
        errors["address"] = "Keep the address to 500 characters or fewer."
    if len(landmark) > 500:
        errors["landmark"] = "Keep the landmark or directions to 500 characters or fewer."
    if location_method not in LOCATION_METHODS:
        errors["location_method"] = "Choose GPS, Map pin, or Manual address."

    latitude = None
    longitude = None
    if location_method in ("GPS", "Map"):
        try:
            latitude = float(latitude_value)
            longitude = float(longitude_value)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
        except (TypeError, ValueError):
            errors["coordinates"] = "Choose a valid GPS location or map pin, or switch to Manual address."

    # The gallery and camera controls share the "photo" field name. Pick the one
    # that actually contains a file, since the unused input is submitted empty.
    photo = next(
        (uploaded_file for uploaded_file in request.files.getlist("photo") if uploaded_file.filename),
        None,
    )
    image_filename = None
    image_bytes = None
    if photo and photo.filename:
        safe_original_name = secure_filename(photo.filename)
        if not safe_original_name or not allowed_image(safe_original_name):
            errors["photo"] = "Choose a JPG, JPEG, PNG, or WEBP image."
        else:
            image_bytes = photo.stream.read(MAX_IMAGE_SIZE + 1)
            if len(image_bytes) > MAX_IMAGE_SIZE:
                errors["photo"] = "The image must be 5 MB or smaller."
            elif not valid_image_contents(image_bytes, safe_original_name):
                errors["photo"] = "The file contents do not match a valid JPG, JPEG, PNG, or WEBP image."

    if errors:
        return render_report_form(errors, form_data, 400)

    location = f"{address} - {landmark}" if landmark else address
    analysis = analyze_complaint(
        category, title, description,
        private_values=(citizen_name, phone_number, address, landmark),
    )
    severity = analysis["suggested_severity"]
    department = analysis["recommended_department"]
    image_path = None

    with database_connection() as connection:
        complaint_id = make_complaint_id(connection)
        if image_bytes is not None:
            extension = secure_filename(photo.filename).rsplit(".", 1)[1].lower()
            image_filename = f"{complaint_id.lower()}_{secrets.token_hex(4)}.{extension}"
            image_path = UPLOAD_FOLDER / image_filename
            image_path.write_bytes(image_bytes)

        try:
            connection.execute(
                """
                INSERT INTO complaints (
                    complaint_id, title, category, description, location,
                    image_filename, severity, suggested_department, status, submission_date,
                    citizen_name, citizen_phone, phone_verified, address, latitude,
                    longitude, location_method, landmark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    complaint_id, title, category, description, location,
                    image_filename, severity, department, "Submitted",
                    datetime.now().isoformat(timespec="seconds"),
                    citizen_name, citizen_phone, 0, address, latitude,
                    longitude, location_method, landmark,
                ),
            )
            connection.execute(
                """
                INSERT INTO complaint_status_history (
                    complaint_id, from_status, to_status, note, changed_at, changed_by
                ) VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    complaint_id, "Submitted", "Initial complaint submission", 
                    datetime.now().isoformat(timespec="seconds"), "System",
                ),
            )
            connection.execute(
                """
                INSERT INTO complaint_analysis (
                    complaint_id, predicted_category, suggested_severity,
                    recommended_department, short_summary, reasoning, analysis_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    complaint_id,
                    analysis["predicted_category"],
                    analysis["suggested_severity"],
                    analysis["recommended_department"],
                    analysis["short_summary"],
                    analysis["reasoning"],
                    analysis["analysis_source"],
                ),
            )
        except Exception:
            if image_path and image_path.exists():
                image_path.unlink()
            raise

    return redirect(url_for("success", complaint_id=complaint_id))


@app.route("/dashboard")
def dashboard():
    """Show live complaint statistics and a public, read-only complaint list."""
    category = request.args.get("category", "")
    severity = request.args.get("severity", "")
    status = request.args.get("status", "")

    # Only allow known filter values into the query; all values remain SQL parameters.
    if category not in COMPLAINT_CATEGORIES:
        category = ""
    if severity not in DASHBOARD_SEVERITIES:
        severity = ""
    if status not in DASHBOARD_STATUSES:
        status = ""

    conditions = []
    parameters = []
    if category:
        conditions.append("category = ?")
        parameters.append(category)
    if severity:
        conditions.append("severity = ?")
        parameters.append(severity)
    if status:
        conditions.append("status = ?")
        parameters.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with database_connection() as connection:
        statistics = connection.execute(
            """
            SELECT
                COUNT(*) AS total_complaints,
                SUM(CASE WHEN status IN ('Submitted', 'Pending', 'Under Review') THEN 1 ELSE 0 END) AS submitted_pending,
                SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved,
                SUM(CASE WHEN severity = 'High' THEN 1 ELSE 0 END) AS high_severity
            FROM complaints
            """
        ).fetchone()
        complaints = connection.execute(
            f"""
            SELECT complaint_id, title, category,
                   'Location shared privately' AS location,
                   severity, suggested_department, status, submission_date
            FROM complaints
            {where_clause}
            ORDER BY submission_date DESC, complaint_id DESC
            """,
            parameters,
        ).fetchall()

    stats = {key: statistics[key] or 0 for key in (
        "total_complaints", "submitted_pending", "in_progress", "resolved", "high_severity"
    )}
    return render_template(
        "dashboard.html",
        stats=stats,
        complaints=complaints,
        categories=COMPLAINT_CATEGORIES,
        severities=DASHBOARD_SEVERITIES,
        statuses=DASHBOARD_STATUSES,
        selected={"category": category, "severity": severity, "status": status},
        has_filters=bool(category or severity or status),
    )


@app.route("/success/<complaint_id>")
def success(complaint_id):
    """Show the confirmation for a complaint saved in the database."""
    with database_connection() as connection:
        complaint = connection.execute(
            """
            SELECT c.complaint_id, c.title, c.category, c.severity,
                   c.suggested_department, c.status,
                   a.predicted_category, a.suggested_severity,
                   a.recommended_department, a.short_summary,
                   a.reasoning, a.analysis_source
            FROM complaints AS c
            LEFT JOIN complaint_analysis AS a ON a.complaint_id = c.complaint_id
            WHERE c.complaint_id = ?
            """,
            (complaint_id,),
        ).fetchone()
    if complaint is None:
        abort(404)
    return render_template("success.html", complaint=complaint)


def admin_required(view_function):
    """Allow protected views only for an authenticated admin session."""
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_username"):
            return redirect(url_for("admin_login", next=request.path))
        return view_function(*args, **kwargs)
    return wrapped_view


@app.route("/track", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def track_complaint():
    """Let citizens view a complaint's public status without exposing contact details."""
    complaint = None
    updates = []
    error = None
    if request.method == "POST":
        complaint_id = request.form.get("complaint_id", "").strip().upper()
        if not re.fullmatch(r"CTA-\d{4}-[A-F0-9]{6}", complaint_id):
            error = "Enter a valid complaint reference ID."
        else:
            with database_connection() as connection:
                complaint = connection.execute(
                    """
                    SELECT complaint_id, category, submission_date, status
                    FROM complaints WHERE complaint_id = ?
                    """,
                    (complaint_id,),
                ).fetchone()
                if complaint:
                    updates = connection.execute(
                        """
                        SELECT to_status, changed_at
                        FROM complaint_status_history
                        WHERE complaint_id = ?
                        ORDER BY changed_at, history_id
                        """,
                        (complaint_id,),
                    ).fetchall()
            if complaint is None:
                error = "No complaint was found with that reference ID."
    return render_template("tracking.html", complaint=complaint, updates=updates, error=error)


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login():
    """Authenticate the configured single admin using a stored password hash."""
    if session.get("admin_username"):
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with database_connection() as connection:
            admin = connection.execute(
                "SELECT username, password_hash FROM admin_users WHERE username = ? AND is_active = 1",
                (username,),
            ).fetchone()
        try:
            password_matches = admin is not None and check_password_hash(admin["password_hash"], password)
        except (ValueError, TypeError):
            password_matches = False
        if password_matches:
            session.clear()
            session["admin_username"] = admin["username"]
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        error = "The username or password was not correct."
    with database_connection() as connection:
        admin_configured = connection.execute(
            "SELECT 1 FROM admin_users WHERE is_active = 1 LIMIT 1"
        ).fetchone() is not None
    return render_template("admin_login.html", error=error, admin_configured=admin_configured)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    with database_connection() as connection:
        complaints = connection.execute(
            """
            SELECT complaint_id, title, category, status, severity, submission_date
            FROM complaints ORDER BY submission_date DESC, complaint_id DESC
            """
        ).fetchall()
    return render_template("admin_dashboard.html", complaints=complaints)


@app.route("/admin/complaints/<complaint_id>")
@admin_required
def admin_complaint_detail(complaint_id):
    with database_connection() as connection:
        complaint = connection.execute(
            """
            SELECT c.*, a.predicted_category, a.suggested_severity AS ai_severity,
                   a.recommended_department AS ai_department, a.short_summary,
                   a.reasoning, a.analysis_source
            FROM complaints AS c
            LEFT JOIN complaint_analysis AS a ON a.complaint_id = c.complaint_id
            WHERE c.complaint_id = ?
            """,
            (complaint_id,),
        ).fetchone()
        history = connection.execute(
            """
            SELECT from_status, to_status, note, changed_at, changed_by
            FROM complaint_status_history WHERE complaint_id = ?
            ORDER BY changed_at DESC, history_id DESC
            """,
            (complaint_id,),
        ).fetchall()
    if complaint is None:
        abort(404)
    return render_template(
        "admin_complaint.html", complaint=complaint, history=history,
        manageable_statuses=MANAGEABLE_STATUSES,
    )


@app.route("/admin/complaints/<complaint_id>/status", methods=["POST"])
@admin_required
@limiter.limit("30 per minute")
def admin_update_status(complaint_id):
    new_status = request.form.get("status", "").strip()
    note = request.form.get("note", "").strip()
    if new_status not in MANAGEABLE_STATUSES:
        abort(400, description="Choose one of the listed complaint statuses.")
    if len(note) > 2000:
        abort(400, description="Keep the action note to 2,000 characters or fewer.")
    with database_connection() as connection:
        current = connection.execute(
            "SELECT status FROM complaints WHERE complaint_id = ?", (complaint_id,)
        ).fetchone()
        if current is None:
            abort(404)
        old_status = current["status"]
        if old_status == new_status and not note:
            flash("Choose a new status or add an action note.", "warning")
            return redirect(url_for("admin_complaint_detail", complaint_id=complaint_id))
        connection.execute(
            "UPDATE complaints SET status = ? WHERE complaint_id = ?",
            (new_status, complaint_id),
        )
        connection.execute(
            """
            INSERT INTO complaint_status_history (
                complaint_id, from_status, to_status, note, changed_at, changed_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                complaint_id, old_status, new_status, note,
                datetime.now().isoformat(timespec="seconds"), session["admin_username"],
            ),
        )
    flash("Complaint update saved.", "success")
    return redirect(url_for("admin_complaint_detail", complaint_id=complaint_id))


@app.route("/admin/complaints/<complaint_id>/photo")
@admin_required
def admin_complaint_photo(complaint_id):
    """Serve a stored complaint image only inside an authenticated admin session."""
    with database_connection() as connection:
        row = connection.execute(
            "SELECT image_filename FROM complaints WHERE complaint_id = ?", (complaint_id,)
        ).fetchone()
    if row is None:
        abort(404)
    filename = row["image_filename"]
    if not filename or Path(filename).name != filename:
        abort(404)
    image_path = UPLOAD_FOLDER / filename
    if not image_path.is_file():
        abort(404)
    image_bytes = image_path.read_bytes()
    if not valid_image_contents(image_bytes, filename):
        abort(404)
    content_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    response = send_file(BytesIO(image_bytes), mimetype=content_types[filename.rsplit(".", 1)[1].lower()])
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.errorhandler(413)
def request_too_large(_error):
    """Show a readable error when an uploaded request exceeds the app limit."""
    return render_report_form({"photo": "The upload is too large. Please choose an image under 5 MB."}, status_code=413)


@app.errorhandler(429)
def rate_limit_error(_error):
    return render_template("rate_limited.html"), 429


# Creating the table here means it is ready whenever Flask starts or imports this app.
init_db()


if __name__ == "__main__":
    app.run()
