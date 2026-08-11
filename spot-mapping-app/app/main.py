import hashlib
import hmac
import io
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "spot_missions.db"
SESSION_COOKIE_NAME = "spot_session"
SESSION_DAYS = 7
PASSWORD_ITERATIONS = 310000
DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_SUPER_ADMIN_USERNAME = "superadmin"
DEFAULT_UNIT_DEPARTMENT = "Generale"
DEFAULT_UNIT_CITY = "Generale"
ROLE_ADMIN = "admin"
ROLE_SUBADMIN = "subadmin"
ROLE_USER = "user"
ROLE_LABELS = {
    ROLE_ADMIN: "Admin",
    ROLE_SUBADMIN: "Subadmin",
    ROLE_USER: "Utente"
}
MIN_MISSION_COMPLETION_MESSAGES = 6
MAX_MISSION_COMPLETION_MESSAGES = 60
GRID_CELLS_PER_COMPLETION_STEP = 25
AREA_M2_PER_COMPLETION_STEP = 250
MISSION_MESSAGE_INTERVAL_SECONDS = 5

app = FastAPI(title="Spot Mapping App")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


class Position(BaseModel):
    latitude: float
    longitude: float


class MissionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    robot_position: Position
    polygon_vertices: list[Position] = Field(min_length=3)
    cell_size_m: float = Field(gt=0)
    grid_rotation_deg: float = Field(ge=0, le=180)
    area_m2: float = Field(gt=0)
    grid_cell_count: Optional[int] = Field(default=None, ge=0)


class MissionDeleteRequest(BaseModel):
    mission_ids: list[int] = Field(min_length=1)


def table_has_column(connection, table_name, column_name):
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(column[1] == column_name for column in columns)


def normalize_username(value):
    return (value or "").strip().lower()


def normalize_text_value(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_unit_value(value):
    return normalize_text_value(value)[:80]


def normalize_mission_name(value):
    return normalize_text_value(value)[:80]


def is_admin(user):
    return user["role"] == ROLE_ADMIN


def is_subadmin(user):
    return user["role"] == ROLE_SUBADMIN


def is_manager(user):
    return user["role"] in {ROLE_ADMIN, ROLE_SUBADMIN}


def get_role_label(role):
    return ROLE_LABELS.get(role, role)


def user_has_unit(user):
    return bool(user.get("department") and user.get("city"))


def get_next_reusable_user_id(connection):
    rows = connection.execute(
        """
        SELECT id
        FROM users
        ORDER BY id ASC
        """
    ).fetchall()
    expected_id = 1

    for row in rows:
        user_id = row[0]

        if user_id > expected_id:
            break

        if user_id == expected_id:
            expected_id += 1

    return expected_id


def estimate_grid_cell_count(area_m2, cell_size_m):
    if cell_size_m <= 0:
        return 1

    return max(1, math.ceil(area_m2 / (cell_size_m * cell_size_m)))


def normalize_grid_cell_count(grid_cell_count, area_m2, cell_size_m):
    if grid_cell_count is not None and grid_cell_count > 0:
        return int(grid_cell_count)

    return estimate_grid_cell_count(area_m2, cell_size_m)


def get_grid_cell_count(mission):
    try:
        grid_cell_count = mission["grid_cell_count"]
    except (KeyError, IndexError):
        grid_cell_count = None

    return normalize_grid_cell_count(
        grid_cell_count,
        mission["area_m2"],
        mission["cell_size_m"]
    )


def get_completion_message_count_from_values(area_m2, cell_size_m, grid_cell_count):
    cell_count = normalize_grid_cell_count(
        grid_cell_count,
        area_m2,
        cell_size_m
    )
    cell_steps = math.ceil(cell_count / GRID_CELLS_PER_COMPLETION_STEP)
    area_steps = math.ceil(area_m2 / AREA_M2_PER_COMPLETION_STEP)
    message_count = MIN_MISSION_COMPLETION_MESSAGES + cell_steps + area_steps

    return min(MAX_MISSION_COMPLETION_MESSAGES, max(
        MIN_MISSION_COMPLETION_MESSAGES,
        message_count
    ))


def get_completion_message_count(mission):
    return get_completion_message_count_from_values(
        mission["area_m2"],
        mission["cell_size_m"],
        get_grid_cell_count(mission)
    )


def get_estimated_duration_seconds(mission):
    return get_completion_message_count(mission) * MISSION_MESSAGE_INTERVAL_SECONDS


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds} s"

    minutes = seconds // 60
    remaining_seconds = seconds % 60

    if remaining_seconds == 0:
        return f"{minutes} min"

    return f"{minutes} min {remaining_seconds} s"


def get_status_label(status):
    labels = {
        "INVIATA": "In corso",
        "COMPLETATA": "Completata",
        "ABORTITA": "Abortita",
        "RIENTRO_BASE": "Rientro alla base"
    }

    return labels.get(status, status)


def get_mission_message_count(connection, mission_id):
    ensure_robot_messages_table(connection)

    return connection.execute(
        """
        SELECT COUNT(*)
        FROM robot_messages
        WHERE mission_id = ?
        """,
        (mission_id,)
    ).fetchone()[0]


def build_mission_progress(connection, mission):
    message_count = get_mission_message_count(connection, mission["id"])
    completion_message_count = get_completion_message_count(mission)
    remaining_messages = max(completion_message_count - message_count, 0)

    if mission["status"] != "INVIATA":
        remaining_messages = 0

    return {
        "mission_id": mission["id"],
        "status": mission["status"],
        "status_label": get_status_label(mission["status"]),
        "message_count": message_count,
        "completion_message_count": completion_message_count,
        "remaining_messages": remaining_messages,
        "estimated_duration_seconds": get_estimated_duration_seconds(mission),
        "estimated_remaining_seconds":
            remaining_messages * MISSION_MESSAGE_INTERVAL_SECONDS,
        "progress_percent": min(
            100,
            round((message_count / completion_message_count) * 100)
        ) if completion_message_count else 0
    }


