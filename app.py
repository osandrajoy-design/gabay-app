from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    jsonify
)

import psycopg
from psycopg.rows import dict_row
from psycopg import errors as pg_errors
from supabase import create_client
import os
import base64
import uuid
import html
import threading
import json
import urllib.parse
import urllib.request

from datetime import datetime
from google import genai
from werkzeug.security import generate_password_hash, check_password_hash


# =========================================================
# GABAY APPLICATION
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get("GABAY_SECRET_KEY", "gabay-prototype-secret-key")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# =========================================================
# SUPABASE SETTINGS
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "").strip()
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "gabay-files").strip() or "gabay-files"

supabase = None

def get_supabase():
    global supabase
    if supabase is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return supabase

# Local folders are retained only as temporary buffers during a request.
PROFILE_FOLDER = os.path.join("/tmp", "gabay", "profiles")
VERIFICATION_FOLDER = os.path.join("/tmp", "gabay", "verification")
REPORT_FOLDER = os.path.join("/tmp", "gabay", "reports")
for _folder in (PROFILE_FOLDER, VERIFICATION_FOLDER, REPORT_FOLDER):
    os.makedirs(_folder, exist_ok=True)

def get_db():
    return psycopg.connect(
        SUPABASE_DB_URL,
        row_factory=dict_row,
        prepare_threshold=None,
        autocommit=False,
    )

def _sql(sql):
    # Existing GABAY SQL uses SQLite's ? placeholders. psycopg/Postgres uses %s.
    return sql.replace("?", "%s")

def execute(connection, sql, params=None):
    return connection.execute(_sql(sql), params or ())

def upload_storage(path, data, content_type):
    get_supabase().storage.from_(SUPABASE_STORAGE_BUCKET).upload(
        path, data, file_options={
            "content-type": content_type,
            "cache-control": "3600",
            "upsert": "true",
        }
    )
    return path

def signed_storage_url(path, expires=300):
    if not path:
        return None
    try:
        result = get_supabase().storage.from_(SUPABASE_STORAGE_BUCKET).create_signed_url(path, expires)
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")
    except Exception as error:
        print("Storage signed URL error:", error)
    return None

def delete_storage(path):
    if path:
        try:
            get_supabase().storage.from_(SUPABASE_STORAGE_BUCKET).remove([path])
        except Exception as error:
            print("Storage delete error:", error)

# =========================================================
# GEMINI CONFIGURATION
# =========================================================

# The key is loaded from the database when available so an administrator
# can change it from the dashboard without rebuilding the application/APK.
# The environment variable remains the initial fallback for first setup.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = None


GEMINI_MODEL = "gemini-3.6-flash"


def configure_gemini_client(api_key):

    """Apply a Gemini API key to the running application."""

    global GEMINI_API_KEY, gemini_client

    GEMINI_API_KEY = (api_key or "").strip()

    if GEMINI_API_KEY:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    else:

        gemini_client = None


def get_configured_gemini_key():

    """Return the persisted key, falling back to the environment key."""

    try:

        connection = get_db()

        setting = execute(connection, 
            "SELECT value FROM app_settings WHERE key = ?",
            ("gemini_api_key",)
        ).fetchone()

        connection.close()

        if setting and setting["value"]:
            return setting["value"].strip()

    except Exception as error:

        print("Gemini settings load error:", error)

    return (os.environ.get("GEMINI_API_KEY") or "").strip()

# =========================================================
# PROTOTYPE GOVERNMENT DEPARTMENTS
# =========================================================
#
# IMPORTANT:
# These are prototype contacts only.
# No actual SMS or email will be sent.
#
# =========================================================

GOVERNMENT_DEPARTMENTS = {

    "Philippine National Police": {
        "phone": "PROTOTYPE-PNP-NUMBER",
        "email": "pnp@gabay-prototype.gov.ph"
    },

    "Bureau of Fire Protection": {
        "phone": "PROTOTYPE-BFP-NUMBER",
        "email": "bfp@gabay-prototype.gov.ph"
    },

    "Local Disaster Risk Reduction and Management Office": {
        "phone": "PROTOTYPE-LDRRMO-NUMBER",
        "email": "ldrrmo@gabay-prototype.gov.ph"
    },

    "City/Municipal Health Office": {
        "phone": "PROTOTYPE-HEALTH-NUMBER",
        "email": "health@gabay-prototype.gov.ph"
    },

    "Social Welfare and Development Office": {
        "phone": "PROTOTYPE-DSWD-NUMBER",
        "email": "socialwelfare@gabay-prototype.gov.ph"
    },

    "Public Works and Engineering Office": {
        "phone": "PROTOTYPE-ENGINEERING-NUMBER",
        "email": "engineering@gabay-prototype.gov.ph"
    }

}


# =========================================================
# DATABASE CONNECTION
# =========================================================

# =========================================================
# CHECK GOVERNMENT ROLE
# =========================================================

def is_government_user():

    return (
        "user_id" in session
        and session.get("role") in [
            "admin",
            "government",
            "gov_employee",
            "government_employee"
        ]
    )


# =========================================================
# INITIALIZE DATABASE
# =========================================================