def parse_stored_datetime(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def mission_elapsed_seconds(mission):
    created_at = parse_stored_datetime(mission["created_at"])

    if created_at is None:
        return 0

    now = datetime.now(created_at.tzinfo) if created_at.tzinfo else datetime.now()

    return max(0, int((now - created_at).total_seconds()))


def complete_mission_if_elapsed(connection, mission):
    if mission["status"] != "INVIATA":
        return False

    if mission_elapsed_seconds(mission) < get_estimated_duration_seconds(mission):
        return False

    ensure_robot_messages_table(connection)

    created_at = datetime.now().isoformat(timespec="seconds")
    connection.execute(
        """
        INSERT INTO robot_messages (
            mission_id,
            created_at,
            level,
            text
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            mission["id"],
            created_at,
            "INFO",
            "Missione completata automaticamente in base alla durata stimata."
        )
    )
    connection.execute(
        """
        UPDATE missions
        SET status = ?
        WHERE id = ?
        """,
        ("COMPLETATA", mission["id"])
    )

    return True


def complete_elapsed_missions(connection, user_id=None, mission_id=None):
    clauses = ["status = 'INVIATA'"]
    params = []

    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)

    if mission_id is not None:
        clauses.append("id = ?")
        params.append(mission_id)

    rows = connection.execute(
        f"""
        SELECT *
        FROM missions
        WHERE {" AND ".join(clauses)}
        """,
        params
    ).fetchall()

    changed = False

    for mission in rows:
        changed = complete_mission_if_elapsed(connection, mission) or changed

    if changed:
        connection.commit()

    return changed


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_ITERATIONS
    ).hex()

    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password, stored_hash):
    try:
        algorithm, iterations, salt, digest = stored_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        int(iterations)
    ).hex()

    return hmac.compare_digest(candidate, digest)


def ensure_auth_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            department TEXT,
            city TEXT
        )
        """
    )

    if not table_has_column(connection, "users", "department"):
        connection.execute("ALTER TABLE users ADD COLUMN department TEXT")

    if not table_has_column(connection, "users", "city"):
        connection.execute("ALTER TABLE users ADD COLUMN city TEXT")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT NOT NULL,
            city TEXT NOT NULL,
            subadmin_user_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(department, city),
            FOREIGN KEY(subadmin_user_id) REFERENCES users(id)
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS registration_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            department TEXT NOT NULL,
            city TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            decision_note TEXT,
            FOREIGN KEY(reviewed_by) REFERENCES users(id)
        )
        """
    )

    if not table_has_column(connection, "missions", "user_id"):
        connection.execute("ALTER TABLE missions ADD COLUMN user_id INTEGER")

    if not table_has_column(connection, "missions", "name"):
        connection.execute("ALTER TABLE missions ADD COLUMN name TEXT")
        connection.execute(
            """
            UPDATE missions
            SET name = 'Missione #' || id
            WHERE name IS NULL OR TRIM(name) = ''
            """
        )

    user_count = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    if user_count == 0:
        user_id = get_next_reusable_user_id(connection)
        connection.execute(
            """
            INSERT INTO users (
                id,
                username,
                password_hash,
                role,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                DEFAULT_SUPER_ADMIN_USERNAME,
                hash_password(DEFAULT_ADMIN_PASSWORD),
                ROLE_ADMIN,
                1,
                datetime.now().isoformat(timespec="seconds")
            )
        )
        admin_id = None
    else:
        legacy_admin = connection.execute(
            """
            SELECT id, username
            FROM users
            WHERE role = ?
              AND username != ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (ROLE_ADMIN, DEFAULT_SUPER_ADMIN_USERNAME)
        ).fetchone()

        admin_id = legacy_admin[0] if legacy_admin else None

        if legacy_admin is not None:
            connection.execute(
                """
                UPDATE users
                SET role = ?,
                    department = COALESCE(department, ?),
                    city = COALESCE(city, ?)
                WHERE id = ?
                """,
                (
                    ROLE_SUBADMIN,
                    DEFAULT_UNIT_DEPARTMENT,
                    DEFAULT_UNIT_CITY,
                    admin_id
                )
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO admin_units (
                    department,
                    city,
                    subadmin_user_id,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    DEFAULT_UNIT_DEPARTMENT,
                    DEFAULT_UNIT_CITY,
                    admin_id,
                    datetime.now().isoformat(timespec="seconds")
                )
            )

        super_admin = connection.execute(
            """
            SELECT id
            FROM users
            WHERE username = ?
            LIMIT 1
            """,
            (DEFAULT_SUPER_ADMIN_USERNAME,)
        ).fetchone()

        if super_admin is None:
            user_id = get_next_reusable_user_id(connection)
            connection.execute(
                """
                INSERT INTO users (
                    id,
                    username,
                    password_hash,
                    role,
                    active,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    DEFAULT_SUPER_ADMIN_USERNAME,
                    hash_password(DEFAULT_ADMIN_PASSWORD),
                    ROLE_ADMIN,
                    1,
                    datetime.now().isoformat(timespec="seconds")
                )
            )
        else:
            connection.execute(
                """
                UPDATE users
                SET role = ?,
                    active = 1,
                    department = NULL,
                    city = NULL
                WHERE id = ?
                """,
                (ROLE_ADMIN, super_admin[0])
            )

    connection.execute(
        """
        UPDATE users
        SET department = COALESCE(department, ?),
            city = COALESCE(city, ?)
        WHERE role IN (?, ?)
          AND (department IS NULL OR city IS NULL)
        """,
        (
            DEFAULT_UNIT_DEPARTMENT,
            DEFAULT_UNIT_CITY,
            ROLE_USER,
            ROLE_SUBADMIN
        )
    )

    if admin_id is None:
        first_subadmin = connection.execute(
            """
            SELECT id
            FROM users
            WHERE role = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (ROLE_SUBADMIN,)
        ).fetchone()
        admin_id = first_subadmin[0] if first_subadmin else None

    if admin_id is None:
        cursor = connection.execute(
            """
            SELECT id
            FROM users
            WHERE role = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (ROLE_ADMIN,)
        ).fetchone()
        admin_id = cursor[0] if cursor else None

    if admin_id is not None:
        connection.execute(
            """
            UPDATE missions
            SET user_id = ?
            WHERE user_id IS NULL
            """,
            (admin_id,)
        )

    connection.execute(
        "DELETE FROM sessions WHERE expires_at <= ?",
        (datetime.now().isoformat(timespec="seconds"),)
    )


async def read_form_data(request):
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1].strip() for key, values in parsed.items()}


def normalize_next_path(value):
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"

    return value


def redirect_to_login(request):
    query = urlencode({"next": request.url.path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def get_current_user(request):
    token = request.cookies.get(SESSION_COOKIE_NAME)

    if not token:
        return None

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    user = connection.execute(
        """
        SELECT
            users.id,
            users.username,
            users.role,
            users.active,
            users.department,
            users.city
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
          AND sessions.expires_at > ?
          AND users.active = 1
        """,
        (token, datetime.now().isoformat(timespec="seconds"))
    ).fetchone()

    connection.close()

    return dict(user) if user else None


def require_api_user(request):
    user = get_current_user(request)

    if user is None:
        raise HTTPException(status_code=401, detail="Login richiesto.")

    return user


def require_admin_user(request):
    user = require_api_user(request)

    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Accesso admin richiesto.")

    return user


def require_manager_user(request):
    user = require_api_user(request)

    if not is_manager(user):
        raise HTTPException(
            status_code=403,
            detail="Accesso admin o subadmin richiesto."
        )

    return user


def require_mission_user(request):
    user = require_api_user(request)

    if is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="Il superadmin gestisce solo account e dipartimenti."
        )

    return user


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    created_at = datetime.now()
    expires_at = created_at + timedelta(days=SESSION_DAYS)

    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        INSERT INTO sessions (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            token,
            user_id,
            created_at.isoformat(timespec="seconds"),
            expires_at.isoformat(timespec="seconds")
        )
    )
    connection.commit()
    connection.close()

    return token


def set_session_cookie(response, token):
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60
    )


def build_visible_mission_scope(user):
    if is_admin(user):
        return "1 = 1", []

    if is_subadmin(user) and user_has_unit(user):
        return (
            """
            (
                missions.user_id = ?
                OR (
                    owners.department = ?
                    AND owners.city = ?
                )
            )
            """,
            [user["id"], user["department"], user["city"]]
        )

    return "missions.user_id = ?", [user["id"]]


def get_visible_mission_for_user(connection, mission_id, user):
    where_clause, params = build_visible_mission_scope(user)

    return connection.execute(
        f"""
        SELECT missions.*
        FROM missions
        LEFT JOIN users AS owners ON owners.id = missions.user_id
        WHERE missions.id = ?
          AND {where_clause}
        """,
        [mission_id, *params]
    ).fetchone()


def get_owned_or_admin_mission(connection, mission_id, user):
    if is_admin(user):
        return connection.execute(
            "SELECT * FROM missions WHERE id = ?",
            (mission_id,)
        ).fetchone()

    return connection.execute(
        "SELECT * FROM missions WHERE id = ? AND user_id = ?",
        (mission_id, user["id"])
    ).fetchone()


def can_delete_mission(mission, user):
    return is_admin(user) or mission["user_id"] == user["id"]


def complete_visible_elapsed_missions(connection, user):
    if is_admin(user) or is_subadmin(user):
        return complete_elapsed_missions(connection)

    return complete_elapsed_missions(connection, user_id=user["id"])


def initialize_database():
    connection = sqlite3.connect(DB_PATH)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS missions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            created_at TEXT NOT NULL,
            robot_latitude REAL NOT NULL,
            robot_longitude REAL NOT NULL,
            polygon_vertices TEXT NOT NULL,
            cell_size_m REAL NOT NULL,
            grid_rotation_deg REAL NOT NULL,
            area_m2 REAL NOT NULL,
            grid_cell_count INTEGER,
            status TEXT NOT NULL
        )
        """
    )

    if not table_has_column(connection, "missions", "grid_cell_count"):
        connection.execute("ALTER TABLE missions ADD COLUMN grid_cell_count INTEGER")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS robot_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            command_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )

    ensure_robot_messages_table(connection)
    ensure_auth_tables(connection)

    connection.commit()
    connection.close()


@app.on_event("startup")
def startup():
    initialize_database()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_current_user(request)

    if user is not None:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next_path": normalize_next_path(request.query_params.get("next")),
            "error": None
        }
    )


@app.post("/login")
async def login(request: Request):
    form = await read_form_data(request)
    username = form.get("username", "").lower()
    password = form.get("password", "")
    next_path = normalize_next_path(form.get("next_path", "/"))

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    user = connection.execute(
        """
        SELECT id, username, password_hash, role, active, department, city
        FROM users
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    connection.close()

    if (
        user is None
        or user["active"] != 1
        or not verify_password(password, user["password_hash"])
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_path": next_path,
                "error": "Username o password non validi."
            },
            status_code=400
        )

    token = create_session(user["id"])
    response = RedirectResponse(url=next_path, status_code=303)
    set_session_cookie(response, token)
    return response


@app.get("/change-password", response_class=HTMLResponse)
def public_change_password_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={"error": None, "success": None}
    )


@app.post("/change-password", response_class=HTMLResponse)
async def public_change_password(request: Request):
    form = await read_form_data(request)
    username = normalize_username(form.get("username"))
    old_password = form.get("old_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    def render_change_password(message=None, success=None, status_code=400):
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={
                "error": message,
                "success": success
            },
            status_code=status_code
        )

    if not re.fullmatch(r"[a-z0-9_-]{3,32}", username):
        return render_change_password("Username non valido.")

    if len(new_password) < 6:
        return render_change_password(
            "La nuova password deve avere almeno 6 caratteri."
        )

    if new_password != confirm_password:
        return render_change_password("Le nuove password non coincidono.")

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    user = connection.execute(
        """
        SELECT id, password_hash, active
        FROM users
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    if (
        user is None
        or user["active"] != 1
        or not verify_password(old_password, user["password_hash"])
    ):
        connection.close()
        return render_change_password("Username o vecchia password non validi.")

    connection.execute(
        """
        UPDATE users
        SET password_hash = ?
        WHERE id = ?
        """,
        (hash_password(new_password), user["id"])
    )
    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    connection.commit()
    connection.close()

    return render_change_password(
        success="Password aggiornata. Puoi accedere con la nuova password.",
        status_code=200
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    user = get_current_user(request)

    if user is not None:
        return RedirectResponse(url="/", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    units = connection.execute(
        """
        SELECT id, department, city
        FROM admin_units
        ORDER BY department, city
        """
    ).fetchall()
    connection.close()

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "units": [dict(row) for row in units],
            "error": None,
            "success": None
        }
    )


@app.post("/register")
async def register_user(request: Request):
    form = await read_form_data(request)
    username = normalize_username(form.get("username"))
    password = form.get("password", "")
    confirm_password = form.get("confirm_password", "")

    try:
        unit_id = int(form.get("unit_id") or 0)
    except ValueError:
        unit_id = 0

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    units = connection.execute(
        """
        SELECT id, department, city
        FROM admin_units
        ORDER BY department, city
        """
    ).fetchall()

    def render_register_error(message):
        connection.close()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "units": [dict(row) for row in units],
                "error": message,
                "success": None
            },
            status_code=400
        )

    if not re.fullmatch(r"[a-z0-9_-]{3,32}", username):
        return render_register_error(
            "Username: usa 3-32 caratteri tra lettere, numeri, _ e -."
        )

    if len(password) < 6:
        return render_register_error(
            "La password deve avere almeno 6 caratteri."
        )

    if password != confirm_password:
        return render_register_error("Le password non coincidono.")

    unit = connection.execute(
        """
        SELECT id, department, city
        FROM admin_units
        WHERE id = ?
        """,
        (unit_id,)
    ).fetchone()

    if unit is None:
        return render_register_error("Seleziona un dipartimento e città validi.")

    existing_user = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if existing_user is not None:
        return render_register_error("Username già esistente.")

    pending_request = connection.execute(
        """
        SELECT id
        FROM registration_requests
        WHERE username = ?
          AND status = ?
        """,
        (username, "pending")
    ).fetchone()

    if pending_request is not None:
        return render_register_error(
            "Esiste già una richiesta in attesa per questo username."
        )

    connection.execute(
        """
        INSERT INTO registration_requests (
            username,
            password_hash,
            department,
            city,
            status,
            requested_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            hash_password(password),
            unit["department"],
            unit["city"],
            "pending",
            datetime.now().isoformat(timespec="seconds")
        )
    )
    connection.commit()
    connection.close()

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "units": [dict(row) for row in units],
            "error": None,
            "success": (
                "Richiesta inviata. Il subadmin del dipartimento e città "
                "selezionati dovrà approvarla."
            )
        }
    )


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)

    if token:
        connection = sqlite3.connect(DB_PATH)
        connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
        connection.commit()
        connection.close()

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    if is_admin(user):
        return RedirectResponse(url="/admin/users", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"current_user": user}
    )