def initialize_database():
    """Optional one-time initialization. Never run automatically at import time.

    Vercel imports this module to create the Flask handler. Opening a PostgreSQL
    connection here can crash the entire serverless function before Flask starts.
    The schema is managed in Supabase SQL Editor/migrations.
    """
    connection = get_db()
    try:
        existing = execute(
            connection,
            "SELECT value FROM app_settings WHERE key = ?",
            ("gemini_api_key",),
        ).fetchone()
        if not existing and os.environ.get("GEMINI_API_KEY"):
            execute(
                connection,
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (
                    "gemini_api_key",
                    os.environ.get("GEMINI_API_KEY", "").strip(),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        else:
            connection.rollback()
    finally:
        connection.close()


# IMPORTANT: Do not call initialize_database() here.
# Vercel imports app.py during cold start, and a database connection at import
# time can make the entire function fail before Flask can serve a request.
# Gemini is also configured lazily when it is actually needed.


# =========================================================
# GEMINI SEVERITY ANALYSIS
# =========================================================

def analyze_request_with_gemini(
    request_id,
    category,
    description
):

    configure_gemini_client(get_configured_gemini_key())

    print()
    print("========================================")
    print("GABAY GEMINI BACKGROUND ANALYSIS")
    print("Request ID:", request_id)
    print("Category:", category)
    print("Description:", description)


    try:

        # =================================================
        # CHECK API KEY
        # =================================================

        if not GEMINI_API_KEY or not gemini_client:

            print(
                "Gemini AI: API key not configured."
            )

            connection = get_db()

            execute(connection, 
                """
                UPDATE requests

                SET priority = ?,
                    analysis_status = ?

                WHERE id = ?
                """,
                (
                    "Moderate",
                    "completed",
                    request_id
                )
            )

            connection.commit()

            connection.close()

            print(
                "FINAL PRIORITY: Moderate"
            )

            print(
                "========================================"
            )

            return


        # =================================================
        # GEMINI PROMPT
        # =================================================

        prompt = f"""

You are the emergency severity classification AI
for a Philippine government assistance system called GABAY.

Your job is to analyze the citizen's request and determine
how urgent and severe the situation is.

The citizen may write using:

- English
- Filipino
- Tagalog
- Bisaya/Cebuano
- mixed English and Filipino
- slang
- informal grammar
- uppercase/lowercase text
- short descriptions

You MUST understand the meaning of the situation rather
than relying only on keywords.

REQUEST CATEGORY:

{category}

CITIZEN DESCRIPTION:

{description}


SEVERITY LEVELS:


CRITICAL

Use Critical when the situation involves immediate danger
to life, possible death, serious injury, or an active emergency.

Examples include:

- heart attack
- person not responding
- person unconscious
- person not breathing
- drowning
- severe bleeding
- active house fire
- multiple houses on fire
- trapped people
- building collapse
- major accident
- people trapped in danger
- active flood threatening lives
- immediate threat to multiple people
- emergency requiring immediate response


HIGH

Use High when the situation is serious and requires urgent
government response but there is no clear immediate
life-threatening danger.


MODERATE

Use Moderate for legitimate government assistance requests
that require action but are not immediately dangerous.

Examples:

- normal assistance requests
- non-emergency medical concerns
- water service problems
- minor infrastructure problems
- ordinary complaints
- assistance requests that can wait


LOW

Use Low for non-urgent matters such as:

- information requests
- documents
- routine government services
- simple inquiries
- minor non-emergency concerns


IMPORTANT:

If a person may die or suffer serious harm without immediate
action, classify the request as CRITICAL.

For example:

"Inatake sa puso asawa ko di na siya nag rerepose"

means:

"My spouse had a heart attack and is no longer responding."

That MUST be classified as Critical.


Another example:

"TULONG YUNG ASAWA NALUNOD DI NA SIYA NAGRERESPONSE"

means:

"Help, my spouse drowned and is no longer responding."

That MUST be classified as Critical.


Another example:

"BIG HOUSE FIRE FIVE HOUSES ARE ALREADY ON FIRE"

must be classified as Critical.


RETURN ONLY ONE OF THESE FOUR WORDS:

Critical
High
Moderate
Low

Do not explain your answer.

Do not return punctuation.

Do not return additional text.

"""


        # =================================================
        # SEND TO GEMINI
        # =================================================

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )


        # =================================================
        # GET RESPONSE
        # =================================================

        severity_response = (
            response.text.strip()
            if response.text
            else ""
        )


        print(
            "Gemini severity response:",
            severity_response
        )


        # =================================================
        # NORMALIZE RESPONSE
        # =================================================

        severity_lower = (
            severity_response
            .lower()
            .strip()
        )


        if "critical" in severity_lower:

            priority = "Critical"

        elif "high" in severity_lower:

            priority = "High"

        elif "moderate" in severity_lower:

            priority = "Moderate"

        elif "low" in severity_lower:

            priority = "Low"

        else:

            print(
                "Gemini returned an unknown severity."
            )

            priority = "Moderate"


        # =================================================
        # UPDATE DATABASE
        # =================================================

        connection = get_db()

        execute(connection, 
            """
            UPDATE requests

            SET priority = ?,
                analysis_status = ?

            WHERE id = ?
            """,
            (
                priority,
                "completed",
                request_id
            )
        )

        connection.commit()

        connection.close()


        print(
            "FINAL PRIORITY:",
            priority
        )

        print(
            "========================================"
        )

        print()


    except Exception as error:

        print(
            "Gemini severity analysis error:",
            error
        )


        # =================================================
        # IF GEMINI FAILS
        # =================================================

        connection = get_db()

        execute(connection, 
            """
            UPDATE requests

            SET priority = ?,
                analysis_status = ?

            WHERE id = ?
            """,
            (
                "Moderate",
                "completed",
                request_id
            )
        )

        connection.commit()

        connection.close()


        print(
            "Gemini failed."
        )

        print(
            "FINAL PRIORITY: Moderate"
        )

        print(
            "========================================"
        )

        print()


def add_notification(user_id, title, message, request_id=None):
    connection = get_db()
    execute(connection, 
        "INSERT INTO notifications (user_id, request_id, title, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, request_id, title, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    connection.commit()
    connection.close()


def log_audit(user_id, action, request_id=None, details=""):
    connection = get_db()
    execute(connection, 
        "INSERT INTO audit_logs (user_id, request_id, action, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, request_id, action, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    connection.commit()
    connection.close()


def role_home_endpoint(role=None):
    role = role or session.get("role", "citizen")
    return "government_dashboard" if role in {"government", "gov_employee", "government_employee", "admin"} else "dashboard"


# =========================================================
# HOME
# =========================================================

@app.route("/health")
def health():
    # Safe deployment diagnostics: never return secrets.
    return jsonify({
        "ok": True,
        "supabase_url_configured": bool(SUPABASE_URL),
        "supabase_service_key_configured": bool(SUPABASE_SERVICE_ROLE_KEY),
        "supabase_db_url_configured": bool(SUPABASE_DB_URL),
        "gemini_key_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "storage_bucket": SUPABASE_STORAGE_BUCKET,
    })


@app.route("/")
def index():

    if "user_id" in session:

        if is_government_user():

            return redirect(
                url_for(
                    "government_dashboard"
                )
            )

        return redirect(
            url_for("dashboard")
        )


    return render_template(
        "index.html"
    )


# =========================================================
# REGISTER
# =========================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if request.method == "POST":

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        face_image = request.form.get(
            "face_image",
            ""
        )


        # =================================================
        # VALIDATION
        # =================================================

        if not full_name:
            return render_template("register.html", error="Full name is required."), 400

        if not email:
            return render_template("register.html", error="Email is required."), 400

        if not password:
            return render_template("register.html", error="Password is required."), 400


        # =================================================
        # SAVE PROFILE FACE IMAGE
        # =================================================

        profile_filename = None


        if face_image:
            try:
                image_data = face_image.split(",", 1)[-1]
                image_bytes = base64.b64decode(image_data, validate=True)

                # Profile photos are optional. A local/Supabase Storage failure
                # must not prevent the citizen account from being created.
                candidate_filename = f"{uuid.uuid4().hex}.jpg"
                upload_storage(
                    f"profiles/{candidate_filename}",
                    image_bytes,
                    "image/jpeg"
                )
                profile_filename = f"profiles/{candidate_filename}"
            except Exception as error:
                print("Profile image storage warning:", repr(error))
                profile_filename = None


        # =================================================
        # INSERT USER
        # =================================================

        connection = get_db()


        try:

            execute(connection, 
                """
                INSERT INTO users
                (
                    full_name,
                    email,
                    password,
                    role,
                    profile_image
                )

                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    full_name,
                    email,
                    generate_password_hash(password),
                    "citizen",
                    profile_filename
                )
            )


            connection.commit()

            connection.close()


            return redirect(
                url_for("login")
            )


        except pg_errors.UniqueViolation:

            connection.close()

            return render_template(
                "register.html",
                error="That email is already registered. Please log in instead."
            ), 409


    return render_template(
        "register.html"
    )


# =========================================================
# LOGIN
# =========================================================

def _authenticate_local_user(email, password, allowed_roles=None):
    """Authenticate against the local SQLite users table."""
    connection = get_db()
    try:
        user = execute(connection, "SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
    finally:
        connection.close()
    if not user:
        return None
    stored_password = user["password"] or ""
    try:
        valid = check_password_hash(stored_password, password)
    except (ValueError, TypeError):
        valid = stored_password == password
    if not valid:
        return None
    role = (user["role"] or "citizen").strip().lower()
    if allowed_roles is not None and role not in allowed_roles:
        return None
    return user

def _start_user_session(user):
    session.clear()
    session["user_id"] = user["id"]
    session["full_name"] = user["full_name"]
    session["role"] = (user["role"] or "citizen").strip().lower()
    session["profile_image"] = user["profile_image"]

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Compatibility route: all roles use the same login screen/tab system."""
    return redirect(url_for("login", role="admin"))

@app.route("/government/login", methods=["GET", "POST"])
def government_login():
    """Compatibility route: all roles use the same login screen/tab system."""
    return redirect(url_for("login", role="government"))

@app.route("/login", methods=["GET", "POST"])
def login():
    # One authentication system for every account type. The selected tab only
    # determines which role is allowed to sign in; the actual role is always
    # taken from the local users table.
    selected_role = request.args.get("role", "citizen").strip().lower()
    if request.method == "POST":
        selected_role = request.form.get("login_role", "citizen").strip().lower()

    role_aliases = {
        "citizen": {"citizen", "user"},
        "government": {"government", "gov_employee", "government_employee"},
        "admin": {"admin"},
    }
    if selected_role not in role_aliases:
        selected_role = "citizen"

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            error = "Please enter both your email and password."
        else:
            try:
                user = _authenticate_local_user(email, password, role_aliases[selected_role])
            except Exception as db_error:
                print("GABAY login database error:", repr(db_error))
                return render_template(
                    "login.html",
                    error="GABAY could not reach the database. Please try again shortly.",
                    selected_role=selected_role,
                ), 500

            if not user:
                role_names = {
                    "citizen": "citizen",
                    "government": "government personnel",
                    "admin": "administrator",
                }
                error = f"Invalid credentials or this account is not registered as {role_names[selected_role]}."
            else:
                # Upgrade legacy plain-text local passwords after successful login.
                stored_password = user["password"] or ""
                if not stored_password.startswith(("scrypt:", "pbkdf2:")):
                    connection = get_db()
                    execute(connection, "UPDATE users SET password = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
                    connection.commit()
                    connection.close()
                _start_user_session(user)

                actual_role = session["role"]
                if actual_role == "admin":
                    return redirect(url_for("admin_dashboard"))
                if actual_role in {"government", "gov_employee", "government_employee"}:
                    return redirect(url_for("government_dashboard"))
                return redirect(url_for("dashboard"))

    return render_template("login.html", error=error, selected_role=selected_role)


# =========================================================
# GOVERNMENT CRITICAL ALERT API
# =========================================================

@app.route(
    "/api/government/critical-alerts"
)
def government_critical_alerts():

    # CRITICAL ALARMS ARE FOR GOVERNMENT EMPLOYEES ONLY.
    # Administrators may access the dashboard/settings, but they must
    # never be assigned an emergency and must never receive the alarm.
    if session.get("role") == "admin":
        return jsonify({
            "success": True,
            "alert": None,
            "alarm_enabled": False
        })

    if not is_government_user():

        if "user_id" not in session:

            return jsonify({
                "error": "Unauthorized"
            }), 401

        return jsonify({
            "error": "Unauthorized"
        }), 403


    employee_id = session["user_id"]
    connection = get_db()

    try:

        # Serialize the claim operation so two employees polling at the
        # same time cannot claim the same Critical emergency.
        execute(connection, "BEGIN")

        assigned = execute(connection, 
            """
            SELECT id
            FROM requests
            WHERE priority = 'Critical'
              AND status != 'Cancelled'
              AND (forwarded IS NULL OR forwarded = 0)
              AND assigned_to = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (employee_id,)
        ).fetchone()


        if not assigned:

            candidate = execute(connection, 
                """
                SELECT id
                FROM requests
                WHERE priority = 'Critical'
                  AND status != 'Cancelled'
                  AND (forwarded IS NULL OR forwarded = 0)
                  AND (assigned_to IS NULL OR assigned_to = 0)
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()


            if candidate:

                claimed = execute(connection, 
                    """
                    UPDATE requests
                    SET assigned_to = ?
                    WHERE id = ?
                      AND (assigned_to IS NULL OR assigned_to = 0)
                      AND (forwarded IS NULL OR forwarded = 0)
                    """,
                    (
                        employee_id,
                        candidate["id"]
                    )
                )

                if claimed.rowcount == 1:

                    assigned = execute(connection, 
                        """
                        SELECT id
                        FROM requests
                        WHERE id = ?
                          AND assigned_to = ?
                          AND (forwarded IS NULL OR forwarded = 0)
                        """,
                        (
                            candidate["id"],
                            employee_id
                        )
                    ).fetchone()


        connection.commit()


        if not assigned:

            return jsonify({
                "success": True,
                "alert": None
            })


        critical = execute(connection, 
            """
            SELECT
                requests.id,
                requests.ticket_number,
                requests.category,
                requests.description,
                requests.priority,
                requests.status,
                requests.created_at,
                requests.forwarded,
                requests.address,
                requests.latitude,
                requests.longitude,
                requests.ai_report,
                requests.map_screenshot,
                requests.assigned_department,
                requests.population_affected,
                requests.households_affected,
                requests.urgency,
                requests.outage_type,
                requests.outage_started_at,
                requests.report_title,
                requests.verification_image,
                users.full_name,
                users.profile_image
            FROM requests
            LEFT JOIN users
                ON requests.user_id = users.id
            WHERE requests.id = ?
              AND requests.assigned_to = ?
              AND (requests.forwarded IS NULL OR requests.forwarded = 0)
            LIMIT 1
            """,
            (
                assigned["id"],
                employee_id
            )
        ).fetchone()


        if not critical:

            return jsonify({
                "success": True,
                "alert": None
            })


        return jsonify({

            "success": True,

            "alert": {

                "id": critical["id"],

                "ticket_number":
                    critical["ticket_number"],

                "category":
                    critical["category"],

                "description":
                    critical["description"],

                "priority":
                    critical["priority"],

                "status":
                    critical["status"],

                "created_at":
                    critical["created_at"],

                "forwarded":
                    bool(critical["forwarded"]),

                "full_name":
                    critical["full_name"],

                "profile_image":
                    critical["profile_image"],

                "verification_image":
                    critical["verification_image"],

                "address":
                    critical["address"],

                "latitude":
                    critical["latitude"],

                "longitude":
                    critical["longitude"],

                "ai_report":
                    critical["ai_report"],

                "map_screenshot": critical["map_screenshot"],
                "assigned_department": critical["assigned_department"],
                "population_affected": critical["population_affected"],
                "households_affected": critical["households_affected"],
                "urgency": critical["urgency"],
                "outage_type": critical["outage_type"],
                "outage_started_at": critical["outage_started_at"],
                "report_title": critical["report_title"],

                "assigned_to_me":
                    True

            }

        })

    finally:

        connection.close()


# =========================================================
# GOVERNMENT LIVE UPDATE API
# =========================================================
#
# The government dashboard will call this endpoint
# every few seconds.
#
# =========================================================

@app.route(
    "/api/government/dashboard-updates"
)
def government_dashboard_updates():

    if not is_government_user():

        if "user_id" not in session:

            return jsonify({
                "error": "Unauthorized"
            }), 401

        return jsonify({
            "error": "Unauthorized"
        }), 403


    connection = get_db()


    # =====================================================
    # TOTAL REQUESTS
    # =====================================================

    total_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # CRITICAL REQUESTS
    # =====================================================

    critical_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Critical'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # HIGH REQUESTS
    # =====================================================

    high_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'High'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # MODERATE REQUESTS
    # =====================================================

    moderate_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Moderate'
        """
    ).fetchone()["total"]


    # =====================================================
    # LOW REQUESTS
    # =====================================================

    low_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Low'
        """
    ).fetchone()["total"]


    # =====================================================
    # PENDING
    # =====================================================

    pending_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status = 'Pending'
        """
    ).fetchone()["total"]


    # =====================================================
    # IN PROGRESS
    # =====================================================

    in_progress_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status = 'In Progress'
        """
    ).fetchone()["total"]


    # =====================================================
    # RESOLVED
    # =====================================================

    resolved_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status = 'Resolved'
        """
    ).fetchone()["total"]


    # =====================================================
    # LATEST REQUEST
    # =====================================================

    latest_request = execute(connection, 
        """
        SELECT

            requests.id,

            requests.ticket_number,

            requests.category,

            requests.description,

            requests.priority,

            requests.status,

            requests.created_at,

            requests.forwarded,

            users.full_name

        FROM requests

        LEFT JOIN users
            ON requests.user_id = users.id

        ORDER BY requests.id DESC

        LIMIT 1
        """
    ).fetchone()


    # =====================================================
    # ACTIVE CRITICAL ALARM
    # =====================================================
    #
    # IMPORTANT:
    #
    # The alarm remains active if there is ANY Critical
    # request that has NOT been forwarded.
    #
    # =====================================================

    active_alarm = execute(connection, 
        """
        SELECT

            requests.id,

            requests.ticket_number,

            requests.category,

            requests.description,

            requests.priority,

            requests.status,

            requests.created_at,

            requests.forwarded,

            users.full_name

        FROM requests

        LEFT JOIN users
            ON requests.user_id = users.id

        WHERE requests.priority = 'Critical'

        AND requests.status != 'Cancelled'

        AND (
            requests.forwarded IS NULL
            OR requests.forwarded = 0
        )

        ORDER BY requests.id DESC

        LIMIT 1
        """
    ).fetchone()


    connection.close()


    latest_data = None


    if latest_request:

        latest_data = {

            "id":
                latest_request["id"],

            "ticket_number":
                latest_request["ticket_number"],

            "category":
                latest_request["category"],

            "description":
                latest_request["description"],

            "priority":
                latest_request["priority"],

            "status":
                latest_request["status"],

            "created_at":
                latest_request["created_at"],

            "forwarded":
                bool(latest_request["forwarded"]),

            "full_name":
                latest_request["full_name"]

        }


    alarm_data = None


    if active_alarm:

        alarm_data = {

            "id":
                active_alarm["id"],

            "ticket_number":
                active_alarm["ticket_number"],

            "category":
                active_alarm["category"],

            "description":
                active_alarm["description"],

            "priority":
                active_alarm["priority"],

            "status":
                active_alarm["status"],

            "created_at":
                active_alarm["created_at"],

            "forwarded":
                bool(active_alarm["forwarded"]),

            "full_name":
                active_alarm["full_name"]

        }


    return jsonify({

        "success": True,

        "total_requests":
            total_requests,

        "critical_requests":
            critical_requests,

        "high_requests":
            high_requests,

        "moderate_requests":
            moderate_requests,

        "low_requests":
            low_requests,

        "pending_requests":
            pending_requests,

        "in_progress_requests":
            in_progress_requests,

        "resolved_requests":
            resolved_requests,

        "latest_request":
            latest_data,

        "alarm_active":
            active_alarm is not None,

        "alarm_request":
            alarm_data

    })


# =========================================================
# GOVERNMENT DEPARTMENT API
# =========================================================

@app.route(
    "/api/government/departments"
)
def government_departments():

    if not is_government_user():

        if "user_id" not in session:

            return jsonify({
                "error": "Unauthorized"
            }), 401

        return jsonify({
            "error": "Unauthorized"
        }), 403


    departments = []


    for name, contact in GOVERNMENT_DEPARTMENTS.items():

        departments.append({

            "name":
                name,

            "phone":
                contact["phone"],

            "email":
                contact["email"]

        })


    return jsonify({

        "success": True,

        "prototype_mode": True,

        "departments": departments

    })


# =========================================================
# AI WRITTEN EMERGENCY REPORT
# =========================================================

def generate_ai_emergency_report(report):

    prompt = f"""
You are GABAY AI, an emergency coordination assistant
for a Philippine government assistance system.

Write a concise, professional incident report that a
government department can immediately act on.

Use ONLY the supplied information. Do not invent facts.
If information is unavailable, write "Not provided".

Ticket Number: {report["ticket_number"]}
Citizen: {report["full_name"]}
Category: {report["category"]}
Priority: {report["priority"]}
Status: {report["status"]}
Date Submitted: {report["created_at"]}
Address: {report["address"]}
Latitude: {report["latitude"]}
Longitude: {report["longitude"]}

Citizen Description:
{report["description"]}

Return a professional report with these headings:

GABAY EMERGENCY INCIDENT REPORT
Incident Summary
Location
Reported Situation
Priority Assessment
Recommended Immediate Action
Information for Responding Department

Keep it factual and concise.
"""


    configure_gemini_client(get_configured_gemini_key())

    if gemini_client and GEMINI_API_KEY:

        try:

            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt
            )

            text = (
                response.text.strip()
                if response.text
                else ""
            )

            if text:
                return text

        except Exception as error:

            print(
                "AI report generation error:",
                error
            )


    return f"""GABAY EMERGENCY INCIDENT REPORT

Incident Summary
Ticket Number: {report["ticket_number"]}
Citizen: {report["full_name"]}
Category: {report["category"]}
Date Submitted: {report["created_at"]}

Location
Address: {report["address"]}
Latitude: {report["latitude"]}
Longitude: {report["longitude"]}

Reported Situation
{report["description"]}

Priority Assessment
{report["priority"]}

Recommended Immediate Action
The assigned government department should verify the
reported situation and dispatch the appropriate response
according to its emergency procedures.

Information for Responding Department
This report was generated from the citizen-submitted
GABAY emergency request. The attached map screenshot
shows the reported GPS location.
"""


@app.route(
    "/api/government/ai-report/<int:request_id>"
)
def government_ai_report(request_id):

    if not is_government_user():

        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 403


    connection = get_db()

    try:

        report = execute(connection, 
            """
            SELECT requests.*, users.full_name
            FROM requests
            LEFT JOIN users
                ON requests.user_id = users.id
            WHERE requests.id = ?
            LIMIT 1
            """,
            (request_id,)
        ).fetchone()


        if not report:

            return jsonify({
                "success": False,
                "error": "Report not found."
            }), 404


        # Cancelled requests are immutable. Staff may read the already-saved
        # written report, but this endpoint must never generate/save new text
        # for a cancelled request.
        if report["status"] == "Cancelled" and not report["ai_report"]:
            return jsonify({
                "success": False,
                "error": "This report is cancelled and has no saved written report to generate."
            }), 409

        # Government personnel can generate an AI-written report for active requests.
        # Critical requests additionally appear in the emergency alarm workflow.

        if report["ai_report"]:

            return jsonify({
                "success": True,
                "report": report["ai_report"]
            })


        ai_report = generate_ai_emergency_report(
            report
        )


        execute(connection, 
            """
            UPDATE requests
            SET ai_report = ?
            WHERE id = ?
            """,
            (
                ai_report,
                request_id
            )
        )

        connection.commit()


        return jsonify({
            "success": True,
            "report": ai_report
        })

    finally:

        connection.close()


# =========================================================
# CITIZEN AI ASSISTANCE CHAT
# =========================================================

@app.route("/api/citizen/ai-assistance", methods=["POST"])
def citizen_ai_assistance():
    """One-on-one government-assistance chat for authenticated citizens."""
    if "user_id" not in session or session.get("role") not in {"citizen", "user"}:
        return jsonify({"success": False, "error": "Citizen access required."}), 403

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    history = payload.get("history") or []

    if not message:
        return jsonify({"success": False, "error": "Please enter a message."}), 400
    if len(message) > 4000:
        return jsonify({"success": False, "error": "Message is too long. Please keep it under 4,000 characters."}), 400

    # Keep the conversation focused on legitimate government/public-service help.
    safe_history = []
    for item in history[-10:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and text:
            safe_history.append(f"{role.title()}: {text[:4000]}")

    prompt = f"""
You are GABAY AI, a one-on-one public-service assistance guide for citizens in the Philippines.

Your job is to help the citizen understand and navigate government-related assistance, services,
requirements, reporting, requests, and next steps. Be warm, concise, practical, and easy to understand.
Do not claim to be a government employee. Do not invent government policies, fees, addresses, phone numbers,
application requirements, deadlines, or case statuses. If a detail needs confirmation from an agency, say so.
For emergencies or immediate danger, tell the citizen to contact the appropriate local emergency service
and use GABAY's Emergency Report feature when appropriate.

Conversation so far:
{chr(10).join(safe_history) if safe_history else "No previous messages."}

Citizen's latest message:
{message}

Reply directly to the citizen. Ask a short clarifying question when needed. Do not produce a formal incident
report unless the citizen specifically asks for help preparing one.
"""

    configure_gemini_client(get_configured_gemini_key())
    if gemini_client and GEMINI_API_KEY:
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt
            )
            text = (response.text or "").strip()
            if text:
                return jsonify({"success": True, "reply": text})
        except Exception as error:
            print("Citizen AI assistance error:", error)

    # Useful local fallback when no AI key is configured or the service is unavailable.
    lower = message.lower()
    if any(word in lower for word in ("emergency", "danger", "fire", "accident", "medical")):
        fallback = ("If this is an immediate emergency or someone is in danger, please contact your local "
                    "emergency service now. In GABAY, you can also use Emergency Report so the appropriate "
                    "government personnel can review the situation.")
    else:
        fallback = ("I can help you with government assistance, submitting or checking a GABAY request, "
                    "understanding what information to provide, and identifying the next step. Tell me what "
                    "government service or problem you need help with, and I’ll guide you.")

    return jsonify({"success": True, "reply": fallback, "fallback": True})


# =========================================================
# FORWARD REQUEST
# =========================================================
#
# IMPORTANT:
#
# This DOES NOT send an actual SMS or email.
#
# It only records the forwarding action in the database.
#
# =========================================================

@app.route(
    "/api/government/forward-request/<int:request_id>",
    methods=["POST"]
)
def forward_request(request_id):

    # =====================================================
    # GOVERNMENT ACCESS
    # =====================================================

    if not is_government_user():

        if "user_id" not in session:

            return jsonify({
                "success": False,
                "error": "Unauthorized"
            }), 401

        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 403


    # Critical emergency forwarding is an employee action. Admins can
    # manage the system but must never take ownership of an emergency.
    if session.get("role") not in [
        "government",
        "gov_employee",
        "government_employee"
    ]:
        return jsonify({
            "success": False,
            "error": "Only assigned government employees can forward a Critical emergency."
        }), 403


    # =====================================================
    # GET REQUEST DATA
    # =====================================================

    connection = get_db()


    report = execute(connection, 
        """
        SELECT *

        FROM requests

        WHERE id = ?
        """,
        (
            request_id,
        )
    ).fetchone()


    if not report:

        connection.close()

        return jsonify({

            "success": False,

            "error":
                "Report not found."

        }), 404


    # Critical reports are also handled by the alarm workflow, but the
    # normal government Report View can forward any request after review.


    # =====================================================
    # CHECK IF ALREADY FORWARDED
    # =====================================================

    if report["status"] == "Cancelled":
        connection.close()
        return jsonify({
            "success": False,
            "error": "Cancelled reports are view-only and cannot be forwarded."
        }), 409

    if report["forwarded"]:

        connection.close()

        return jsonify({

            "success": False,

            "error":
                "This report has already been forwarded."

        }), 400


    # =====================================================
    # GET JSON DATA
    # =====================================================

    data = request.get_json(
        silent=True
    )


    if not data:

        data = {}


    method = str(
        data.get(
            "method",
            ""
        )
    ).strip().lower()


    department = str(
        data.get(
            "department",
            ""
        )
    ).strip()


    # =====================================================
    # VALIDATE METHOD
    # =====================================================

    if method not in [
        "phone",
        "email"
    ]:

        connection.close()

        return jsonify({

            "success": False,

            "error":
                "Please select a valid forwarding method."

        }), 400


    # =====================================================
    # VALIDATE DEPARTMENT
    # =====================================================

    if department not in GOVERNMENT_DEPARTMENTS:

        connection.close()

        return jsonify({

            "success": False,

            "error":
                "Please select a valid government department."

        }), 400


    # =====================================================
    # GET DEPARTMENT CONTACT
    # =====================================================

    department_data = (
        GOVERNMENT_DEPARTMENTS[
            department
        ]
    )


    if method == "phone":

        target = department_data["phone"]

        method_name = "Phone Number"


    else:

        target = department_data["email"]

        method_name = "Email Address"


    # =====================================================
    # PREPARE AI WRITTEN REPORT
    # =====================================================

    ai_report = (
        report["ai_report"]
        if report["ai_report"]
        else generate_ai_emergency_report(report)
    )


    # =====================================================
    # SAVE MAP SCREENSHOT ATTACHMENT
    # =====================================================

    map_screenshot = None

    map_data = data.get(
        "map_screenshot",
        ""
    )


    if (
        isinstance(map_data, str)
        and map_data.startswith("data:image/")
    ):

        try:

            _, encoded = map_data.split(
                ",",
                1
            )

            image_bytes = base64.b64decode(
                encoded,
                validate=True
            )


            if len(image_bytes) <= 8 * 1024 * 1024:

                map_filename = (
                    report["ticket_number"]
                    + "_map.png"
                )

                map_screenshot = f"maps/{map_filename}"
                upload_storage(map_screenshot, image_bytes, "image/png")


        except Exception as error:

            print(
                "Map screenshot save error:",
                error
            )


    # =====================================================
    # SAVE WRITTEN REPORT + MAP ATTACHMENT
    # =====================================================
    #
    # The department receives one properly formatted HTML
    # report package. The AI-written report and the captured
    # emergency map are kept together in the same attachment.
    #
    # =====================================================

    report_filename = (
        report["ticket_number"]
        + "_emergency_report.html"
    )

    report_path = f"reports/{report_filename}"

    escaped_ai_report = html.escape(
        ai_report or "No AI report was generated."
    ).replace("\n", "<br>")

    map_attachment_html = ""

    if map_screenshot:
        try:
            map_bytes = get_supabase().storage.from_(SUPABASE_STORAGE_BUCKET).download(map_screenshot)
            map_base64 = base64.b64encode(map_bytes).decode("ascii")
        except Exception:
            map_base64 = ""

        map_attachment_html = f"""
        <section class="map-section">
            <h2>Emergency Location Map</h2>
            <p>The red marker identifies the reported emergency location.</p>
            <img src="data:image/png;base64,{map_base64}"
                 alt="Emergency location map with red alarm marker">
        </section>
        """
    else:
        map_attachment_html = """
        <section class="map-section">
            <h2>Emergency Location Map</h2>
            <p>Map screenshot was not available for this report.</p>
        </section>
        """

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GABAY Emergency Incident Report - {html.escape(report["ticket_number"])}</title>
<style>
body {{ font-family: Arial, sans-serif; color:#172033; background:#f5f7fb; margin:0; padding:32px; }}
.document {{ max-width:900px; margin:auto; background:#fff; padding:34px; border-radius:16px; box-shadow:0 8px 30px rgba(0,0,0,.08); }}
h1 {{ color:#0b3d91; margin-top:0; }}
h2 {{ color:#0b3d91; border-bottom:1px solid #e5e7eb; padding-bottom:7px; }}
.meta {{ background:#f8fafc; padding:16px; border-radius:10px; line-height:1.7; }}
.report {{ white-space:normal; line-height:1.65; }}
.map-section img {{ width:100%; max-width:820px; border:1px solid #d9e0ea; border-radius:10px; display:block; }}
.small {{ color:#667085; font-size:12px; }}
</style>
</head>
<body>
<div class="document">
<h1>GABAY Emergency Incident Report</h1>
<div class="meta">
<strong>Ticket:</strong> {html.escape(str(report["ticket_number"]))}<br>
<strong>Department:</strong> {html.escape(department)}<br>
<strong>Forwarding Method:</strong> {html.escape(method_name)}<br>
<strong>Forwarded At:</strong> {html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}<br>
</div>
<h2>AI Written Department Report</h2>
<div class="report">{escaped_ai_report}</div>
{map_attachment_html}
<p class="small">Generated and submitted by the GABAY government portal.</p>
</div>
</body>
</html>
"""

    upload_storage(
        report_path,
        report_html.encode("utf-8"),
        "text/html; charset=utf-8"
    )


    # =====================================================
    # RECORD FORWARDING
    # =====================================================

    forwarded_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


    cursor = execute(connection, 
        """
        UPDATE requests

        SET

            forwarded = 1,

            forward_method = ?,

            forward_target = ?,

            assigned_department = ?,

            forwarded_by = ?,

            forwarded_at = ?,

            status = 'In Progress',

            alarm_state = 'resolved',

            ai_report = ?,

            map_screenshot = ?,

            report_attachment = ?,

            report_submitted_at = ?

        WHERE id = ?

          AND (
                forwarded IS NULL
                OR forwarded = 0
              )

          AND assigned_to = ?
        """,
        (
            method_name,
            target,
            department,
            session["user_id"],
            forwarded_at,
            ai_report,
            map_screenshot,
            report_path,
            forwarded_at,
            request_id,
            session["user_id"]
        )
    )


    if cursor.rowcount != 1:

        connection.close()

        return jsonify({

            "success": False,

            "error":
                "This emergency was already handled by another employee."

        }), 409


    connection.commit()
    citizen_row = execute(connection, "SELECT user_id, ticket_number FROM requests WHERE id = ?", (request_id,)).fetchone()
    connection.close()
    if citizen_row:
        add_notification(citizen_row["user_id"], "Emergency forwarded", f"Your emergency report {citizen_row['ticket_number']} was forwarded to {department}.", request_id)
    log_audit(session["user_id"], "emergency_forwarded", request_id, department)

    # =====================================================
    # SUCCESS MESSAGE
    # =====================================================

    confirmation_message = (
        "This report has been forwarded to "
        + department
        + "."
    )


    print()
    print("========================================")
    print("GABAY PROTOTYPE FORWARD")
    print("========================================")
    print("Ticket:", report["ticket_number"])
    print("Department:", department)
    print("Method:", method_name)
    print("Target:", target)
    print("Forwarded by:", session.get("full_name"))
    print("Time:", forwarded_at)
    print()
    print("SIMULATION MODE")
    print("NO ACTUAL SMS OR EMAIL WAS SENT.")
    print("========================================")
    print()


    return jsonify({

        "success": True,

        "prototype_mode": True,

        "message":
            confirmation_message,

        "ticket_number":
            report["ticket_number"],

        "department":
            department,

        "method":
            method_name,

        "target":
            target,

        "forwarded_at":
            forwarded_at,

        "alarm_should_stop":
            True,

        "ai_report":
            ai_report,

        "map_screenshot":
            (
                url_for(
                    "report_file",
                    filename=os.path.basename(map_screenshot)
                )
                if map_screenshot
                else ""
            ),

        "report_attachment":
            url_for(
                "report_file",
                filename=os.path.basename(report_path)
            ),

        "report_submitted":
            True

    })


# =========================================================
# CITIZEN AI ASSISTANCE PAGE
# =========================================================

@app.route("/ai-assistance")
def citizen_ai_chat():
    """Dedicated one-on-one AI assistance page for authenticated citizens."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    if session.get("role") in {"government", "gov_employee", "government_employee"}:
        return redirect(url_for("government_dashboard"))

    return render_template(
        "citizen_ai_chat.html",
        full_name=session.get("full_name", "Citizen"),
        role=session.get("role", "citizen"),
        profile_image=session.get("profile_image")
    )


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    if session.get("role") in {"government", "gov_employee", "government_employee"}:
        return redirect(url_for("government_dashboard"))

    return render_template(
        "dashboard.html",

        full_name=session.get(
            "full_name",
            "Citizen"
        ),

        role=session.get(
            "role",
            "citizen"
        ),

        profile_image=session.get(
            "profile_image"
        )
    )


# =========================================================
# ADMIN AI SETTINGS
# =========================================================

def is_admin_user():

    return (
        "user_id" in session
        and session.get("role") == "admin"
    )


@app.route("/api/admin/gemini-settings", methods=["GET"])
def admin_gemini_settings():

    if not is_admin_user():
        return jsonify({
            "success": False,
            "error": "Administrator access required."
        }), 403

    key = get_configured_gemini_key()

    return jsonify({
        "success": True,
        "configured": bool(key),
        "masked_key": (
            "••••••••" + key[-4:]
            if len(key) >= 4
            else ("••••••••" if key else "")
        )
    })


@app.route("/api/admin/gemini-settings", methods=["POST"])
def update_admin_gemini_settings():

    if not is_admin_user():
        return jsonify({
            "success": False,
            "error": "Administrator access required."
        }), 403

    payload = request.get_json(silent=True) or {}
    api_key = str(payload.get("api_key", "")).strip()

    if not api_key:
        return jsonify({
            "success": False,
            "error": "Enter a Gemini API key."
        }), 400

    # Never echo the full key back to the browser.
    connection = get_db()
    execute(connection, 
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (
            "gemini_api_key",
            api_key,
            datetime.now().isoformat(timespec="seconds")
        )
    )
    connection.commit()
    connection.close()

    configure_gemini_client(api_key)

    return jsonify({
        "success": True,
        "message": "Gemini API key updated successfully.",
        "configured": True,
        "masked_key": "••••••••" + api_key[-4:]
    })


@app.route("/api/admin/gemini-settings/clear", methods=["POST"])
def clear_admin_gemini_settings():

    if not is_admin_user():
        return jsonify({
            "success": False,
            "error": "Administrator access required."
        }), 403

    connection = get_db()
    execute(connection, 
        "DELETE FROM app_settings WHERE key = ?",
        ("gemini_api_key",)
    )
    connection.commit()
    connection.close()

    configure_gemini_client("")

    return jsonify({
        "success": True,
        "message": "Gemini API key removed from GABAY."
    })



# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin/dashboard")
def admin_dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Unauthorized access.", 403

    connection = get_db()

    users_total = execute(connection, 
        "SELECT COUNT(*) AS total FROM users"
    ).fetchone()["total"]

    citizens_total = execute(connection, 
        "SELECT COUNT(*) AS total FROM users WHERE role = 'citizen'"
    ).fetchone()["total"]

    government_total = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM users
        WHERE role IN ('government', 'gov_employee', 'government_employee')
        """
    ).fetchone()["total"]

    admins_total = execute(connection, 
        "SELECT COUNT(*) AS total FROM users WHERE role = 'admin'"
    ).fetchone()["total"]

    total_reports = execute(connection, 
        "SELECT COUNT(*) AS total FROM requests WHERE status NOT IN ('Resolved', 'Cancelled', 'Closed')"
    ).fetchone()["total"]

    pending_reports = execute(connection, 
        "SELECT COUNT(*) AS total FROM requests WHERE status = 'Pending'"
    ).fetchone()["total"]

    active_reports = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status IN ('Under Review', 'In Progress', 'Action Taken')
        """
    ).fetchone()["total"]

    emergency_reports = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE requests.status NOT IN ('Resolved', 'Cancelled', 'Closed')
          AND (lower(category) LIKE '%emergency%'
           OR lower(category) IN (
                'medical emergency',
                'fire emergency',
                'crime / safety',
                'flood / disaster',
                'energy emergency',
                'rescue / accident'
           ))
        """
    ).fetchone()["total"]

    recent_reports = execute(connection, 
        """
        SELECT
            requests.id,
            requests.ticket_number,
            requests.category,
            requests.priority,
            requests.status,
            requests.assigned_department,
            requests.created_at,
            users.full_name
        FROM requests
        LEFT JOIN users ON users.id = requests.user_id
        WHERE requests.status NOT IN ('Resolved', 'Cancelled', 'Closed')
        ORDER BY requests.id DESC
        LIMIT 12
        """
    ).fetchall()

    users = execute(connection, 
        """
        SELECT id, full_name, email, role
        FROM users
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()

    departments = execute(connection, 
        """
        SELECT assigned_department, COUNT(*) AS total
        FROM requests
        WHERE status NOT IN ('Resolved', 'Cancelled', 'Closed')
          AND assigned_department IS NOT NULL
          AND trim(assigned_department) != ''
        GROUP BY assigned_department
        ORDER BY total DESC
        LIMIT 8
        """
    ).fetchall()

    connection.close()

    return render_template(
        "admin_dashboard.html",
        users_total=users_total,
        citizens_total=citizens_total,
        government_total=government_total,
        admins_total=admins_total,
        total_reports=total_reports,
        pending_reports=pending_reports,
        active_reports=active_reports,
        emergency_reports=emergency_reports,
        recent_reports=recent_reports,
        users=users,
        departments=departments,
        full_name=session.get("full_name", "GABAY Administrator"),
        role="admin"
    )


# =========================================================
# REQUEST HISTORY
# =========================================================

@app.route("/request-history")
def request_history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    role = session.get("role")
    if role not in {"admin", "government", "gov_employee", "government_employee"}:
        return "Unauthorized access.", 403

    connection = get_db()
    rows = execute(connection, 
        """
        SELECT
            requests.id,
            requests.ticket_number,
            requests.category,
            requests.description,
            requests.priority,
            requests.status,
            requests.address,
            requests.assigned_department,
            requests.created_at,
            requests.updated_at,
            users.full_name
        FROM requests
        LEFT JOIN users ON users.id = requests.user_id
        ORDER BY requests.id DESC
        """
    ).fetchall()
    connection.close()

    return render_template(
        "government_request_history.html",
        requests=rows,
        full_name=session.get("full_name", "GABAY Administrator" if role == "admin" else "Government Personnel"),
        profile_image=session.get("profile_image"),
        role=role
    )


# =========================================================
# GOVERNMENT DASHBOARD
# =========================================================

@app.route("/government/dashboard")
def government_dashboard():

    # =====================================================
    # GOVERNMENT ACCESS ONLY
    # =====================================================

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    if not is_government_user():

        return (
            "Unauthorized access.",
            403
        )


    connection = get_db()


    # =====================================================
    # TOTAL REQUESTS
    # =====================================================

    total_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # CRITICAL REQUESTS
    # =====================================================

    critical_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Critical'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # HIGH REQUESTS
    # =====================================================

    high_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'High'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # MODERATE REQUESTS
    # =====================================================

    moderate_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Moderate'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # LOW REQUESTS
    # =====================================================

    low_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE priority = 'Low'
          AND status NOT IN ('Resolved', 'Cancelled', 'Closed')
        """
    ).fetchone()["total"]


    # =====================================================
    # PENDING REQUESTS
    # =====================================================

    pending_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status = 'Pending'
        """
    ).fetchone()["total"]


    # =====================================================
    # IN PROGRESS REQUESTS
    # =====================================================

    in_progress_requests = execute(connection, 
        """
        SELECT COUNT(*) AS total
        FROM requests
        WHERE status = 'In Progress'
        """
    ).fetchone()["total"]


    # =====================================================
    # RECENT REQUESTS
    # =====================================================

    recent_requests = execute(connection, 
        """
        SELECT

            requests.id,

            requests.ticket_number,

            requests.category,

            requests.description,

            requests.priority,

            requests.status,

            requests.created_at,

            users.full_name

        FROM requests

        LEFT JOIN users
            ON requests.user_id = users.id

        WHERE requests.status NOT IN ('Resolved', 'Cancelled', 'Closed')

        ORDER BY requests.id DESC

        LIMIT 10
        """
    ).fetchall()


    # =====================================================
    # CRITICAL REQUEST LIST
    # =====================================================

    critical_request_list = execute(connection, 
        """
        SELECT

            requests.id,

            requests.ticket_number,

            requests.category,

            requests.description,

            requests.priority,

            requests.status,

            requests.created_at,

            requests.forwarded,

            users.full_name

        FROM requests

        LEFT JOIN users
            ON requests.user_id = users.id

        WHERE requests.priority = 'Critical'
          AND requests.status NOT IN ('Resolved', 'Cancelled', 'Closed')

        ORDER BY requests.id DESC

        LIMIT 20
        """
    ).fetchall()


    connection.close()


    # =====================================================
    # RENDER
    # =====================================================

    return render_template(
        "government_dashboard.html",

        total_requests=total_requests,

        critical_requests=critical_requests,

        high_requests=high_requests,

        moderate_requests=moderate_requests,

        low_requests=low_requests,

        pending_requests=pending_requests,

        in_progress_requests=in_progress_requests,


        recent_requests=recent_requests,

        critical_request_list=
            critical_request_list,

        full_name=session.get(
            "full_name",
            "Government Personnel"
        ),

        profile_image=session.get(
            "profile_image"
        ),

        role=session.get(
            "role",
            "government"
        )
    )


@app.route("/my-requests")
def my_requests():
    if "user_id" not in session or session.get("role") != "citizen": return redirect(url_for("login"))
    connection = get_db(); rows = execute(connection, "SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC", (session["user_id"],)).fetchall(); connection.close()
    return render_template("my_requests.html", requests=rows, full_name=session.get("full_name", "Citizen"))


@app.route("/my-requests/<int:request_id>/cancel", methods=["POST"])
def cancel_request(request_id):
    if "user_id" not in session or session.get("role") != "citizen":
        return redirect(url_for("login"))

    connection = get_db()
    row = execute(connection, 
        "SELECT id, ticket_number, status FROM requests WHERE id = ? AND user_id = ?",
        (request_id, session["user_id"])
    ).fetchone()

    if not row:
        connection.close()
        return redirect(url_for("my_requests"))

    if row["status"] in {"Resolved", "Cancelled", "Closed"}:
        connection.close()
        return redirect(url_for("my_requests"))

    execute(connection, 
        "UPDATE requests SET status = 'Cancelled' WHERE id = ? AND user_id = ?",
        (request_id, session["user_id"])
    )
    connection.commit()
    connection.close()

    add_notification(
        session["user_id"],
        "Request cancelled",
        f"Request {row['ticket_number']} has been cancelled by you.",
        request_id
    )
    return redirect(url_for("my_requests"))


@app.route("/notifications")
def notifications_page():
    if "user_id" not in session or session.get("role") != "citizen": return redirect(url_for("login"))
    connection = get_db(); rows = execute(connection, "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50", (session["user_id"],)).fetchall(); connection.close()
    return render_template("notifications.html", notifications=rows, full_name=session.get("full_name", "Citizen"))


@app.route("/services")
def services_page():
    return render_template(
        "portal_info.html",
        title="GABAY Services",
        heading="Government assistance, in one place.",
        text="Browse assistance categories, submit requests, track government action, and use GABAY assistance tools.",
        cards=[
            ("Medical", "Health and medical assistance."),
            ("Fire", "Fire and safety concerns."),
            ("Safety", "Public safety assistance."),
            ("Flood", "Flood and disaster assistance."),
            ("Electricity", "Energy and power outage assistance."),
            ("Infrastructure", "Infrastructure-related concerns."),
            ("Government", "Government service concerns."),
            ("Food", "Food assistance."),
            ("Transportation", "Transportation assistance."),
        ]
    )


@app.route("/about")
def about_page():
    return render_template("portal_info.html", title="About GABAY", heading="What is GABAY?", text="GABAY (Government Assistance, Basic-services Action & Yield) is a centralized citizen-support platform designed to make government assistance easier to access, submit, monitor, and manage. Its purpose is to connect citizens with appropriate services while giving authorized government personnel a clearer workflow for reviewing requests and responding to community needs.", cards=[("Our purpose", "Reduce unnecessary delays and confusion by bringing government assistance requests into one organized digital platform."), ("For citizens", "Provide a simpler way to discover services, submit requests, report emergencies, use location assistance, and track progress."), ("For government personnel", "Help authorized personnel review submitted information, assign departments, update statuses, and coordinate action."), ("Emergency response", "Provide a dedicated emergency reporting path with category selection, human verification, location information, and government-side processing."), ("AI assistance", "Help citizens understand services and organize information while keeping real government decisions and actions with authorized personnel."), ("Transparency and tracking", "Keep citizens informed through request status updates and notifications as their cases move through the workflow."), ("Why GABAY matters", "The platform is intended to make assistance more accessible and communication more organized between citizens and government offices."), ("Our vision", "Create a clearer digital bridge between people who need assistance and the personnel responsible for responding to them.")])


# =========================================================
# LOCATION / FACE VERIFICATION HELPERS
# =========================================================

def reverse_geocode_address(latitude, longitude):
    """Turn GPS coordinates into a human-readable address for staff/citizens.

    Coordinates remain internal routing data; the UI uses the resolved address.
    """
    try:
        query = urllib.parse.urlencode({"lat": latitude, "lon": longitude, "format": "jsonv2", "zoom": 18, "addressdetails": 1})
        req = urllib.request.Request(
            "https://nominatim.openstreetmap.org/reverse?" + query,
            headers={"User-Agent": "GABAY/1.0 emergency-location-resolver"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return (data.get("display_name") or "").strip()
    except Exception as error:
        print("Reverse geocoding error:", error)
        return ""


def validate_face_verification_image(raw_bytes):
    """Perform stronger non-biometric validation of the submitted face capture.

    This verifies that a real decodable image was captured, has a useful size,
    and is not a tiny/empty payload. It deliberately does not claim identity
    matching without a dedicated biometric verification service.
    """
    if not raw_bytes or len(raw_bytes) < 12_000:
        return False, "The face verification photo is too small. Please capture a clear face photo."
    if len(raw_bytes) > 8 * 1024 * 1024:
        return False, "The face verification photo is too large. Please capture a smaller photo."
    # JPEG/PNG signatures are enough for the prototype and avoid accepting arbitrary text.
    valid_signature = raw_bytes.startswith(b"\xff\xd8\xff") or raw_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    if not valid_signature:
        return False, "Please submit a valid camera image (JPEG or PNG)."
    return True, "Camera face capture accepted for human verification."


# =========================================================
# PUBLIC EMERGENCY REPORTING
# =========================================================

EMERGENCY_CATEGORIES = [
    ("Medical Emergency", "Person injured, unconscious, not responding, or needing urgent medical help."),
    ("Fire Emergency", "Active fire, smoke, explosion, or people trapped by fire."),
    ("Crime / Safety", "Immediate danger, violence, suspicious activity, or a person at risk."),
    ("Flood / Disaster", "Flooding, landslide, earthquake, storm damage, or another disaster."),
    ("Energy Emergency", "Power outage or electrical emergency affecting people or critical services."),
    ("Rescue / Accident", "Road accident, trapped person, drowning, or another rescue situation."),
]


@app.route("/emergency")
def public_emergency():
    # First page: show the emergency categories, just like Services.
    return render_template(
        "emergency.html",
        categories=EMERGENCY_CATEGORIES
    )


@app.route("/emergency/report")
def emergency_report_form_query():
    # Use a query parameter because categories such as "Crime / Safety"
    # contain a slash and should never be interpreted as a URL path segment.
    category = request.args.get("category", "").strip()
    allowed = {name for name, _ in EMERGENCY_CATEGORIES}
    if category not in allowed:
        return redirect(url_for("public_emergency"))
    return render_template(
        "emergency_form.html",
        category=category,
        category_description=dict(EMERGENCY_CATEGORIES)[category]
    )


@app.route("/emergency/report/<path:category>")
def emergency_report_form(category):
    category = category.strip()
    allowed = {name for name, _ in EMERGENCY_CATEGORIES}
    if category not in allowed:
        return redirect(url_for("public_emergency"))
    description = dict(EMERGENCY_CATEGORIES)[category]
    return render_template(
        "emergency_form.html",
        category=category,
        category_description=description
    )


@app.route("/api/public/emergency", methods=["POST"])
def submit_public_emergency():
    payload = request.get_json(silent=True) or {}
    required = ["category", "description", "address", "latitude", "longitude", "verification_image"]
    if any(not str(payload.get(k, "")).strip() for k in required):
        return jsonify({"success": False, "error": "Category, description, location, GPS and human verification are required."}), 400

    category = str(payload["category"]).strip()
    allowed = {name for name, _ in EMERGENCY_CATEGORIES}
    if category not in allowed:
        return jsonify({"success": False, "error": "Please select a valid emergency category."}), 400

    # Public reporters cannot choose or alter the emergency urgency.
    # The server assigns the initial public-emergency urgency; authorized
    # government personnel may adjust it later from the protected dashboard.
    urgency = "High"

    connection = get_db()

    ticket = "GABAY-EM-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()
    image_name = uuid.uuid4().hex + ".jpg"
    try:
        raw = str(payload["verification_image"]).split(",", 1)[-1]
        image_bytes = base64.b64decode(raw, validate=True)
        valid, verification_message = validate_face_verification_image(image_bytes)
        if not valid:
            connection.close()
            return jsonify({"success": False, "error": verification_message}), 400
        upload_storage(
            f"verification/{image_name}",
            image_bytes,
            "image/jpeg"
        )
    except Exception:
        connection.close()
        return jsonify({"success": False, "error": "Invalid verification image."}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_id = session.get("user_id")
    supplied_address = str(payload["address"]).strip()
    resolved_address = reverse_geocode_address(str(payload["latitude"]).strip(), str(payload["longitude"]).strip())
    final_address = resolved_address or supplied_address
    execute(connection, 
        "INSERT INTO public_emergencies (ticket_number, user_id, category, description, address, latitude, longitude, verification_image, urgency, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket, user_id, category, str(payload["description"]).strip(), final_address, str(payload["latitude"]).strip(), str(payload["longitude"]).strip(), image_name, urgency, now)
    )
    connection.commit()
    connection.close()

    return jsonify({"success": True, "ticket_number": ticket, "category": category, "status": "Pending", "alarm_state": "active"})


# =========================================================
# REQUEST PAGE
# =========================================================

@app.route(
    "/request/<category>"
)
def request_assistance(category):

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    return render_template(
        "request.html",
        category=category
    )


# =========================================================
# SUBMIT REQUEST
# =========================================================

@app.route(
    "/submit-request",
    methods=["POST"]
)
def submit_request():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    # =====================================================
    # GET FORM DATA
    # =====================================================

    category = request.form.get(
        "category",
        "Other"
    ).strip()


    description = request.form.get(
        "description",
        ""
    ).strip()


    contact_number = request.form.get(
        "contact_number",
        ""
    ).strip()


    email = request.form.get(
        "email",
        ""
    ).strip()


    address = request.form.get(
        "address",
        ""
    ).strip()


    latitude = request.form.get(
        "latitude",
        ""
    ).strip()


    longitude = request.form.get(
        "longitude",
        ""
    ).strip()


    verification_image = request.form.get("verification_image", "")
    population_affected = request.form.get("population_affected", "").strip()
    households_affected = request.form.get("households_affected", "").strip()
    urgency = request.form.get("urgency", "").strip()
    outage_type = request.form.get("outage_type", "").strip()
    outage_started_at = request.form.get("outage_started_at", "").strip()


    # =====================================================
    # VALIDATION
    # =====================================================

    if not description:

        return (
            "Please describe your problem."
        )


    if not contact_number:

        return (
            "Contact number is required."
        )


    if not address:

        return (
            "Please provide your location."
        )


    if not latitude or not longitude:

        return (
            "GPS location is required."
        )


    if not verification_image:

        return (
            "Face verification is required."
        )


    # =====================================================
    # TICKET NUMBER
    # =====================================================

    ticket_number = (

        "GABAY-"

        + datetime.now().strftime(
            "%Y%m%d"
        )

        + "-"

        + uuid.uuid4()
        .hex[:6]
        .upper()

    )


    # =====================================================
    # DATE/TIME
    # =====================================================

    created_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


    # =====================================================
    # SAVE VERIFICATION IMAGE
    # =====================================================

    verification_filename = None


    try:

        if "," in verification_image:

            image_data = (
                verification_image
                .split(",", 1)[1]
            )

        else:

            image_data = verification_image


        image_bytes = base64.b64decode(
            image_data, validate=True
        )
        valid, verification_message = validate_face_verification_image(image_bytes)
        if not valid:
            return verification_message


        verification_filename = (
            f"{uuid.uuid4().hex}.jpg"
        )


        upload_storage(
            f"verification/{verification_filename}",
            image_bytes,
            "image/jpeg"
        )


    except Exception as error:

        print(
            "Verification image error:",
            error
        )

        return (
            "There was a problem saving "
            "the verification image."
        )


    # =====================================================
    # INSERT REQUEST
    # =====================================================

    connection = get_db()


    cursor = execute(connection, 
        """
        INSERT INTO requests
        (
            ticket_number,

            user_id,

            category,

            description,

            contact_number,

            email,

            address,

            latitude,

            longitude,

            priority,

            status,

            verification_image,

            created_at,

            analysis_status,

            forwarded,

            forward_method,

            forward_target,

            forwarded_by,

            forwarded_at,
            alarm_state,
            population_affected,
            households_affected,
            urgency,
            outage_type,
            outage_started_at

        )

        VALUES (
    		?, ?, ?, ?, ?, ?, ?, ?, ?,
    		?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    		?, ?, ?, ?, ?, ?
	)
	RETURNING id
        """,
        (
            ticket_number,

            session["user_id"],

            category,

            description,

            contact_number,

            email,

            address,

            latitude,

            longitude,

            "Analyzing",

            "Pending",

            verification_filename,

            created_at,

            "processing",

            0,

            None,

            None,

            None,

            None,
            "active" if category.lower() in {"emergency", "electricity", "fire", "safety", "flood"} else "inactive",
            int(population_affected) if population_affected.isdigit() else None,
            int(households_affected) if households_affected.isdigit() else None,
            urgency or None,
            outage_type or None,
            outage_started_at or None
        )
    )


    request_id = cursor.fetchone()["id"]


    connection.commit()
    connection.close()
    add_notification(session["user_id"], "Request submitted", f"Your request {ticket_number} was received by GABAY.", request_id)

    # =====================================================
    # START GEMINI IN BACKGROUND
    # =====================================================

    analysis_thread = threading.Thread(

        target=analyze_request_with_gemini,

        args=(
            request_id,
            category,
            description
        ),

        daemon=True
    )


    analysis_thread.start()


    # =====================================================
    # SEND USER TO PROCESSING PAGE
    # =====================================================

    return redirect(
        url_for(
            "request_processing",
            request_id=request_id
        )
    )


# =========================================================
# REQUEST PROCESSING PAGE
# =========================================================

@app.route(
    "/request-processing/<int:request_id>"
)
def request_processing(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db()
    if is_government_user():
        request_data = execute(connection, "SELECT requests.*, users.full_name FROM requests LEFT JOIN users ON requests.user_id = users.id WHERE requests.id = ?", (request_id,)).fetchone()
    else:
        request_data = execute(connection, "SELECT * FROM requests WHERE id = ? AND user_id = ?", (request_id, session["user_id"])).fetchone()


    connection.close()


    if not request_data:

        return (
            "Request not found.",
            404
        )


    return render_template(
        "request_processing.html",
        request_data=request_data
    )


# =========================================================
# REQUEST STATUS API
# =========================================================

@app.route(
    "/api/request-status/<int:request_id>"
)
def request_status(request_id):

    if "user_id" not in session:

        return jsonify({
            "error": "Unauthorized"
        }), 401


    connection = get_db()


    request_data = execute(connection, 
        """
        SELECT

            id,

            ticket_number,

            category,

            description,

            priority,

            status,

            analysis_status,

            created_at

        FROM requests

        WHERE id = ?

        AND user_id = ?
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()


    connection.close()


    if not request_data:

        return jsonify({
            "error": "Request not found"
        }), 404


    is_ready = (

        request_data["analysis_status"]
        == "completed"

        and request_data["priority"]
        not in (
            None,
            "",
            "Analyzing"
        )

    )


    return jsonify({

        "ready":
            is_ready,

        "id":
            request_data["id"],

        "ticket_number":
            request_data["ticket_number"],

        "category":
            request_data["category"],

        "description":
            request_data["description"],

        "priority":
            request_data["priority"],

        "status":
            request_data["status"],

        "analysis_status":
            request_data["analysis_status"],

        "created_at":
            request_data["created_at"]

    })


# =========================================================
# REQUEST INFORMATION
# =========================================================

@app.route(
    "/request-info/<int:request_id>"
)
def request_info(request_id):

    # Government personnel use this same View link to open the full report.
    # Citizens remain restricted to their own requests.
    if is_government_user():
        return redirect(url_for("government_request_info", request_id=request_id))

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    connection = get_db()


    request_data = execute(connection, 
        """
        SELECT *

        FROM requests

        WHERE id = ?

        AND user_id = ?
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()


    connection.close()


    if not request_data:

        return (
            "Request not found.",
            404
        )


    ticket_number = (
        request_data["ticket_number"]
    )

    category = (
        request_data["category"]
    )

    description = (
        request_data["description"]
    )

    priority = (
        request_data["priority"]
    )

    status = (
        request_data["status"]
    )

    analysis_status = (
        request_data["analysis_status"]
    )

    created_at = (
        request_data["created_at"]
    )


    return render_template(
        "request_success.html",

        request_data=request_data,

        ticket_number=ticket_number,

        category=category,

        description=description,

        priority=priority,

        status=status,

        analysis_status=analysis_status,

        created_at=created_at
    )


# =========================================================
# GOVERNMENT REPORT ATTACHMENT
# =========================================================

@app.route("/uploads/reports/<path:filename>")
def report_file(filename):
    signed = signed_storage_url(f"reports/{filename}")
    if not signed:
        return "File unavailable", 404
    return redirect(signed)


# =========================================================
# PROFILE IMAGE
# =========================================================

@app.route("/uploads/profiles/<path:filename>")
def profile_image(filename):
    signed = signed_storage_url(f"profiles/{filename}")
    if not signed:
        return "File unavailable", 404
    return redirect(signed)


# =========================================================
# VERIFICATION IMAGE
# =========================================================

@app.route("/uploads/verification/<path:filename>")
def verification_image(filename):
    signed = signed_storage_url(f"verification/{filename}")
    if not signed:
        return "File unavailable", 404
    return redirect(signed)


@app.route("/api/citizen/notifications")
def citizen_notifications():
    if "user_id" not in session or session.get("role") != "citizen":
        return jsonify({"success": False, "error": "Citizen access required."}), 403
    connection = get_db()
    rows = execute(connection, "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50", (session["user_id"],)).fetchall()
    connection.close()
    return jsonify({"success": True, "notifications": [dict(r) for r in rows]})


@app.route("/api/citizen/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    if "user_id" not in session or session.get("role") != "citizen":
        return jsonify({"success": False, "error": "Citizen access required."}), 403
    connection = get_db()
    execute(connection, "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notification_id, session["user_id"]))
    connection.commit(); connection.close()
    return jsonify({"success": True})


@app.route("/government/request-info/<int:request_id>")
def government_request_info(request_id):
    if not is_government_user():
        return redirect(url_for("login"))
    connection = get_db()
    row = execute(connection, 
        """SELECT requests.*, users.full_name, users.email, users.profile_image
           FROM requests LEFT JOIN users ON requests.user_id = users.id
           WHERE requests.id = ?""", (request_id,)
    ).fetchone()
    connection.close()
    if not row:
        return "Report not found.", 404
    return render_template("government_request_info.html", report=dict(row))


@app.route("/api/government/request/<int:request_id>")
def government_request_detail(request_id):
    if not is_government_user():
        return jsonify({"success": False, "error": "Government access required."}), 403
    connection = get_db()
    row = execute(connection, "SELECT requests.*, users.full_name, users.email FROM requests LEFT JOIN users ON requests.user_id = users.id WHERE requests.id = ?", (request_id,)).fetchone()
    connection.close()
    if not row:
        return jsonify({"success": False, "error": "Report not found."}), 404
    return jsonify({"success": True, "report": dict(row)})


@app.route("/api/government/request/<int:request_id>/edit", methods=["POST"])
def edit_government_request(request_id):
    if session.get("role") not in {"government", "gov_employee", "government_employee"}:
        return jsonify({"success": False, "error": "Only government personnel may edit reports."}), 403
    connection = get_db()
    state = execute(connection, "SELECT status FROM requests WHERE id = ?", (request_id,)).fetchone()
    connection.close()
    if not state:
        return jsonify({"success": False, "error": "Report not found."}), 404
    if state["status"] == "Cancelled":
        return jsonify({"success": False, "error": "Cancelled reports are view-only. No changes can be made."}), 409

    payload = request.get_json(silent=True) or {}
    allowed = {"report_title", "description", "category", "address", "population_affected", "households_affected", "urgency", "assigned_department", "outage_type", "outage_started_at", "priority", "ai_report"}
    updates = {k: payload.get(k) for k in allowed if k in payload}
    if not updates:
        return jsonify({"success": False, "error": "No editable fields supplied."}), 400
    if updates.get("urgency") and updates["urgency"] not in {"Low", "Moderate", "High", "Critical"}:
        return jsonify({"success": False, "error": "Invalid urgency."}), 400
    if updates.get("outage_type") and updates["outage_type"] not in {"Total Power Outage", "Partial Power Outage", "Intermittent Power Outage", "Low Voltage / Power Fluctuation", "Planned / Scheduled Outage", "Unplanned / Unexpected Outage", "Unknown / Not Sure"}:
        return jsonify({"success": False, "error": "Invalid power outage type."}), 400
    sets = ", ".join(f"{k} = ?" for k in updates) + ", edited_at = ?, edited_by = ?"
    values = list(updates.values()) + [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session["user_id"], request_id]
    connection = get_db()
    cursor = execute(connection, f"UPDATE requests SET {sets} WHERE id = ?", values)
    if cursor.rowcount != 1:
        connection.close(); return jsonify({"success": False, "error": "Report not found."}), 404
    connection.commit(); connection.close()
    log_audit(session["user_id"], "report_edited", request_id, str(updates))
    return jsonify({"success": True, "message": "Report corrections saved.", "updated": updates})


@app.route("/api/government/request/<int:request_id>/department", methods=["POST"])
def save_department(request_id):
    if session.get("role") not in {"government", "gov_employee", "government_employee"}:
        return jsonify({"success": False, "error": "Government personnel access required."}), 403
    department = str((request.get_json(silent=True) or {}).get("department", "")).strip()
    if department not in GOVERNMENT_DEPARTMENTS:
        return jsonify({"success": False, "error": "Please select a valid department."}), 400
    connection = get_db(); cursor = execute(connection, "UPDATE requests SET assigned_department = ? WHERE id = ?", (department, request_id)); connection.commit(); connection.close()
    if cursor.rowcount != 1:
        return jsonify({"success": False, "error": "Report not found."}), 404
    log_audit(session["user_id"], "department_selected", request_id, department)
    return jsonify({"success": True, "department": department})


@app.route("/api/government/request/<int:request_id>/acknowledge", methods=["POST"])
def acknowledge_emergency(request_id):
    if session.get("role") not in {"government", "gov_employee", "government_employee"}:
        return jsonify({"success": False, "error": "Government personnel access required."}), 403
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_db(); cursor = execute(connection, "UPDATE requests SET alarm_state = 'acknowledged', acknowledged_at = ?, status = CASE WHEN status = 'Pending' THEN 'Under Review' ELSE status END WHERE id = ? AND priority = 'Critical'", (now, request_id)); connection.commit(); connection.close()
    if cursor.rowcount != 1: return jsonify({"success": False, "error": "Emergency not found."}), 404
    log_audit(session["user_id"], "emergency_acknowledged", request_id)
    return jsonify({"success": True, "alarm_state": "acknowledged"})


@app.route("/api/government/request/<int:request_id>/status", methods=["POST"])
def update_request_status(request_id):
    if session.get("role") not in {"government", "gov_employee", "government_employee"}:
        return jsonify({"success": False, "error": "Government personnel access required."}), 403
    status = str((request.get_json(silent=True) or {}).get("status", "")).strip()
    allowed = {"Pending", "Under Review", "In Progress", "Action Taken", "Resolved"}
    if status not in allowed: return jsonify({"success": False, "error": "Invalid status."}), 400
    connection = get_db(); row = execute(connection, "SELECT user_id, ticket_number FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row: connection.close(); return jsonify({"success": False, "error": "Request not found."}), 404
    execute(connection, "UPDATE requests SET status = ? WHERE id = ?", (status, request_id)); connection.commit(); connection.close()
    add_notification(row["user_id"], "Request status updated", f"Your request {row['ticket_number']} is now {status}.", request_id)
    log_audit(session["user_id"], "status_updated", request_id, status)
    return jsonify({"success": True, "status": status})


@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    if not is_admin_user(): return jsonify({"success": False, "error": "Administrator access required."}), 403
    connection = get_db(); rows = execute(connection, "SELECT id, full_name, email, role, profile_image FROM users ORDER BY id DESC").fetchall(); connection.close()
    return jsonify({"success": True, "users": [dict(r) for r in rows]})


@app.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
def admin_change_role(user_id):
    if not is_admin_user(): return jsonify({"success": False, "error": "Administrator access required."}), 403
    role = str((request.get_json(silent=True) or {}).get("role", "")).strip()
    if role not in {"citizen", "government", "gov_employee", "government_employee", "admin"}: return jsonify({"success": False, "error": "Invalid role."}), 400
    connection = get_db(); cursor = execute(connection, "UPDATE users SET role = ? WHERE id = ?", (role, user_id)); connection.commit(); connection.close()
    if cursor.rowcount != 1: return jsonify({"success": False, "error": "User not found."}), 404
    log_audit(session["user_id"], "role_changed", None, f"user={user_id}; role={role}")
    return jsonify({"success": True, "role": role})


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("index")
    )


# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)