@app.post("/missions")
def create_mission(mission: MissionCreate, request: Request):
    user = require_mission_user(request)
    created_at = datetime.now().isoformat(timespec="seconds")
    mission_name = normalize_mission_name(mission.name)

    if not mission_name:
        raise HTTPException(status_code=400, detail="Nome missione richiesto.")

    vertices_json = json.dumps(
        [vertex.model_dump() for vertex in mission.polygon_vertices]
    )
    grid_cell_count = normalize_grid_cell_count(
        mission.grid_cell_count,
        mission.area_m2,
        mission.cell_size_m
    )
    completion_message_count = get_completion_message_count_from_values(
        mission.area_m2,
        mission.cell_size_m,
        grid_cell_count
    )

    connection = sqlite3.connect(DB_PATH)

    cursor = connection.execute(
        """
        INSERT INTO missions (
            name,
            created_at,
            robot_latitude,
            robot_longitude,
            polygon_vertices,
            cell_size_m,
            grid_rotation_deg,
            area_m2,
            grid_cell_count,
            status,
            user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mission_name,
            created_at,
            mission.robot_position.latitude,
            mission.robot_position.longitude,
            vertices_json,
            mission.cell_size_m,
            mission.grid_rotation_deg,
            mission.area_m2,
            grid_cell_count,
            "INVIATA",
            user["id"]
        )
    )

    connection.commit()
    mission_id = cursor.lastrowid
    connection.close()

    return {
        "message": "Missione salvata correttamente.",
        "mission_id": mission_id,
        "name": mission_name,
        "status": "INVIATA",
        "status_label": get_status_label("INVIATA"),
        "grid_cell_count": grid_cell_count,
        "completion_message_count": completion_message_count,
        "estimated_duration_seconds":
            completion_message_count * MISSION_MESSAGE_INTERVAL_SECONDS
    }


def serialize_mission(row, current_user=None):
    mission = dict(row)
    mission["name"] = mission.get("name") or f"Missione #{mission['id']}"
    mission["polygon_vertices"] = json.loads(mission["polygon_vertices"])
    mission["grid_cell_count"] = get_grid_cell_count(mission)
    mission["completion_message_count"] = get_completion_message_count(mission)
    mission["estimated_duration_seconds"] = get_estimated_duration_seconds(mission)
    mission["status_label"] = get_status_label(mission["status"])

    if current_user is not None:
        mission["can_delete"] = can_delete_mission(mission, current_user)

    return mission


@app.get("/missions")
def get_missions(request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    where_clause, params = build_visible_mission_scope(user)

    complete_visible_elapsed_missions(connection, user)

    rows = connection.execute(
        f"""
        SELECT
            missions.id,
            missions.name,
            missions.created_at,
            missions.robot_latitude,
            missions.robot_longitude,
            missions.polygon_vertices,
            missions.cell_size_m,
            missions.grid_rotation_deg,
            missions.area_m2,
            missions.grid_cell_count,
            missions.status,
            missions.user_id,
            users.username,
            users.department,
            users.city
        FROM missions
        LEFT JOIN users AS owners ON owners.id = missions.user_id
        LEFT JOIN users ON users.id = missions.user_id
        WHERE {where_clause}
        ORDER BY missions.id DESC
        """,
        params
    ).fetchall()

    connection.close()

    return [serialize_mission(row, user) for row in rows]


@app.get("/missions/active")
def get_active_mission(request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    complete_elapsed_missions(connection, user_id=user["id"])

    row = connection.execute(
        """
        SELECT
            missions.id,
            missions.name,
            missions.created_at,
            missions.robot_latitude,
            missions.robot_longitude,
            missions.polygon_vertices,
            missions.cell_size_m,
            missions.grid_rotation_deg,
            missions.area_m2,
            missions.grid_cell_count,
            missions.status,
            missions.user_id,
            users.username
        FROM missions
        LEFT JOIN users ON users.id = missions.user_id
        WHERE missions.user_id = ?
          AND missions.status IN ('INVIATA', 'RIENTRO_BASE')
        ORDER BY
            CASE missions.status
                WHEN 'INVIATA' THEN 0
                ELSE 1
            END,
            missions.id DESC
        LIMIT 1
        """,
        (user["id"],)
    ).fetchone()

    connection.close()

    if row is None:
        return None

    return serialize_mission(row, user)


def delete_mission_rows(connection, mission_ids, user):
    clean_ids = sorted({int(mission_id) for mission_id in mission_ids if mission_id > 0})

    if not clean_ids:
        return []

    ensure_robot_messages_table(connection)

    placeholders = ",".join("?" for _ in clean_ids)
    existing_rows = connection.execute(
        f"SELECT id, user_id FROM missions WHERE id IN ({placeholders})",
        clean_ids
    ).fetchall()
    existing_ids = [row[0] for row in existing_rows]

    if not existing_ids:
        return []

    unauthorized_ids = [
        row[0] for row in existing_rows
        if not (is_admin(user) or row[1] == user["id"])
    ]

    if unauthorized_ids:
        raise HTTPException(
            status_code=403,
            detail="Puoi eliminare solo le tue missioni."
        )

    placeholders = ",".join("?" for _ in existing_ids)

    connection.execute(
        f"DELETE FROM robot_messages WHERE mission_id IN ({placeholders})",
        existing_ids
    )
    connection.execute(
        f"DELETE FROM robot_commands WHERE mission_id IN ({placeholders})",
        existing_ids
    )
    connection.execute(
        f"DELETE FROM missions WHERE id IN ({placeholders})",
        existing_ids
    )

    return existing_ids


@app.delete("/missions/{mission_id}")
def delete_mission(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)

    try:
        deleted_ids = delete_mission_rows(connection, [mission_id], user)
    except HTTPException:
        connection.close()
        raise

    if not deleted_ids:
        connection.close()
        raise HTTPException(status_code=404, detail="Missione non trovata.")

    connection.commit()
    connection.close()

    return {
        "message": "Missione eliminata.",
        "deleted_ids": deleted_ids
    }


@app.post("/admin/missions/delete")
def delete_selected_missions(payload: MissionDeleteRequest, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)

    try:
        deleted_ids = delete_mission_rows(connection, payload.mission_ids, user)
    except HTTPException:
        connection.close()
        raise

    connection.commit()
    connection.close()

    return {
        "message": "Missioni eliminate.",
        "deleted_ids": deleted_ids
    }


@app.get("/history", response_class=HTMLResponse)
def mission_history(request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    if is_admin(user):
        return RedirectResponse(url="/admin/users", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"current_user": user}
    )


@app.post("/missions/{mission_id}/abort")
def abort_mission(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    mission = get_owned_or_admin_mission(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    command_payload = {
        "mission_active": False,
        "return_to_start": True,
        "return_position": {
            "latitude": mission["robot_latitude"],
            "longitude": mission["robot_longitude"]
        }
    }

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS robot_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            command_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        INSERT INTO robot_commands (
            mission_id,
            created_at,
            command_type,
            payload,
            status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            mission_id,
            datetime.now().isoformat(timespec="seconds"),
            "ABORT_AND_RETURN",
            json.dumps(command_payload),
            "INVIATO"
        )
    )

    connection.execute(
        """
        UPDATE missions
        SET status = ?
        WHERE id = ?
        """,
        ("RIENTRO_BASE", mission_id)
    )

    connection.commit()
    connection.close()

    return {
        "message": "Comando di abort e rientro inviato.",
        "mission_id": mission_id,
        "status": "RIENTRO_BASE",
        "status_label": get_status_label("RIENTRO_BASE"),
        "command": command_payload
    }

import random


def ensure_robot_messages_table(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS robot_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            text TEXT NOT NULL
        )
        """
    )


@app.post("/missions/{mission_id}/messages/simulate")
def simulate_robot_message(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    ensure_robot_messages_table(connection)
    complete_elapsed_missions(
        connection,
        user_id=None if is_admin(user) else user["id"],
        mission_id=mission_id
    )

    mission = get_owned_or_admin_mission(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    status = mission["status"]
    new_status = status

    if status == "INVIATA":
        message_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM robot_messages
            WHERE mission_id = ?
            """,
            (mission_id,)
        ).fetchone()[0]
        completion_message_count = get_completion_message_count(mission)

        if message_count >= completion_message_count - 1:
            level = "INFO"
            text = "Missione completata. Area mappata correttamente."
            new_status = "COMPLETATA"
        else:
            possible_messages = [
                ("INFO", "Missione ricevuta. In attesa dell'elaborazione del perimetro."),
                ("INFO", "Telemetria GPS ricevuta correttamente."),
                ("INFO", "Configurazione griglia acquisita dal simulatore."),
                ("INFO", "Sensore LiDAR simulato operativo."),
                ("INFO", "Monitoraggio missione attivo."),
                ("WARNING", "Segnale GPS simulato con precisione ridotta."),
                ("WARNING", "Vegetazione densa rilevata nella zona selezionata.")
            ]
            level, text = random.choice(possible_messages)
    elif status == "RIENTRO_BASE":
        possible_messages = [
            (
                "WARNING",
                "mission_active = false. Rientro al punto iniziale in corso."
            )
        ]
        level, text = random.choice(possible_messages)
    else:
        possible_messages = [
            ("INFO", f"Missione in stato {status}. Nessuna attività operativa.")
        ]
        level, text = random.choice(possible_messages)

    created_at = datetime.now().isoformat(timespec="seconds")

    cursor = connection.execute(
        """
        INSERT INTO robot_messages (
            mission_id,
            created_at,
            level,
            text
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            mission_id,
            created_at,
            level,
            text
        )
    )

    if new_status != status:
        connection.execute(
            """
            UPDATE missions
            SET status = ?
            WHERE id = ?
            """,
            (new_status, mission_id)
        )

    progress_mission = dict(mission)
    progress_mission["status"] = new_status
    progress = build_mission_progress(connection, progress_mission)

    connection.commit()
    message_id = cursor.lastrowid
    connection.close()

    return {
        "id": message_id,
        "mission_id": mission_id,
        "created_at": created_at,
        "level": level,
        "text": text,
        "status": new_status,
        "status_label": get_status_label(new_status),
        "grid_cell_count": get_grid_cell_count(progress_mission),
        "completion_message_count": progress["completion_message_count"],
        "estimated_duration_seconds": progress["estimated_duration_seconds"],
        "message_count": progress["message_count"],
        "remaining_messages": progress["remaining_messages"],
        "estimated_remaining_seconds": progress["estimated_remaining_seconds"],
        "progress_percent": progress["progress_percent"]
    }


@app.get("/missions/{mission_id}/messages")
def get_robot_messages(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    ensure_robot_messages_table(connection)
    complete_elapsed_missions(
        connection,
        user_id=None if is_manager(user) else user["id"],
        mission_id=mission_id
    )

    mission = get_visible_mission_for_user(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    rows = connection.execute(
        """
        SELECT id, mission_id, created_at, level, text
        FROM robot_messages
        WHERE mission_id = ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (mission_id,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


@app.get("/missions/{mission_id}/progress")
def get_mission_progress(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    complete_elapsed_missions(
        connection,
        user_id=None if is_manager(user) else user["id"],
        mission_id=mission_id
    )

    mission = get_visible_mission_for_user(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    progress = build_mission_progress(connection, mission)
    connection.close()

    return progress


@app.post("/missions/{mission_id}/messages/abort-start")
def add_abort_start_message(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    ensure_robot_messages_table(connection)

    mission = get_owned_or_admin_mission(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Missione non trovata.")

    created_at = datetime.now().isoformat(timespec="seconds")
    text = (
        "mission_active = false. Comando Abort ricevuto: "
        "rientro obbligatorio al punto di partenza avviato."
    )

    connection.execute(
        """
        INSERT INTO robot_messages (mission_id, created_at, level, text)
        VALUES (?, ?, ?, ?)
        """,
        (mission_id, created_at, "WARNING", text)
    )

    connection.commit()
    connection.close()

    return {
        "mission_id": mission_id,
        "created_at": created_at,
        "level": "WARNING",
        "text": text
    }


@app.post("/missions/{mission_id}/messages/return-complete")
def add_return_complete_message(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    ensure_robot_messages_table(connection)

    mission = get_owned_or_admin_mission(connection, mission_id, user)

    if mission is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Missione non trovata.")

    created_at = datetime.now().isoformat(timespec="seconds")
    text = "Spot simulato rientrato correttamente al punto di partenza."

    connection.execute(
        """
        INSERT INTO robot_messages (mission_id, created_at, level, text)
        VALUES (?, ?, ?, ?)
        """,
        (mission_id, created_at, "INFO", text)
    )

    connection.execute(
        """
        UPDATE missions
        SET status = ?
        WHERE id = ?
        """,
        ("ABORTITA", mission_id)
    )

    connection.commit()
    connection.close()

    return {
        "mission_id": mission_id,
        "created_at": created_at,
        "level": "INFO",
        "text": text,
        "status": "ABORTITA",
        "status_label": get_status_label("ABORTITA")
    }


@app.get("/missions/{mission_id}")
def get_mission_detail(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    complete_elapsed_missions(
        connection,
        user_id=None if is_manager(user) else user["id"],
        mission_id=mission_id
    )

    where_clause, params = build_visible_mission_scope(user)

    row = connection.execute(
        f"""
        SELECT
            missions.id,
            missions.name,
            missions.created_at,
            missions.robot_latitude,
            missions.robot_longitude,
            missions.polygon_vertices,
            missions.cell_size_m,
            missions.grid_rotation_deg,
            missions.area_m2,
            missions.grid_cell_count,
            missions.status,
            missions.user_id,
            users.username,
            users.department,
            users.city
        FROM missions
        LEFT JOIN users AS owners ON owners.id = missions.user_id
        LEFT JOIN users ON users.id = missions.user_id
        WHERE missions.id = ?
          AND {where_clause}
        """,
        [mission_id, *params]
    ).fetchone()

    connection.close()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    return serialize_mission(row, user)


@app.get("/mission/{mission_id}", response_class=HTMLResponse)
def mission_detail_page(mission_id: int, request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    if is_admin(user):
        return RedirectResponse(url="/admin/users", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="mission_detail.html",
        context={"mission_id": mission_id, "current_user": user}
    )

@app.get("/missions/{mission_id}/report.pdf")
def download_mission_report_pdf(mission_id: int, request: Request):
    user = require_mission_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    where_clause, params = build_visible_mission_scope(user)

    mission = connection.execute(
        f"""
        SELECT
            missions.*,
            users.username,
            users.department,
            users.city
        FROM missions
        LEFT JOIN users AS owners ON owners.id = missions.user_id
        LEFT JOIN users ON users.id = missions.user_id
        WHERE missions.id = ?
          AND {where_clause}
        """,
        [mission_id, *params]
    ).fetchone()

    if mission is None:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Missione non trovata."
        )

    ensure_robot_messages_table(connection)

    messages = connection.execute(
        """
        SELECT created_at, level, text
        FROM robot_messages
        WHERE mission_id = ?
        ORDER BY id ASC
        """,
        (mission_id,)
    ).fetchall()

    connection.close()

    vertices = json.loads(mission["polygon_vertices"])
    grid_cell_count = get_grid_cell_count(mission)
    estimated_duration_seconds = get_estimated_duration_seconds(mission)

    buffer = io.BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm
    )

    styles = getSampleStyleSheet()
    story = []

    story.append(
        Paragraph(
            f"Report Missione Spot #{mission['id']}",
            styles["Title"]
        )
    )

    story.append(Spacer(1, 12))

    mission_data = [
        ["Nome missione", mission["name"] or f"Missione #{mission['id']}"],
        ["Data creazione", mission["created_at"].replace("T", " ")],
        ["Account", mission["username"] or "Non assegnato"],
        ["Dipartimento", mission["department"] or "-"],
        ["Città", mission["city"] or "-"],
        ["Stato missione", get_status_label(mission["status"])],
        ["Area selezionata", f"{mission['area_m2']:.1f} m²"],
        ["Dimensione celle", f"{mission['cell_size_m']} m"],
        ["Quadrati griglia", str(grid_cell_count)],
        ["Durata stimata simulata", format_duration(estimated_duration_seconds)],
        ["Rotazione griglia", f"{mission['grid_rotation_deg']}°"],
        ["Numero vertici poligono", str(len(vertices))],
        [
            "Posizione GPS iniziale",
            f"{mission['robot_latitude']:.6f}, "
            f"{mission['robot_longitude']:.6f}"
        ]
    ]

    mission_table = Table(
        mission_data,
        colWidths=[5 * cm, 11 * cm]
    )

    mission_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E5EEE8")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C4BC")),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 7)
        ])
    )

    story.append(mission_table)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Vertici georeferenziati del poligono", styles["Heading2"]))

    vertices_data = [["N°", "Latitudine", "Longitudine"]]

    for index, vertex in enumerate(vertices, start=1):
        vertices_data.append([
            str(index),
            f"{vertex['latitude']:.6f}",
            f"{vertex['longitude']:.6f}"
        ])

    vertices_table = Table(
        vertices_data,
        colWidths=[2 * cm, 7 * cm, 7 * cm]
    )

    vertices_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A2D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C4BC")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("PADDING", (0, 0), (-1, -1), 6)
        ])
    )

    story.append(vertices_table)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Messaggi Spot simulati", styles["Heading2"]))

    if messages:
        messages_data = [["Data e ora", "Livello", "Messaggio"]]

        for message in messages:
            messages_data.append([
                message["created_at"].replace("T", " "),
                message["level"],
                Paragraph(message["text"], styles["BodyText"])
            ])

        messages_table = Table(
            messages_data,
            colWidths=[4 * cm, 2.5 * cm, 9.5 * cm],
            repeatRows=1
        )

        messages_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A2D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C4BC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 6)
            ])
        )

        story.append(messages_table)
    else:
        story.append(
            Paragraph(
                "Nessun messaggio simulato associato alla missione.",
                styles["BodyText"]
            )
        )

    document.build(story)

    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="report_missione_{mission_id}.pdf"'
        }
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    if not is_manager(user):
        raise HTTPException(
            status_code=403,
            detail="Accesso admin o subadmin richiesto."
        )

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    selected_department = normalize_unit_value(
        request.query_params.get("department")
    )
    selected_city = normalize_unit_value(request.query_params.get("city"))

    current_account = connection.execute(
        """
        SELECT
            users.id,
            users.username,
            users.role,
            users.active,
            users.department,
            users.city,
            users.created_at
        FROM users
        WHERE users.id = ?
        """,
        (user["id"],)
    ).fetchone()

    account_clauses = ["users.role IN (?, ?)"]
    account_params = [ROLE_USER, ROLE_SUBADMIN]

    if is_admin(user):
        if selected_department:
            account_clauses.append("users.department = ?")
            account_params.append(selected_department)

        if selected_city:
            account_clauses.append("users.city = ?")
            account_params.append(selected_city)
    else:
        account_clauses = [
            "users.role = ?",
            "users.department = ?",
            "users.city = ?"
        ]
        account_params = [ROLE_USER, user["department"], user["city"]]

    users = connection.execute(
        f"""
        SELECT
            users.id,
            users.username,
            users.role,
            users.active,
            users.department,
            users.city,
            users.created_at
        FROM users
        WHERE {" AND ".join(account_clauses)}
        ORDER BY users.department, users.city, users.role DESC, users.id ASC
        """,
        account_params
    ).fetchall()

    units = connection.execute(
        """
        SELECT
            admin_units.id,
            admin_units.department,
            admin_units.city,
            admin_units.created_at,
            users.id AS subadmin_id,
            users.username AS subadmin_username,
            users.active AS subadmin_active
        FROM admin_units
        JOIN users ON users.id = admin_units.subadmin_user_id
        ORDER BY admin_units.department, admin_units.city
        """
    ).fetchall()

    department_rows = connection.execute(
        """
        SELECT department
        FROM admin_units
        UNION
        SELECT department
        FROM users
        WHERE department IS NOT NULL AND TRIM(department) != ''
        ORDER BY department
        """
    ).fetchall()
    city_rows = connection.execute(
        """
        SELECT city
        FROM admin_units
        UNION
        SELECT city
        FROM users
        WHERE city IS NOT NULL AND TRIM(city) != ''
        ORDER BY city
        """
    ).fetchall()

    pending_requests = []

    if is_subadmin(user):
        pending_requests = connection.execute(
            """
            SELECT id, username, department, city, requested_at
            FROM registration_requests
            WHERE status = ?
              AND department = ?
              AND city = ?
            ORDER BY requested_at ASC, id ASC
            """,
            ("pending", user["department"], user["city"])
        ).fetchall()

    connection.close()

    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={
            "current_user": user,
            "current_admin": dict(current_account) if current_account else None,
            "users": [dict(row) for row in users],
            "units": [dict(row) for row in units],
            "departments": [row["department"] for row in department_rows],
            "cities": [row["city"] for row in city_rows],
            "pending_requests": [dict(row) for row in pending_requests],
            "selected_department": selected_department,
            "selected_city": selected_city,
            "role_labels": ROLE_LABELS,
            "is_global_admin": is_admin(user),
            "is_subadmin": is_subadmin(user),
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success")
        }
    )


@app.post("/admin/units")
async def admin_create_unit_subadmin(request: Request):
    require_admin_user(request)
    form = await read_form_data(request)
    username = normalize_username(form.get("username"))
    password = form.get("password", "")
    department = normalize_unit_value(form.get("department"))
    city = normalize_unit_value(form.get("city"))

    if not re.fullmatch(r"[a-z0-9_-]{3,32}", username):
        query = urlencode({
            "error": "Username: usa 3-32 caratteri tra lettere, numeri, _ e -."
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if len(password) < 6:
        query = urlencode({"error": "La password deve avere almeno 6 caratteri."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if len(department) < 2 or len(city) < 2:
        query = urlencode({
            "error": "Dipartimento e città devono avere almeno 2 caratteri."
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    existing_unit = connection.execute(
        """
        SELECT id
        FROM admin_units
        WHERE department = ?
          AND city = ?
        """,
        (department, city)
    ).fetchone()

    if existing_unit is not None:
        connection.close()
        query = urlencode({
            "error": "Esiste già un subadmin per questo dipartimento e città."
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    try:
        subadmin_id = get_next_reusable_user_id(connection)
        cursor = connection.execute(
            """
            INSERT INTO users (
                id,
                username,
                password_hash,
                role,
                active,
                created_at,
                department,
                city
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subadmin_id,
                username,
                hash_password(password),
                ROLE_SUBADMIN,
                1,
                datetime.now().isoformat(timespec="seconds"),
                department,
                city
            )
        )
        connection.execute(
            """
            INSERT INTO admin_units (
                department,
                city,
                subadmin_user_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                department,
                city,
                subadmin_id,
                datetime.now().isoformat(timespec="seconds")
            )
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.close()
        query = urlencode({
            "error": "Username già esistente o coppia dipartimento/città già assegnata."
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.close()

    query = urlencode({
        "success": (
            f"Subadmin {username} creato per "
            f"{department} / {city}."
        )
    })
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


@app.post("/admin/units/{unit_id}/delete")
def admin_delete_unit(unit_id: int, request: Request):
    user = require_admin_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    unit = connection.execute(
        """
        SELECT
            admin_units.id,
            admin_units.department,
            admin_units.city,
            admin_units.subadmin_user_id,
            users.username AS subadmin_username
        FROM admin_units
        JOIN users ON users.id = admin_units.subadmin_user_id
        WHERE admin_units.id = ?
        """,
        (unit_id,)
    ).fetchone()

    if unit is None:
        connection.close()
        query = urlencode({"error": "Dipartimento non trovato."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    user_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE role = ?
          AND department = ?
          AND city = ?
        """,
        (ROLE_USER, unit["department"], unit["city"])
    ).fetchone()[0]

    if user_count > 0:
        connection.close()
        query = urlencode({
            "error": (
                "Non puoi eliminare il dipartimento finché contiene utenti. "
                "Elimina prima gli account utente collegati."
            )
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.execute(
        "DELETE FROM sessions WHERE user_id = ?",
        (unit["subadmin_user_id"],)
    )
    connection.execute(
        "UPDATE missions SET user_id = NULL WHERE user_id = ?",
        (unit["subadmin_user_id"],)
    )
    connection.execute(
        """
        UPDATE registration_requests
        SET reviewed_by = NULL
        WHERE reviewed_by = ?
        """,
        (unit["subadmin_user_id"],)
    )
    connection.execute(
        """
        UPDATE registration_requests
        SET status = ?,
            reviewed_at = ?,
            reviewed_by = ?,
            decision_note = ?
        WHERE status = ?
          AND department = ?
          AND city = ?
        """,
        (
            "rejected",
            datetime.now().isoformat(timespec="seconds"),
            user["id"],
            "Dipartimento eliminato dall'admin.",
            "pending",
            unit["department"],
            unit["city"]
        )
    )
    connection.execute("DELETE FROM admin_units WHERE id = ?", (unit_id,))
    connection.execute(
        "DELETE FROM users WHERE id = ?",
        (unit["subadmin_user_id"],)
    )
    connection.commit()
    connection.close()

    query = urlencode({
        "success": (
            f"Dipartimento {unit['department']} / {unit['city']} eliminato "
            f"con il subadmin {unit['subadmin_username']}."
        )
    })
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def admin_toggle_user(user_id: int, request: Request):
    user = require_manager_user(request)

    if user_id == user["id"]:
        query = urlencode({"error": "Non puoi disattivare il tuo account."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    target = connection.execute(
        """
        SELECT id, username, role, active, department, city
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    if target is None:
        connection.close()
        query = urlencode({"error": "Account non trovato."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if is_subadmin(user):
        if (
            target["role"] != ROLE_USER
            or target["department"] != user["department"]
            or target["city"] != user["city"]
        ):
            connection.close()
            query = urlencode({
                "error": "Il subadmin può gestire solo utenti del proprio dipartimento e città."
            })
            return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if is_admin(user) and target["role"] == ROLE_ADMIN:
        connection.close()
        query = urlencode({"error": "Non puoi modificare un altro admin da qui."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    new_active = 0 if target["active"] else 1

    connection.execute(
        "UPDATE users SET active = ? WHERE id = ?",
        (new_active, user_id)
    )

    if new_active == 0:
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    connection.commit()
    connection.close()

    action = "attivato" if new_active else "disattivato"
    query = urlencode({"success": f"Account {target['username']} {action}."})
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


def change_current_user_password(
    request: Request,
    user,
    current_password: str,
    new_password: str,
    confirm_password: str,
    redirect_path: str
):
    if len(new_password) < 6:
        query = urlencode({"error": "La nuova password deve avere almeno 6 caratteri."})
        return RedirectResponse(url=f"{redirect_path}?{query}", status_code=303)

    if new_password != confirm_password:
        query = urlencode({"error": "Le nuove password non coincidono."})
        return RedirectResponse(url=f"{redirect_path}?{query}", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    current_user = connection.execute(
        """
        SELECT id, password_hash
        FROM users
        WHERE id = ?
        """,
        (user["id"],)
    ).fetchone()

    if current_user is None or not verify_password(
        current_password,
        current_user["password_hash"]
    ):
        connection.close()
        query = urlencode({"error": "Password attuale non corretta."})
        return RedirectResponse(url=f"{redirect_path}?{query}", status_code=303)

    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user["id"])
    )

    current_token = request.cookies.get(SESSION_COOKIE_NAME)

    if current_token:
        connection.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user["id"], current_token)
        )

    connection.commit()
    connection.close()

    query = urlencode({"success": "Password aggiornata."})
    return RedirectResponse(url=f"{redirect_path}?{query}", status_code=303)


@app.post("/admin/users/current/password")
async def admin_change_current_password(request: Request):
    user = require_manager_user(request)
    form = await read_form_data(request)

    return change_current_user_password(
        request=request,
        user=user,
        current_password=form.get("current_password", ""),
        new_password=form.get("new_password", ""),
        confirm_password=form.get("confirm_password", ""),
        redirect_path="/admin/users"
    )


@app.post("/admin/users/{user_id}/password")
async def admin_reset_user_password(user_id: int, request: Request):
    user = require_admin_user(request)

    if user_id == user["id"]:
        query = urlencode({
            "error": "Non puoi reimpostare la password dell'admin corrente da qui."
        })
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    form = await read_form_data(request)
    password = form.get("password", "")

    if len(password) < 6:
        query = urlencode({"error": "La nuova password deve avere almeno 6 caratteri."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    target = connection.execute(
        """
        SELECT id, username, role, department, city
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    if target is None:
        connection.close()
        query = urlencode({"error": "Account non trovato."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if target["role"] == ROLE_ADMIN:
        connection.close()
        query = urlencode({"error": "Non puoi reimpostare la password di un admin."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(password), user_id)
    )
    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    connection.commit()
    connection.close()

    query = urlencode({"success": f"Password aggiornata per {target['username']}."})
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request):
    user = require_admin_user(request)

    if user_id == user["id"]:
        query = urlencode({"error": "Non puoi eliminare il tuo account."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    target = connection.execute(
        """
        SELECT id, username, role, department, city
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    if target is None:
        connection.close()
        query = urlencode({"error": "Account non trovato."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if target["role"] == ROLE_ADMIN:
        connection.close()
        query = urlencode({"error": "Non puoi eliminare un admin da qui."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    if target["role"] == ROLE_SUBADMIN:
        user_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role = ?
              AND department = ?
              AND city = ?
            """,
            (ROLE_USER, target["department"], target["city"])
        ).fetchone()[0]

        if user_count > 0:
            connection.close()
            query = urlencode({
                "error": (
                    "Non puoi eliminare il subadmin finché il suo dipartimento "
                    "contiene utenti."
                )
            })
            return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    connection.execute("UPDATE missions SET user_id = NULL WHERE user_id = ?", (user_id,))
    connection.execute(
        """
        UPDATE registration_requests
        SET reviewed_by = NULL
        WHERE reviewed_by = ?
        """,
        (user_id,)
    )

    if target["role"] == ROLE_SUBADMIN:
        connection.execute(
            "DELETE FROM admin_units WHERE subadmin_user_id = ?",
            (user_id,)
        )
        connection.execute(
            """
            UPDATE registration_requests
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?,
                decision_note = ?
            WHERE status = ?
              AND department = ?
              AND city = ?
            """,
            (
                "rejected",
                datetime.now().isoformat(timespec="seconds"),
                user["id"],
                "Subadmin eliminato dall'admin.",
                "pending",
                target["department"],
                target["city"]
            )
        )

    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()

    query = urlencode({"success": f"Account {target['username']} eliminato."})
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


def get_pending_registration_for_manager(connection, request_id, user):
    request_row = connection.execute(
        """
        SELECT id, username, password_hash, department, city, status
        FROM registration_requests
        WHERE id = ?
          AND status = ?
        """,
        (request_id, "pending")
    ).fetchone()

    if request_row is None:
        return None

    if is_admin(user):
        return None

    if (
        request_row["department"] == user["department"]
        and request_row["city"] == user["city"]
    ):
        return request_row

    return None


@app.post("/admin/registration-requests/{request_id}/accept")
def accept_registration_request(request_id: int, request: Request):
    user = require_manager_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    request_row = get_pending_registration_for_manager(
        connection,
        request_id,
        user
    )

    if request_row is None:
        connection.close()
        query = urlencode({"error": "Richiesta non trovata o non autorizzata."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    existing_user = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (request_row["username"],)
    ).fetchone()

    if existing_user is not None:
        connection.execute(
            """
            UPDATE registration_requests
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?,
                decision_note = ?
            WHERE id = ?
            """,
            (
                "rejected",
                datetime.now().isoformat(timespec="seconds"),
                user["id"],
                "Username già esistente al momento della revisione.",
                request_id
            )
        )
        connection.commit()
        connection.close()
        query = urlencode({"error": "Username già esistente: richiesta rifiutata."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    try:
        new_user_id = get_next_reusable_user_id(connection)
        cursor = connection.execute(
            """
            INSERT INTO users (
                id,
                username,
                password_hash,
                role,
                active,
                created_at,
                department,
                city
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_user_id,
                request_row["username"],
                request_row["password_hash"],
                ROLE_USER,
                1,
                datetime.now().isoformat(timespec="seconds"),
                request_row["department"],
                request_row["city"]
            )
        )
        connection.execute(
            """
            UPDATE registration_requests
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?,
                decision_note = ?
            WHERE id = ?
            """,
            (
                "accepted",
                datetime.now().isoformat(timespec="seconds"),
                user["id"],
                f"Creato utente #{new_user_id}.",
                request_id
            )
        )
        connection.execute(
            """
            UPDATE registration_requests
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?,
                decision_note = ?
            WHERE id != ?
              AND username = ?
              AND status = ?
            """,
            (
                "rejected",
                datetime.now().isoformat(timespec="seconds"),
                user["id"],
                "Username già approvato da un'altra richiesta.",
                request_id,
                request_row["username"],
                "pending"
            )
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        connection.close()
        query = urlencode({"error": "Impossibile creare l'utente richiesto."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.close()
    query = urlencode({
        "success": f"Richiesta di {request_row['username']} accettata."
    })
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


@app.post("/admin/registration-requests/{request_id}/reject")
def reject_registration_request(request_id: int, request: Request):
    user = require_manager_user(request)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    request_row = get_pending_registration_for_manager(
        connection,
        request_id,
        user
    )

    if request_row is None:
        connection.close()
        query = urlencode({"error": "Richiesta non trovata o non autorizzata."})
        return RedirectResponse(url=f"/admin/users?{query}", status_code=303)

    connection.execute(
        """
        UPDATE registration_requests
        SET status = ?,
            reviewed_at = ?,
            reviewed_by = ?,
            decision_note = ?
        WHERE id = ?
        """,
        (
            "rejected",
            datetime.now().isoformat(timespec="seconds"),
            user["id"],
            "Richiesta rifiutata.",
            request_id
        )
    )
    connection.commit()
    connection.close()

    query = urlencode({
        "success": f"Richiesta di {request_row['username']} rifiutata."
    })
    return RedirectResponse(url=f"/admin/users?{query}", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "current_user": user,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success")
        }
    )


@app.post("/account/password")
async def change_own_password(request: Request):
    user = require_api_user(request)
    form = await read_form_data(request)

    return change_current_user_password(
        request=request,
        user=user,
        current_password=form.get("current_password", ""),
        new_password=form.get("new_password", ""),
        confirm_password=form.get("confirm_password", ""),
        redirect_path="/account"
    )


@app.get("/admin/backup/database")
def download_database_backup(request: Request):
    require_admin_user(request)
    raise HTTPException(
        status_code=403,
        detail="Il superadmin gestisce solo account e dipartimenti."
    )


@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request):
    user = get_current_user(request)

    if user is None:
        return redirect_to_login(request)

    if is_admin(user):
        return RedirectResponse(url="/admin/users", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="messages.html",
        context={"current_user": user}
    )
