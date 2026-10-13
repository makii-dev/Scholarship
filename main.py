import os
import sys
import shutil
import sqlite3
import hashlib
import subprocess
from datetime import datetime

import flet as ft


# ===== SECTION 1: SETTINGS =====

# The folder of this file. Other paths are built from it, so the app runs from any launch folder.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

# The two rules of the consensus system (anti-bias logic):
REVIEWS_NEEDED = 3
APPROVAL_THRESHOLD = 3.5
MAX_BUDGET = 1_000_000_000_000

EDUCATION_LEVELS = ["High School", "Senior High", "College"]

# Only the hash (a scrambled fingerprint) of the secret committee code is stored here,
# so reading this file does not reveal the code.
COMMITTEE_CODE_HASH = "e31708870dbfc94df5ab2641ab615a80f8625aff33f4c64210d5f5fcc2f6bd96"

# The Super Admin has no database row (so it cannot be deleted); only its password hash is kept.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = "16b97aa1d7dc5c090fd8614914850b589892c0ea72f6b52882a2138d0b81e279"

# ---- Colors used by the design (hex codes) ----
COLOR_PRIMARY = "#4F46E5"
COLOR_PRIMARY_DARK = "#3730A3"
COLOR_ACCENT = "#7C3AED"
COLOR_PAGE_BG = "#F3F4FB"
COLOR_CARD = "#FFFFFF"
COLOR_TEXT = "#1F2937"
COLOR_MUTED = "#6B7280"
COLOR_SOFT_PRIMARY = "#EEF2FF"
COLOR_SELECTED = "#C7D2FE"
COLOR_LINE = "#E5E7EB"
COLOR_SUCCESS = "#16A34A"
COLOR_SUCCESS_SOFT = "#DCFCE7"
COLOR_WARNING = "#B45309"
COLOR_WARNING_SOFT = "#FEF3C7"
COLOR_INFO = "#2563EB"
COLOR_INFO_SOFT = "#DBEAFE"
COLOR_DANGER = "#DC2626"
COLOR_DANGER_SOFT = "#FEE2E2"

# For each status: (text color, background color) of its little badge.
STATUS_STYLES = {
    "Pending": (COLOR_WARNING, COLOR_WARNING_SOFT),
    "Needs Revision": (COLOR_INFO, COLOR_INFO_SOFT),
    "Approved": (COLOR_SUCCESS, COLOR_SUCCESS_SOFT),
    "Rejected": (COLOR_DANGER, COLOR_DANGER_SOFT),
}


# ===== SECTION 2: DATABASE FUNCTIONS =====

def get_connection():
    """Open the SQLite database (waits up to 20 s if busy, WAL mode on, foreign keys enforced)."""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def hash_password(password):
    """
    Scramble a text with SHA-256 so passwords and codes are never stored as typed.
    (A real system would also add salting.)
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_uploads_folder():
    """Create the 'uploads' folder if it does not exist yet."""
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def wipe_old_database_if_needed(conn):
    """
    If the database has the old version-1 layout, delete its tables and uploaded files so everyone signs up fresh.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(Users)")
    column_names = [row[1] for row in cursor.fetchall()]

    if len(column_names) == 0 or "first_name" in column_names:
        return

    # Drop the tables that depend on others first, and Users after them.
    for table_name in ("Reviews", "Documents", "Applications", "Users", "Master_Budget"):
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.commit()

    if os.path.isdir(UPLOADS_DIR):
        for folder_name in os.listdir(UPLOADS_DIR):
            folder_path = os.path.join(UPLOADS_DIR, folder_name)
            if os.path.isdir(folder_path):
                shutil.rmtree(folder_path)


def init_database():
    """
    Create the 5 tables and the starting budget row if missing. Safe to call on every start; creates no users.
    """
    conn = get_connection()
    wipe_old_database_if_needed(conn)
    cursor = conn.cursor()

    # COLLATE NOCASE means "Maria" and "maria" count as the same username.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Users (
            user_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password        TEXT NOT NULL,
            role            TEXT NOT NULL CHECK (role IN ('student', 'committee')),
            full_name       TEXT NOT NULL,
            first_name      TEXT NOT NULL,
            middle_name     TEXT,
            last_name       TEXT NOT NULL,
            education_level TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Applications (
            app_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id       INTEGER NOT NULL,
            status           TEXT NOT NULL DEFAULT 'Pending'
                             CHECK (status IN ('Pending', 'Needs Revision', 'Approved', 'Rejected')),
            submission_date  TEXT NOT NULL,
            requested_amount REAL NOT NULL,
            awarded_amount   REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (student_id) REFERENCES Users (user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Documents (
            doc_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id    INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            doc_type  TEXT,
            FOREIGN KEY (app_id) REFERENCES Applications (app_id)
        )
    """)

    # UNIQUE (app_id, reviewer_id): one member can review an application only once,
    # so the 3 reviews come from 3 different people.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Reviews (
            review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id      INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            score       INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
            notes       TEXT,
            UNIQUE (app_id, reviewer_id),
            FOREIGN KEY (app_id) REFERENCES Applications (app_id),
            FOREIGN KEY (reviewer_id) REFERENCES Users (user_id)
        )
    """)

    # Master_Budget only ever has one row.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Master_Budget (
            budget_id  INTEGER PRIMARY KEY,
            total_pool REAL NOT NULL DEFAULT 1000000.0,
            allocated  REAL NOT NULL DEFAULT 0.0
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM Master_Budget")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO Master_Budget (budget_id, total_pool, allocated) VALUES (1, 1000000.0, 0.0)"
        )

    conn.commit()
    conn.close()


def clean_text(text):
    """Remove spaces at the start/end and squeeze repeated spaces into one."""
    if text is None:
        return ""
    return " ".join(str(text).split())


def build_full_name(first_name, middle_name, last_name):
    """Join the name parts, skipping the middle name if it is empty."""
    parts = [first_name, middle_name, last_name]
    return " ".join(part for part in parts if part)


def create_account(role, first_name, middle_name, last_name, username, password,
                   education_level=None, committee_code=None):
    """
    Create a student or committee account. Every rule is enforced here: names, username (3-20 characters),
    password (6+ characters), education level, and the secret code for committee members.
    Returns (True/False, message).
    """
    first_name = clean_text(first_name)
    middle_name = clean_text(middle_name)
    last_name = clean_text(last_name)
    username = (username or "").strip()
    password = password or ""

    if first_name == "" or last_name == "":
        return False, "Please enter your first name and last name."
    if len(first_name) > 50 or len(middle_name) > 50 or len(last_name) > 50:
        return False, "Names can have at most 50 characters."
    if len(username) < 3 or len(username) > 20:
        return False, "Your username must have 3 to 20 characters."
    for letter in username:
        if not (letter.isalnum() or letter in "._"):
            return False, "Your username can only use letters, numbers, dots and underscores."
    if username.lower() == ADMIN_USERNAME:
        # This name belongs to the Super Admin. We give the same message as for any taken name.
        return False, "That username is already taken. Please choose another one."
    if len(password) < 6:
        return False, "Your password must have at least 6 characters."

    if role == "student":
        if education_level not in EDUCATION_LEVELS:
            return False, "Please choose your education level."
    elif role == "committee":
        education_level = None
        code = (committee_code or "").strip()
        if code == "":
            return False, "The committee access code is required. Sign-up denied."
        if hash_password(code) != COMMITTEE_CODE_HASH:
            return False, "Invalid committee access code. Sign-up denied."
    else:
        return False, "Unknown account type."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM Users WHERE username = ?", (username,))
        if cursor.fetchone()[0] > 0:
            return False, "That username is already taken. Please choose another one."

        cursor.execute(
            "INSERT INTO Users (username, password, role, full_name, first_name, "
            "middle_name, last_name, education_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                username,
                hash_password(password),
                role,
                build_full_name(first_name, middle_name, last_name),
                first_name,
                middle_name if middle_name else None,   # empty middle name -> NULL
                last_name,
                education_level,
            ),
        )
        conn.commit()
        return True, "Account created! You can now sign in."

    except sqlite3.IntegrityError:
        conn.rollback()
        return False, "That username is already taken. Please choose another one."
    except Exception as error:
        conn.rollback()
        return False, f"Could not create the account: {error}"
    finally:
        conn.close()


def check_login(username, password):
    """
    Return the user's info as a dictionary if the username and password match, otherwise None.
    The 'role' in it decides which dashboard opens.
    """
    # The Super Admin is checked first. It has no row in the Users table.
    if username.strip().lower() == ADMIN_USERNAME and hash_password(password) == ADMIN_PASSWORD_HASH:
        return {
            "user_id": 0,
            "username": ADMIN_USERNAME,
            "role": "admin",
            "full_name": "Super Admin",
            "first_name": "Admin",
            "education_level": None,
        }

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, role, full_name, first_name, education_level "
        "FROM Users WHERE username = ? AND password = ?",
        (username, hash_password(password)),
    )
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None
    return {
        "user_id": row[0],
        "username": row[1],
        "role": row[2],
        "full_name": row[3],
        "first_name": row[4],
        "education_level": row[5],
    }


def get_budget():
    """Return (total_pool, allocated) from the Master_Budget table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT total_pool, allocated FROM Master_Budget WHERE budget_id = 1")
    row = cursor.fetchone()
    conn.close()
    return row[0], row[1]


def submit_application(student_id, requested_amount, source_path):
    """
    Save a new application: insert the row, copy the file into uploads/{student_id}/ and save its path.
    A student may have only one pending application. Returns (True/False, message).
    """
    if not os.path.isfile(source_path):
        return False, "The chosen file could not be found. Please choose it again."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM Applications WHERE student_id = ? AND status = 'Pending'",
            (student_id,),
        )
        if cursor.fetchone()[0] > 0:
            return False, "You already have a pending application. Please wait for the decision."

        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        cursor.execute(
            "INSERT INTO Applications (student_id, status, submission_date, requested_amount, awarded_amount) "
            "VALUES (?, 'Pending', ?, ?, 0)",
            (student_id, today, requested_amount),
        )
        new_app_id = cursor.lastrowid  # the id SQLite just gave to this application

        # The application id goes in front of the file name so files never overwrite each other.
        student_folder = os.path.join(UPLOADS_DIR, str(student_id))
        os.makedirs(student_folder, exist_ok=True)
        new_file_name = f"{new_app_id}_{os.path.basename(source_path)}"
        destination = os.path.join(student_folder, new_file_name)
        shutil.copy(source_path, destination)

        cursor.execute(
            "INSERT INTO Documents (app_id, file_path, doc_type) VALUES (?, ?, ?)",
            (new_app_id, destination, "Tax/Income Document"),
        )

        conn.commit()
        return True, "Application submitted! Its status is now 'Pending'."

    except Exception as error:
        conn.rollback()  # undo the half-finished work if anything went wrong
        return False, f"Could not submit the application: {error}"
    finally:
        conn.close()


def get_student_applications(student_id):
    """
    Return all applications of one student, newest first.
    Each row is: (app_id, status, submission_date, requested_amount, awarded_amount)
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT app_id, status, submission_date, requested_amount, awarded_amount "
        "FROM Applications WHERE student_id = ? ORDER BY app_id DESC",
        (student_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_pending_applications():
    """
    Return pending applications, oldest first: (app_id, requested_amount, submission_date, review_count).
    Student names are left out on purpose (blind review).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.app_id,
               a.requested_amount,
               a.submission_date,
               (SELECT COUNT(*) FROM Reviews r WHERE r.app_id = a.app_id)
        FROM Applications a
        WHERE a.status = 'Pending'
        ORDER BY a.app_id ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_application_details(app_id):
    """Return (app_id, requested_amount, submission_date, status) or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT app_id, requested_amount, submission_date, status FROM Applications WHERE app_id = ?",
        (app_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def get_document_path(app_id):
    """Return the saved file path of the application's document, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT file_path FROM Documents WHERE app_id = ? ORDER BY doc_id LIMIT 1",
        (app_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return row[0]


def get_reviews_for_app(app_id):
    """
    Return all reviews of one application, in the order they were written.
    Each row is: (reviewer_id, score, notes)
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT reviewer_id, score, notes FROM Reviews WHERE app_id = ? ORDER BY review_id",
        (app_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def calculate_average(reviews):
    """Take a list of reviews (reviewer_id, score, notes) and return the average score."""
    if len(reviews) == 0:
        return 0.0
    total = 0
    for review in reviews:
        total = total + review[1]   # review[1] is the score
    return total / len(reviews)


def submit_review(app_id, reviewer_id, score, notes):
    """
    Save one committee member's review (score 1-5 plus notes).
    Returns (True/False, message to show on screen).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # BEGIN IMMEDIATE locks the database for writing, so two members
        # cannot add a 4th review at the same moment.
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("SELECT status FROM Applications WHERE app_id = ?", (app_id,))
        row = cursor.fetchone()
        if row is None or row[0] != "Pending":
            return False, "This application is no longer open for review."

        cursor.execute("SELECT COUNT(*) FROM Reviews WHERE app_id = ?", (app_id,))
        if cursor.fetchone()[0] >= REVIEWS_NEEDED:
            return False, f"This application already has all {REVIEWS_NEEDED} reviews."

        cursor.execute(
            "SELECT COUNT(*) FROM Reviews WHERE app_id = ? AND reviewer_id = ?",
            (app_id, reviewer_id),
        )
        if cursor.fetchone()[0] > 0:
            return False, "You have already reviewed this application."

        cursor.execute(
            "INSERT INTO Reviews (app_id, reviewer_id, score, notes) VALUES (?, ?, ?, ?)",
            (app_id, reviewer_id, score, notes),
        )
        conn.commit()
        return True, "Review submitted. Thank you!"

    except Exception as error:
        conn.rollback()
        return False, f"Could not save the review: {error}"
    finally:
        conn.close()


def decide_application(app_id, decision):
    """
    Final decision: 'Approved', 'Rejected' or 'Needs Revision'. Needs exactly 3 reviews.
    Approval needs an average of 3.5+ and enough budget (the amount is added to Master_Budget.allocated);
    the other two are only allowed below 3.5. Returns (True/False, message).
    """
    if decision not in ("Approved", "Rejected", "Needs Revision"):
        return False, "Unknown decision."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT status, requested_amount FROM Applications WHERE app_id = ?", (app_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return False, "Application not found."
        status, requested_amount = row
        if status != "Pending":
            return False, "A decision was already made for this application."

        cursor.execute(
            "SELECT reviewer_id, score, notes FROM Reviews WHERE app_id = ?", (app_id,)
        )
        reviews = cursor.fetchall()
        if len(reviews) != REVIEWS_NEEDED:
            return False, f"A decision needs exactly {REVIEWS_NEEDED} reviews."

        average = calculate_average(reviews)

        if decision == "Approved":
            if average < APPROVAL_THRESHOLD:
                return False, f"The average score ({average:.2f}) is below {APPROVAL_THRESHOLD}."

            cursor.execute("SELECT total_pool, allocated FROM Master_Budget WHERE budget_id = 1")
            total_pool, allocated = cursor.fetchone()
            if requested_amount > total_pool - allocated:
                return False, "Not enough funds left in the budget to approve this request."

            cursor.execute(
                "UPDATE Applications SET status = 'Approved', awarded_amount = ? WHERE app_id = ?",
                (requested_amount, app_id),
            )
            cursor.execute(
                "UPDATE Master_Budget SET allocated = allocated + ? WHERE budget_id = 1",
                (requested_amount,),
            )
            message = f"Application #{app_id} approved. {format_peso(requested_amount)} was allocated."
        else:
            if average >= APPROVAL_THRESHOLD:
                return False, "This application reached the approval score, so it cannot be rejected."
            cursor.execute(
                "UPDATE Applications SET status = ? WHERE app_id = ?", (decision, app_id)
            )
            message = f"Application #{app_id} was marked as '{decision}'."

        conn.commit()
        return True, message

    except Exception as error:
        conn.rollback()
        return False, f"Could not save the decision: {error}"
    finally:
        conn.close()


def get_users_by_role(role):
    """
    Return (user_id, username, full_name, role, education_level) for every user of a role, sorted by name.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, full_name, role, education_level FROM Users "
        "WHERE role = ? ORDER BY full_name COLLATE NOCASE",
        (role,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_user(user_id, requester):
    """
    Super Admin only. Delete a user and everything that belongs to them.
    Student: applications, documents (rows and files) and reviews on them; approved money returns to the budget.
    Committee member: the reviews they wrote. Returns (True/False, message).
    """
    if requester is None or requester.get("role") != "admin":
        return False, "Only the super admin can delete users."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("SELECT role, full_name FROM Users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            return False, "That user no longer exists."
        role, full_name = row

        if role == "student":
            cursor.execute(
                "SELECT app_id, status, awarded_amount FROM Applications WHERE student_id = ?",
                (user_id,),
            )
            for app_id, status, awarded_amount in cursor.fetchall():
                if status == "Approved":
                    # Give the money back (MAX(0, ...) makes sure it never goes below zero).
                    cursor.execute(
                        "UPDATE Master_Budget SET allocated = MAX(0, allocated - ?) WHERE budget_id = 1",
                        (awarded_amount,),
                    )
                cursor.execute("DELETE FROM Reviews WHERE app_id = ?", (app_id,))
                cursor.execute("DELETE FROM Documents WHERE app_id = ?", (app_id,))
                cursor.execute("DELETE FROM Applications WHERE app_id = ?", (app_id,))
        else:
            cursor.execute("DELETE FROM Reviews WHERE reviewer_id = ?", (user_id,))

        cursor.execute("DELETE FROM Users WHERE user_id = ?", (user_id,))
        conn.commit()

    except Exception as error:
        conn.rollback()
        return False, f"Could not delete the user: {error}"
    finally:
        conn.close()

    # The database part succeeded, so now remove the student's uploaded files.
    if role == "student":
        shutil.rmtree(os.path.join(UPLOADS_DIR, str(user_id)), ignore_errors=True)

    return True, f"{full_name} was deleted."


def set_available_funds(new_available, requester):
    """
    Super Admin only. Set the available funds: total_pool becomes allocated + new_available.
    Returns (True/False, message).
    """
    if requester is None or requester.get("role") != "admin":
        return False, "Only the super admin can change the budget."

    try:
        new_available = round(float(new_available), 2)
    except (TypeError, ValueError):
        return False, "Please enter a valid amount."
    # (This one comparison also rejects 'nan', 'inf' and negative numbers.)
    if not (0 <= new_available <= MAX_BUDGET):
        return False, f"Please enter an amount between 0 and {format_peso(MAX_BUDGET)}."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT allocated FROM Master_Budget WHERE budget_id = 1")
        allocated = cursor.fetchone()[0]

        new_total = round(allocated + new_available, 2)
        if new_total <= 0:
            return False, "The total pool cannot be zero. Please enter an amount above 0."

        cursor.execute("UPDATE Master_Budget SET total_pool = ? WHERE budget_id = 1", (new_total,))
        conn.commit()
        return True, (f"Available funds set to {format_peso(new_available)}. "
                      f"The total pool is now {format_peso(new_total)}.")

    except Exception as error:
        conn.rollback()
        return False, f"Could not change the budget: {error}"
    finally:
        conn.close()


# ===== SECTION 3: SMALL HELPER FUNCTIONS =====

def format_peso(amount):
    """Turn a number into text like '₱850,000' (or '₱1,250.50' if it has cents)."""
    if amount == int(amount):
        return f"₱{int(amount):,}"
    return f"₱{amount:,.2f}"


def open_document(file_path):
    """Open a file in the computer's default program. Returns (True/False, error message)."""
    if not os.path.exists(file_path):
        return False, "The document file could not be found on this computer."
    try:
        if sys.platform.startswith("win"):
            os.startfile(file_path)                  # Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", file_path])    # macOS
        else:
            subprocess.Popen(["xdg-open", file_path])  # Linux
        return True, ""
    except Exception as error:
        return False, f"Could not open the document: {error}"


# ===== SECTION 4: DESIGN BUILDING BLOCKS =====

def pad(horizontal=0, vertical=0):
    """Padding with the same space left/right and the same space top/bottom."""
    return ft.Padding(left=horizontal, top=vertical, right=horizontal, bottom=vertical)


def brand_gradient():
    """The indigo -> violet gradient used on headers and the sign-in background."""
    return ft.LinearGradient(
        begin=ft.Alignment.TOP_LEFT,
        end=ft.Alignment.BOTTOM_RIGHT,
        colors=[COLOR_PRIMARY_DARK, COLOR_PRIMARY, COLOR_ACCENT],
    )


def make_title(text, size=20, color=COLOR_TEXT, **options):
    """A bold heading text."""
    return ft.Text(text, size=size, weight=ft.FontWeight.BOLD, color=color, **options)


def make_muted(text, size=13, **options):
    """A small grey text for descriptions and labels."""
    return ft.Text(text, size=size, color=COLOR_MUTED, **options)


def make_card(content, padding=20, **options):
    """A white rounded box with a soft shadow. Everything sits inside cards."""
    return ft.Container(
        content=content,
        padding=padding,
        bgcolor=COLOR_CARD,
        border_radius=20,
        shadow=ft.BoxShadow(blur_radius=18, color="#D1D5DB", offset=ft.Offset(0, 4)),
        **options,
    )


def make_icon_badge(icon, icon_color, background, size=44):
    """An icon inside a colored circle."""
    return ft.Container(
        content=ft.Icon(icon, color=icon_color, size=size // 2),
        width=size,
        height=size,
        border_radius=size // 2,
        bgcolor=background,
        alignment=ft.Alignment.CENTER,
    )


def make_pill(text, text_color, background):
    """A small rounded label, used for statuses and review counters."""
    return ft.Container(
        content=ft.Text(text, size=12, weight=ft.FontWeight.BOLD, color=text_color),
        bgcolor=background,
        border_radius=20,
        padding=pad(12, 5),
    )


def make_fact(label, value, value_color=COLOR_TEXT):
    """A small label with a bigger value under it, e.g. 'Requested' / '₱50,000'."""
    return ft.Column(
        [make_muted(label, 12), make_title(value, 16, value_color)],
        spacing=2,
    )


def make_text_field(label, icon, is_password=False, **options):
    """A rounded input box with an icon. Extra settings (like on_submit) go in **options."""
    return ft.TextField(
        label=label,
        prefix_icon=icon,
        password=is_password,
        can_reveal_password=is_password,   # the little "eye" button
        border_radius=12,
        focused_border_color=COLOR_PRIMARY,
        **options,
    )


def make_primary_button(text, on_click, icon=None, color=COLOR_PRIMARY):
    """A big filled button (the main action on a screen)."""
    return ft.Button(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor=color,
            color="#FFFFFF",
            shape=ft.RoundedRectangleBorder(radius=12),
            padding=18,
        ),
    )


def make_outline_button(text, on_click, icon=None, color=COLOR_PRIMARY):
    """A button with only an outline (a secondary action)."""
    return ft.OutlinedButton(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            color=color,
            shape=ft.RoundedRectangleBorder(radius=12),
            padding=18,
        ),
    )


def make_link_button(text, on_click, icon=None, color=COLOR_PRIMARY):
    """A flat text-only button, used for 'Back' and small links."""
    return ft.TextButton(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(color=color),
    )


class Notice:
    """
    A hidden message bar. show() makes it green (ok) or red (error); hide() removes it. Call page.update() afterwards.
    """

    def __init__(self):
        self.box = ft.Container(visible=False, padding=pad(14, 12), border_radius=12)

    def show(self, message, is_ok):
        if is_ok:
            text_color, background, icon = COLOR_SUCCESS, COLOR_SUCCESS_SOFT, ft.Icons.CHECK_CIRCLE
        else:
            text_color, background, icon = COLOR_DANGER, COLOR_DANGER_SOFT, ft.Icons.ERROR
        self.box.bgcolor = background
        self.box.content = ft.Row(
            [
                ft.Icon(icon, color=text_color, size=20),
                ft.Text(message, color=text_color, size=13, weight=ft.FontWeight.W_600, expand=True),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.box.visible = True

    def hide(self):
        self.box.visible = False


def make_header(title, subtitle, avatar_letter, on_logout):
    """The gradient banner at the top of both dashboards (avatar, name, logout)."""
    avatar = ft.Container(
        content=ft.Text(avatar_letter, size=20, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        width=52,
        height=52,
        border_radius=26,
        bgcolor="#6366F1",
        alignment=ft.Alignment.CENTER,
    )
    return ft.Container(
        padding=20,
        border_radius=24,
        gradient=brand_gradient(),
        content=ft.Row(
            [
                ft.Row(
                    [
                        avatar,
                        ft.Column(
                            [
                                ft.Text(title, size=20, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                                ft.Text(subtitle, size=13, color="#E0E7FF"),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    spacing=14,
                    expand=True,
                ),
                ft.TextButton(
                    "Logout",
                    icon=ft.Icons.LOGOUT,
                    on_click=on_logout,
                    style=ft.ButtonStyle(color="#FFFFFF"),
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def show_auth_card(page, icon, title, subtitle, controls):
    """
    Draw an entrance screen: a white card on a gradient background.
    A ResponsiveRow splits the width into 12 columns (xs = phone ... xl = large monitor), so the card is
    full width on a phone and narrow and centered on a computer.
    """
    page.clean()

    logo = ft.Container(
        content=ft.Icon(icon, color="#FFFFFF", size=32),
        width=64,
        height=64,
        border_radius=32,
        gradient=brand_gradient(),
        alignment=ft.Alignment.CENTER,
    )

    card = make_card(
        ft.Column(
            [
                ft.Row([logo], alignment=ft.MainAxisAlignment.CENTER),
                make_title(title, 24, text_align=ft.TextAlign.CENTER),
                make_muted(subtitle, 14, text_align=ft.TextAlign.CENTER),
                ft.Container(height=2),
                *controls,
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        padding=28,
    )

    page.add(
        ft.Container(
            expand=True,
            gradient=brand_gradient(),
            content=ft.Column(
                [
                    ft.Container(height=20),
                    ft.ResponsiveRow(
                        [
                            ft.Container(
                                content=card,
                                padding=pad(12, 0),
                                col={"xs": 12, "sm": 9, "md": 7, "lg": 5, "xl": 4},
                            )
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Container(height=20),
                ],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
    )


def show_dashboard_page(page, controls):
    """
    Draw a dashboard: a scrolling column on a light background, centered and width-limited on big screens.
    """
    page.clean()
    page.add(
        ft.Container(
            expand=True,
            bgcolor=COLOR_PAGE_BG,
            content=ft.Column(
                [
                    ft.ResponsiveRow(
                        [
                            ft.Container(
                                padding=16,
                                col={"xs": 12, "xl": 11, "xxl": 9},
                                content=ft.Column(
                                    controls,
                                    spacing=16,
                                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                                ),
                            )
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                    )
                ],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
    )


# ===== SECTION 5: SIGN IN / SIGN UP SCREENS =====

def make_feature_row(icon, title, description):
    """One line of the 'why use this portal' list on the welcome screen."""
    return ft.Row(
        [
            make_icon_badge(icon, COLOR_PRIMARY, COLOR_SOFT_PRIMARY, 40),
            ft.Column(
                [make_title(title, 14), make_muted(description, 12)],
                spacing=1,
                expand=True,
            ),
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def show_welcome_screen(page, file_picker):
    """The very first screen: two big options, Sign In and Sign Up."""

    def go_to_sign_in(e):
        show_sign_in_screen(page, file_picker)

    def go_to_sign_up(e):
        show_sign_up_choice_screen(page, file_picker)

    show_auth_card(
        page,
        ft.Icons.SCHOOL,
        "Scholarship & Financial Aid Portal",
        "Apply for financial aid, or review applications as a committee member.",
        [
            make_primary_button("Sign In", go_to_sign_in, ft.Icons.LOGIN),
            make_outline_button("Sign Up", go_to_sign_up, ft.Icons.PERSON_ADD),
            ft.Divider(height=1, color=COLOR_LINE),
            make_feature_row(ft.Icons.UPLOAD_FILE, "Easy applications",
                             "Request funds and upload your income document."),
            make_feature_row(ft.Icons.GROUPS, "Fair, blind review",
                             "Three committee members score every application."),
            make_feature_row(ft.Icons.VERIFIED_USER, "Clear decisions",
                             "Track your status and read the committee's notes."),
        ],
    )


def show_sign_in_screen(page, file_picker, success_message=""):
    """
    Sign In screen. After a correct login, the role from the database decides which dashboard opens.
    """
    notice = Notice()
    if success_message != "":
        notice.show(success_message, True)

    username_field = make_text_field("Username", ft.Icons.PERSON, autofocus=True)
    password_field = make_text_field("Password", ft.Icons.LOCK, is_password=True)

    def handle_sign_in(e):
        """Runs when the user clicks 'Sign In' (or presses Enter in the password box)."""
        username = (username_field.value or "").strip()
        password = password_field.value or ""

        if username == "" or password == "":
            notice.show("Please enter your username and password.", False)
            page.update()
            return

        user = check_login(username, password)
        if user is None:
            notice.show("Wrong username or password.", False)
            page.update()
            return

        # The role comes from the database, so the user never has to choose it.
        if user["role"] == "student":
            show_student_dashboard(page, user, file_picker)
        elif user["role"] == "committee":
            show_committee_dashboard(page, user, file_picker)
        else:
            show_admin_dashboard(page, user, file_picker)

    def go_to_sign_up(e):
        show_sign_up_choice_screen(page, file_picker)

    def go_back(e):
        show_welcome_screen(page, file_picker)

    password_field.on_submit = handle_sign_in  # pressing Enter also signs in

    show_auth_card(
        page,
        ft.Icons.LOGIN,
        "Welcome back",
        "Sign in to continue.",
        [
            notice.box,
            username_field,
            password_field,
            make_primary_button("Sign In", handle_sign_in, ft.Icons.LOGIN),
            ft.Row(
                [make_muted("New here?"), make_link_button("Create an account", go_to_sign_up)],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=0,
                wrap=True,
            ),
            make_link_button("Back", go_back, ft.Icons.ARROW_BACK, COLOR_MUTED),
        ],
    )


def make_option_tile(icon, title, description, on_click):
    """A big clickable tile used on the 'Student or Committee?' screen."""
    return ft.Container(
        on_click=on_click,
        ink=True,
        padding=16,
        border_radius=16,
        bgcolor=COLOR_SOFT_PRIMARY,
        content=ft.Row(
            [
                make_icon_badge(icon, COLOR_PRIMARY, "#FFFFFF", 48),
                ft.Column(
                    [make_title(title, 17), make_muted(description, 13)],
                    spacing=2,
                    expand=True,
                ),
                ft.Icon(ft.Icons.CHEVRON_RIGHT, color=COLOR_MUTED),
            ],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def show_sign_up_choice_screen(page, file_picker):
    """Sign Up, step 1: choose between a Student account and a Committee account."""

    def choose_student(e):
        show_student_sign_up_screen(page, file_picker)

    def choose_committee(e):
        show_committee_sign_up_screen(page, file_picker)

    def go_to_sign_in(e):
        show_sign_in_screen(page, file_picker)

    def go_back(e):
        show_welcome_screen(page, file_picker)

    show_auth_card(
        page,
        ft.Icons.PERSON_ADD,
        "Create your account",
        "Who are you signing up as?",
        [
            make_option_tile(ft.Icons.SCHOOL, "Student",
                             "Apply for a scholarship", choose_student),
            make_option_tile(ft.Icons.GROUPS, "Committee Member",
                             "Review applications (access code needed)", choose_committee),
            ft.Row(
                [make_muted("Already have an account?"), make_link_button("Sign In", go_to_sign_in)],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=0,
                wrap=True,
            ),
            make_link_button("Back", go_back, ft.Icons.ARROW_BACK, COLOR_MUTED),
        ],
    )


def make_name_fields():
    """The three name boxes that both sign-up forms share."""
    first_field = make_text_field("First name", ft.Icons.PERSON, autofocus=True)
    middle_field = make_text_field("Middle name (optional)", ft.Icons.PERSON_OUTLINE)
    last_field = make_text_field("Last name", ft.Icons.PERSON)
    return first_field, middle_field, last_field


def show_student_sign_up_screen(page, file_picker):
    """Sign Up for students: names, username, education level and password."""
    notice = Notice()

    first_field, middle_field, last_field = make_name_fields()
    username_field = make_text_field("Username", ft.Icons.BADGE)
    education_dropdown = ft.Dropdown(
        label="Education level",
        leading_icon=ft.Icons.SCHOOL,
        options=[ft.DropdownOption(key=level) for level in EDUCATION_LEVELS],
        expand=True,   # inside a Row, 'expand' means "use all the free width"
    )
    password_field = make_text_field("Password", ft.Icons.LOCK, is_password=True)

    def handle_create(e):
        """Runs when the student clicks 'Create Student Account'."""
        success, message = create_account(
            role="student",
            first_name=first_field.value,
            middle_name=middle_field.value,
            last_name=last_field.value,
            username=username_field.value,
            password=password_field.value,
            education_level=education_dropdown.value,
        )
        if success:
            show_sign_in_screen(page, file_picker, success_message=message)
        else:
            notice.show(message, False)
            page.update()

    def go_back(e):
        show_sign_up_choice_screen(page, file_picker)

    show_auth_card(
        page,
        ft.Icons.SCHOOL,
        "Student Sign Up",
        "Tell us a little about yourself.",
        [
            notice.box,
            first_field,
            middle_field,
            last_field,
            username_field,
            ft.Row([education_dropdown]),
            password_field,
            make_primary_button("Create Student Account", handle_create, ft.Icons.PERSON_ADD),
            make_link_button("Back", go_back, ft.Icons.ARROW_BACK, COLOR_MUTED),
        ],
    )


def show_committee_sign_up_screen(page, file_picker):
    """
    Committee sign-up form. The code box hides what is typed and the screen gives no hint about the code.
    """
    notice = Notice()

    first_field, middle_field, last_field = make_name_fields()
    username_field = make_text_field("Username", ft.Icons.BADGE)
    code_field = make_text_field("Committee access code", ft.Icons.KEY, is_password=True)
    password_field = make_text_field("Password", ft.Icons.LOCK, is_password=True)

    def handle_create(e):
        """Runs when the committee member clicks 'Create Committee Account'."""
        success, message = create_account(
            role="committee",
            first_name=first_field.value,
            middle_name=middle_field.value,
            last_name=last_field.value,
            username=username_field.value,
            password=password_field.value,
            committee_code=code_field.value,
        )
        if success:
            show_sign_in_screen(page, file_picker, success_message=message)
        else:
            code_field.value = ""
            notice.show(message, False)
            page.update()

    def go_back(e):
        show_sign_up_choice_screen(page, file_picker)

    show_auth_card(
        page,
        ft.Icons.GROUPS,
        "Committee Sign Up",
        "Only authorized members can join the committee.",
        [
            notice.box,
            first_field,
            middle_field,
            last_field,
            username_field,
            code_field,
            password_field,
            make_primary_button("Create Committee Account", handle_create, ft.Icons.PERSON_ADD),
            make_link_button("Back", go_back, ft.Icons.ARROW_BACK, COLOR_MUTED),
        ],
    )


# ===== SECTION 6: STUDENT DASHBOARD =====

def build_application_card(application):
    """Build the card for one application: status, amounts and committee notes."""
    app_id, status, submission_date, requested_amount, awarded_amount = application
    reviews = get_reviews_for_app(app_id)
    status_color, status_background = STATUS_STYLES.get(status, (COLOR_MUTED, COLOR_PAGE_BG))

    lines = [
        ft.Row(
            [
                ft.Row(
                    [
                        make_icon_badge(ft.Icons.ASSIGNMENT, COLOR_PRIMARY, COLOR_SOFT_PRIMARY, 40),
                        make_title(f"Application #{app_id}", 17),
                    ],
                    spacing=12,
                ),
                make_pill(status, status_color, status_background),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            run_spacing=8,
        ),
    ]

    facts = [
        make_fact("Requested", format_peso(requested_amount)),
        make_fact("Submitted", submission_date),
    ]
    if status == "Approved":
        facts.append(make_fact("Awarded", format_peso(awarded_amount), COLOR_SUCCESS))
    lines.append(ft.Row(facts, spacing=28, wrap=True, run_spacing=10))

    if status == "Pending":
        # Notes are only shown after the committee has made its decision.
        lines.append(ft.ProgressBar(value=len(reviews) / REVIEWS_NEEDED,
                                    color=COLOR_PRIMARY, bgcolor=COLOR_LINE))
        lines.append(make_muted(f"Under review: {len(reviews)} of {REVIEWS_NEEDED} reviews completed."))
    else:
        lines.append(make_title("Committee notes", 14))
        found_any_notes = False
        for number, review in enumerate(reviews, start=1):
            notes = review[2]
            if notes:
                # Reviewers are shown as "Reviewer 1, 2, 3" so they stay anonymous.
                lines.append(
                    ft.Container(
                        padding=12,
                        border_radius=12,
                        bgcolor=COLOR_PAGE_BG,
                        content=ft.Column(
                            [make_muted(f"Reviewer {number}", 12), ft.Text(notes, size=14, color=COLOR_TEXT)],
                            spacing=2,
                        ),
                    )
                )
                found_any_notes = True
        if not found_any_notes:
            lines.append(make_muted("The committee did not leave any written notes."))

    return make_card(ft.Column(lines, spacing=12))


def show_student_dashboard(page, user, file_picker):
    """Draw the Student Dashboard: the application form and the status tracker."""

    # Remembers the picked file (a dict can be changed by the nested functions below).
    selected = {"path": None}

    notice = Notice()
    amount_field = make_text_field(
        "Requested amount (₱)", ft.Icons.PAYMENTS, keyboard_type=ft.KeyboardType.NUMBER
    )
    file_label = make_muted("No file chosen yet")
    tracker_column = ft.Column(spacing=14, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def refresh_tracker():
        """Rebuild the list of status cards from the database."""
        tracker_column.controls.clear()
        applications = get_student_applications(user["user_id"])
        if len(applications) == 0:
            tracker_column.controls.append(
                make_card(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.INBOX, size=44, color=COLOR_MUTED),
                            make_muted("You have not submitted an application yet.", 14,
                                       text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    padding=30,
                )
            )
        for application in applications:
            tracker_column.controls.append(build_application_card(application))

    async def handle_choose_file(e):
        """
        Open the file dialog and remember the chosen file. It is 'async' because Flet's FilePicker must be awaited.
        """
        files = await file_picker.pick_files(
            allow_multiple=False,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["pdf", "png", "jpg", "jpeg"],
        )
        if not files:
            return  # the student closed the dialog without choosing anything

        if files[0].path is None:
            # Happens only when Flet runs in a web browser (no real file path).
            notice.show("Cannot read the file path. Please run the app as a desktop app.", False)
        else:
            selected["path"] = files[0].path
            file_label.value = files[0].name
            file_label.color = COLOR_SUCCESS
            notice.hide()
        page.update()

    def handle_submit(e):
        """Runs when the student clicks 'Submit Application'."""
        amount_text = (amount_field.value or "").replace(",", "").strip()
        try:
            amount = float(amount_text)
        except ValueError:
            amount = None

        total_pool = get_budget()[0]
        # (This single comparison also rejects 'nan', zero and negative numbers.)
        if amount is None or not (0 < amount <= total_pool):
            notice.show(f"Please enter an amount between 1 and {format_peso(total_pool)}.", False)
            page.update()
            return

        if selected["path"] is None:
            notice.show("Please choose your tax / income document first.", False)
            page.update()
            return

        success, message = submit_application(user["user_id"], amount, selected["path"])
        notice.show(message, success)

        if success:
            amount_field.value = ""
            selected["path"] = None
            file_label.value = "No file chosen yet"
            file_label.color = COLOR_MUTED
            refresh_tracker()
        page.update()

    def handle_logout(e):
        show_welcome_screen(page, file_picker)

    refresh_tracker()

    header = make_header(
        f"Hi, {user['first_name']}!",
        f"{user['full_name']}  •  {user['education_level']} student",
        user["first_name"][:1].upper(),
        handle_logout,
    )

    upload_box = ft.Container(
        padding=14,
        border_radius=14,
        bgcolor=COLOR_SOFT_PRIMARY,
        content=ft.Column(
            [
                ft.Row(
                    [
                        make_icon_badge(ft.Icons.UPLOAD_FILE, COLOR_PRIMARY, "#FFFFFF", 40),
                        ft.Column(
                            [make_title("Tax / income document", 14), file_label],
                            spacing=1,
                            expand=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                make_outline_button("Choose document", handle_choose_file, ft.Icons.FOLDER_OPEN),
                make_muted("Accepted files: PDF, PNG, JPG", 12),
            ],
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )

    form_card = make_card(
        ft.Column(
            [
                make_title("Apply for a Scholarship", 20),
                make_muted("Enter the amount you need and attach proof of income."),
                amount_field,
                upload_box,
                notice.box,
                make_primary_button("Submit Application", handle_submit, ft.Icons.SEND),
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        col={"xs": 12, "md": 5},   # full width on phones, 5 of 12 columns on larger screens
    )

    tracker_section = ft.Container(
        col={"xs": 12, "md": 7},
        content=ft.Column(
            [make_title("My Applications", 20), tracker_column],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )

    show_dashboard_page(
        page,
        [
            header,
            ft.ResponsiveRow([form_card, tracker_section], spacing=16, run_spacing=16),
        ],
    )


# ===== SECTION 7: COMMITTEE DASHBOARD =====

def show_committee_dashboard(page, user, file_picker):
    """
    Committee dashboard: budget on top, application queue on the left, review panel on the right
    (stacked on phones). file_picker is only passed along so Logout can return to the start.
    """

    # Remember which application is open in the review panel (None = nothing).
    state = {"selected": None}

    notice = Notice()
    budget_title = make_title("", 20)
    budget_caption = make_muted("")
    budget_bar = ft.ProgressBar(value=0, color=COLOR_PRIMARY, bgcolor=COLOR_LINE)
    pending_count_text = make_title("0", 30, COLOR_PRIMARY)
    queue_list = ft.ListView(height=430, spacing=10)
    review_panel = ft.Column(spacing=14, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def refresh_budget():
        """Reload the budget numbers, e.g. 'Available Funds: ₱850,000 / ₱1,000,000'."""
        total_pool, allocated = get_budget()
        available = total_pool - allocated
        budget_title.value = f"Available Funds: {format_peso(available)} / {format_peso(total_pool)}"
        budget_caption.value = f"{format_peso(allocated)} has been allocated so far."
        budget_bar.value = available / total_pool if total_pool > 0 else 0   # the bar shows how much money is left

    def show_empty_panel(message="Select an application from the queue to start reviewing.", redraw=True):
        """Show a friendly 'nothing selected' message in the right-hand panel."""
        review_panel.controls.clear()
        review_panel.controls.append(
            ft.Container(
                padding=30,
                content=ft.Column(
                    [
                        ft.Icon(ft.Icons.RATE_REVIEW, size=48, color=COLOR_MUTED),
                        make_muted(message, 14, text_align=ft.TextAlign.CENTER),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10,
                ),
            )
        )
        if redraw:
            page.update()

    def handle_select_application(app_id):
        """Runs when a committee member clicks an application in the queue."""
        notice.hide()
        state["selected"] = app_id
        refresh_queue()
        open_review_panel(app_id)

    def refresh_queue():
        """Reload the list of Pending applications on the left."""
        queue_list.controls.clear()
        pending = get_pending_applications()
        pending_count_text.value = str(len(pending))

        if len(pending) == 0:
            queue_list.controls.append(
                ft.Container(
                    padding=24,
                    content=ft.Column(
                        [
                            ft.Icon(ft.Icons.INBOX, size=40, color=COLOR_MUTED),
                            make_muted("No pending applications.", 14),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=8,
                    ),
                )
            )

        for app_id, requested_amount, submission_date, review_count in pending:
            if state["selected"] == app_id:
                tile_color = COLOR_SELECTED
            else:
                tile_color = COLOR_SOFT_PRIMARY
            queue_list.controls.append(
                ft.Container(
                    # a=app_id freezes THIS row's id in the lambda; otherwise every row would use the last id.
                    on_click=lambda e, a=app_id: handle_select_application(a),
                    ink=True,
                    padding=14,
                    border_radius=14,
                    bgcolor=tile_color,
                    content=ft.Row(
                        [
                            make_icon_badge(ft.Icons.DESCRIPTION, COLOR_PRIMARY, "#FFFFFF", 42),
                            ft.Column(
                                [
                                    make_title(format_peso(requested_amount), 16),
                                    make_muted(f"Application #{app_id}  •  {submission_date}", 12),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            make_pill(f"{review_count}/{REVIEWS_NEEDED}", COLOR_INFO, COLOR_INFO_SOFT),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

    def handle_decision(app_id, decision):
        """Runs when a final-decision button (Approve / Reject / Revision) is clicked."""
        success, message = decide_application(app_id, decision)
        notice.show(message, success)
        refresh_budget()
        if success:
            state["selected"] = None
            refresh_queue()
            show_empty_panel("Decision saved. Select another application from the queue.")
        else:
            refresh_queue()
            open_review_panel(app_id)

    def open_review_panel(app_id):
        """Fill the right-hand panel with everything about one application."""
        details = get_application_details(app_id)
        if details is None or details[3] != "Pending":
            state["selected"] = None
            refresh_queue()
            show_empty_panel("That application is no longer pending.")
            return

        requested_amount, submission_date = details[1], details[2]
        reviews = get_reviews_for_app(app_id)
        review_count = len(reviews)

        already_reviewed = False
        for review in reviews:
            if review[0] == user["user_id"]:   # review[0] is the reviewer_id
                already_reviewed = True

        def handle_open_document(e):
            """Runs when 'Open Document' is clicked."""
            path = get_document_path(app_id)
            if path is None:
                notice.show("No document was found for this application.", False)
            else:
                success, message = open_document(path)
                if success:
                    notice.show("Document opened in your default viewer.", True)
                else:
                    notice.show(message, False)
            page.update()

        review_panel.controls.clear()

        review_panel.controls.extend([
            ft.Row(
                [
                    make_title(f"Application #{app_id}", 22),
                    make_pill("Pending", *STATUS_STYLES["Pending"]),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=True,
                run_spacing=8,
            ),
            ft.Row(
                [
                    make_fact("Requested amount", format_peso(requested_amount), COLOR_PRIMARY),
                    make_fact("Submitted", submission_date),
                ],
                spacing=32,
                wrap=True,
                run_spacing=10,
            ),
            make_outline_button("Open Document", handle_open_document, ft.Icons.OPEN_IN_NEW),
            ft.Divider(height=1, color=COLOR_LINE),
            make_muted(f"Reviews submitted: {review_count} / {REVIEWS_NEEDED}", 13),
            ft.ProgressBar(value=review_count / REVIEWS_NEEDED, color=COLOR_PRIMARY, bgcolor=COLOR_LINE),
        ])

        if already_reviewed:
            review_panel.controls.append(
                make_pill("You have already reviewed this application", COLOR_SUCCESS, COLOR_SUCCESS_SOFT)
            )
        elif review_count >= REVIEWS_NEEDED:
            review_panel.controls.append(
                make_muted("This application already has all of its reviews.")
            )
        else:
            score_dropdown = ft.Dropdown(
                label="Your score (1 = weak, 5 = excellent)",
                leading_icon=ft.Icons.STAR,
                options=[ft.DropdownOption(key=str(n)) for n in range(1, 6)],
                expand=True,
            )
            notes_field = make_text_field(
                "Notes / feedback for the student", None,
                multiline=True, min_lines=3, max_lines=6,
            )

            def handle_submit_review(e):
                """Runs when 'Submit Review' is clicked."""
                if score_dropdown.value is None:
                    notice.show("Please choose a score from 1 to 5.", False)
                    page.update()
                    return

                success, message = submit_review(
                    app_id,
                    user["user_id"],
                    int(score_dropdown.value),
                    (notes_field.value or "").strip(),
                )
                notice.show(message, success)
                refresh_queue()
                open_review_panel(app_id)  # redraw the panel (this also updates the page)

            review_panel.controls.extend([
                make_title("Your Review", 17),
                make_muted("Other reviewers' scores stay hidden until all 3 reviews are in.", 12),
                ft.Row([score_dropdown]),
                notes_field,
                make_primary_button("Submit Review", handle_submit_review, ft.Icons.SEND),
            ])

        # --- THE CONSENSUS LOGIC: once there are exactly 3 reviews ---
        if review_count == REVIEWS_NEEDED:
            average = calculate_average(reviews)
            reached_goal = average >= APPROVAL_THRESHOLD

            review_panel.controls.append(ft.Divider(height=1, color=COLOR_LINE))
            review_panel.controls.append(
                ft.Container(
                    padding=16,
                    border_radius=14,
                    bgcolor=COLOR_SUCCESS_SOFT if reached_goal else COLOR_DANGER_SOFT,
                    content=ft.Row(
                        [
                            make_fact("Average score", f"{average:.2f} / 5",
                                      COLOR_SUCCESS if reached_goal else COLOR_DANGER),
                            make_muted(f"Needed to approve: {APPROVAL_THRESHOLD}"),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        wrap=True,
                        run_spacing=6,
                    ),
                )
            )
            for number, review in enumerate(reviews, start=1):
                score, notes = review[1], review[2]
                review_panel.controls.append(
                    ft.Container(
                        padding=12,
                        border_radius=12,
                        bgcolor=COLOR_PAGE_BG,
                        content=ft.Column(
                            [
                                make_muted(f"Reviewer {number}  •  score {score}", 12),
                                ft.Text(notes or "(no notes)", size=14, color=COLOR_TEXT),
                            ],
                            spacing=2,
                        ),
                    )
                )

            if reached_goal:
                review_panel.controls.append(
                    make_primary_button(
                        "Approve & Allocate",
                        lambda e: handle_decision(app_id, "Approved"),
                        ft.Icons.CHECK_CIRCLE,
                        COLOR_SUCCESS,
                    )
                )
            else:
                review_panel.controls.extend([
                    make_muted(f"The average is below {APPROVAL_THRESHOLD}, so this application cannot be approved."),
                    ft.Row(
                        [
                            make_outline_button("Request Revision",
                                                lambda e: handle_decision(app_id, "Needs Revision"),
                                                ft.Icons.EDIT, COLOR_INFO),
                            make_outline_button("Reject Application",
                                                lambda e: handle_decision(app_id, "Rejected"),
                                                ft.Icons.CLOSE, COLOR_DANGER),
                        ],
                        wrap=True,
                        spacing=10,
                        run_spacing=10,
                    ),
                ])

        page.update()

    def handle_refresh(e):
        """Runs when 'Refresh' is clicked: reload the budget and the queue."""
        refresh_budget()
        refresh_queue()
        page.update()

    def handle_logout(e):
        show_welcome_screen(page, file_picker)

    refresh_budget()
    refresh_queue()
    show_empty_panel(redraw=False)

    header = make_header(
        f"Hi, {user['first_name']}!",
        f"{user['full_name']}  •  Scholarship Committee",
        user["first_name"][:1].upper(),
        handle_logout,
    )

    budget_card = make_card(
        ft.Row(
            [
                make_icon_badge(ft.Icons.ACCOUNT_BALANCE_WALLET, COLOR_PRIMARY, COLOR_SOFT_PRIMARY, 52),
                ft.Column(
                    [budget_title, budget_bar, budget_caption],
                    spacing=8,
                    expand=True,
                ),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        col={"xs": 12, "md": 8},
    )
    pending_card = make_card(
        ft.Row(
            [
                make_icon_badge(ft.Icons.ASSIGNMENT, COLOR_WARNING, COLOR_WARNING_SOFT, 52),
                ft.Column([make_muted("Pending applications"), pending_count_text], spacing=0, expand=True),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        col={"xs": 12, "md": 4},
    )

    queue_card = make_card(
        ft.Column(
            [
                ft.Row(
                    [
                        make_title("Application Queue", 18),
                        make_link_button("Refresh", handle_refresh, ft.Icons.REFRESH),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                queue_list,
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        col={"xs": 12, "md": 5},
    )
    review_card = make_card(review_panel, col={"xs": 12, "md": 7})

    show_dashboard_page(
        page,
        [
            header,
            ft.ResponsiveRow([budget_card, pending_card], spacing=16, run_spacing=16),
            notice.box,
            ft.ResponsiveRow([queue_card, review_card], spacing=16, run_spacing=16),
        ],
    )


# ===== SECTION 8: SUPER ADMIN DASHBOARD =====

def show_admin_dashboard(page, user, file_picker):
    """
    Super Admin dashboard: change the available funds, and delete students or committee members
    (after a confirmation click).
    """
    # Which tab is open, and which user (if any) is waiting for delete confirmation.
    state = {"tab": "student", "confirm": None}

    notice = Notice()
    student_count_text = make_title("0", 30, COLOR_PRIMARY)
    committee_count_text = make_title("0", 30, COLOR_PRIMARY)
    budget_title = make_title("", 18)
    budget_caption = make_muted("")
    funds_field = make_text_field(
        "New available funds (₱)", ft.Icons.PAYMENTS, keyboard_type=ft.KeyboardType.NUMBER
    )
    tab_row = ft.Row(spacing=10, run_spacing=10, wrap=True)
    user_list = ft.ListView(height=460, spacing=10)

    def build_user_row(row):
        """Build the tile for ONE user (or its 'are you sure?' version)."""
        user_id, username, full_name, role, education_level = row

        details = f"@{username}"
        if education_level:
            details = details + f"  •  {education_level}"

        who = ft.Row(
            [
                make_icon_badge(ft.Icons.PERSON, COLOR_PRIMARY, "#FFFFFF", 42),
                ft.Column([make_title(full_name, 15), make_muted(details, 12)], spacing=1, expand=True),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

        if state["confirm"] == user_id:
            if role == "student":
                warning = ("Delete this student for good? Their applications and documents are "
                           "deleted too, and any funds already allocated to them go back to the budget.")
            else:
                warning = "Delete this committee member for good? All of their reviews are deleted too."
            content = ft.Column(
                [
                    who,
                    ft.Text(warning, size=13, color=COLOR_DANGER, weight=ft.FontWeight.W_600),
                    ft.Row(
                        [
                            # "uid=user_id" saves THIS row's id inside the lambda (same trick as the queue).
                            make_primary_button("Yes, delete", lambda e, uid=user_id: handle_confirm_delete(uid),
                                                ft.Icons.DELETE, COLOR_DANGER),
                            make_outline_button("Cancel", handle_cancel, ft.Icons.CLOSE, COLOR_MUTED),
                        ],
                        spacing=10,
                        run_spacing=10,
                        wrap=True,
                    ),
                ],
                spacing=10,
            )
            tile_color = COLOR_DANGER_SOFT
        else:
            content = ft.Row(
                [
                    who,
                    make_outline_button("Delete", lambda e, uid=user_id: handle_ask(uid),
                                        ft.Icons.DELETE, COLOR_DANGER),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            tile_color = COLOR_SOFT_PRIMARY

        return ft.Container(padding=14, border_radius=14, bgcolor=tile_color, content=content)

    def refresh_screen():
        """Reload the counts, the tab buttons and the list of users from the database."""
        students = get_users_by_role("student")
        committee = get_users_by_role("committee")
        student_count_text.value = str(len(students))

        total_pool, allocated = get_budget()
        budget_title.value = (f"Available Funds: {format_peso(total_pool - allocated)} "
                              f"/ {format_peso(total_pool)}")
        budget_caption.value = f"{format_peso(allocated)} is already allocated to approved applications."
        committee_count_text.value = str(len(committee))

        tab_row.controls.clear()
        tabs = (
            ("student", "Students", ft.Icons.SCHOOL, len(students)),
            ("committee", "Committee", ft.Icons.GROUPS, len(committee)),
        )
        for tab_name, label, icon, count in tabs:
            handler = lambda e, t=tab_name: handle_tab(t)
            if state["tab"] == tab_name:
                tab_row.controls.append(make_primary_button(f"{label} ({count})", handler, icon))
            else:
                tab_row.controls.append(make_outline_button(f"{label} ({count})", handler, icon))

        if state["tab"] == "student":
            rows = students
        else:
            rows = committee

        user_list.controls.clear()
        if len(rows) == 0:
            user_list.controls.append(
                ft.Container(
                    padding=24,
                    content=ft.Column(
                        [
                            ft.Icon(ft.Icons.INBOX, size=40, color=COLOR_MUTED),
                            make_muted("No users here yet.", 14),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=8,
                    ),
                )
            )
        for row in rows:
            user_list.controls.append(build_user_row(row))

    def handle_tab(tab_name):
        """Switch between the Students list and the Committee list."""
        state["tab"] = tab_name
        state["confirm"] = None
        notice.hide()
        refresh_screen()
        page.update()

    def handle_ask(user_id):
        """First click on 'Delete': ask for confirmation."""
        state["confirm"] = user_id
        notice.hide()
        refresh_screen()
        page.update()

    def handle_cancel(e):
        state["confirm"] = None
        refresh_screen()
        page.update()

    def handle_confirm_delete(user_id):
        """Second click ('Yes, delete'): really delete the user."""
        success, message = delete_user(user_id, user)
        notice.show(message, success)
        state["confirm"] = None
        refresh_screen()
        page.update()

    def handle_logout(e):
        show_welcome_screen(page, file_picker)

    def handle_set_funds(e):
        """Runs when the admin clicks 'Update Available Funds' (or presses Enter in the box)."""
        amount_text = (funds_field.value or "").replace(",", "").strip()
        success, message = set_available_funds(amount_text, user)
        notice.show(message, success)
        if success:
            funds_field.value = ""
            refresh_screen()
        page.update()

    funds_field.on_submit = handle_set_funds

    refresh_screen()

    header = make_header(
        f"Hi, {user['first_name']}!",
        "Super Admin  •  User management",
        user["first_name"][:1].upper(),
        handle_logout,
    )

    student_card = make_card(
        ft.Row(
            [
                make_icon_badge(ft.Icons.SCHOOL, COLOR_PRIMARY, COLOR_SOFT_PRIMARY, 52),
                ft.Column([make_muted("Students"), student_count_text], spacing=0, expand=True),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        col={"xs": 12, "sm": 6},
    )
    committee_card = make_card(
        ft.Row(
            [
                make_icon_badge(ft.Icons.GROUPS, COLOR_ACCENT, COLOR_SOFT_PRIMARY, 52),
                ft.Column([make_muted("Committee members"), committee_count_text], spacing=0, expand=True),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        col={"xs": 12, "sm": 6},
    )

    budget_card = make_card(
        ft.Column(
            [
                ft.Row(
                    [
                        make_icon_badge(ft.Icons.ACCOUNT_BALANCE_WALLET, COLOR_PRIMARY, COLOR_SOFT_PRIMARY, 52),
                        ft.Column(
                            [make_muted("Scholarship budget"), budget_title, budget_caption],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    spacing=16,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                funds_field,
                make_muted("Money already given to approved applications stays allocated. "
                           "The total pool becomes that amount plus the new available funds.", 12),
                make_primary_button("Update Available Funds", handle_set_funds, ft.Icons.SAVE),
            ],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
    )

    manage_card = make_card(
        ft.Column(
            [
                make_title("Manage Users", 18),
                tab_row,
                user_list,
            ],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
    )

    show_dashboard_page(
        page,
        [
            header,
            ft.ResponsiveRow([student_card, committee_card], spacing=16, run_spacing=16),
            budget_card,
            notice.box,
            manage_card,
        ],
    )


# ===== SECTION 9: PROGRAM START =====

def main(page: ft.Page):
    """Flet calls this function when the app window opens."""
    page.title = "Scholarship Application & Financial Aid Portal"
    page.padding = 0                      # let our own containers fill the whole window
    page.bgcolor = COLOR_PAGE_BG
    page.theme_mode = ft.ThemeMode.LIGHT  # our colors are designed for a light theme
    page.theme = ft.Theme(color_scheme_seed=COLOR_PRIMARY)

    # Cosmetic starting size; skipped if it fails.
    try:
        page.window.width = 1100
        page.window.height = 780
        page.window.min_width = 360
        page.window.min_height = 520
    except Exception:
        pass

    create_uploads_folder()
    init_database()

    # The FilePicker must be registered with the page once, then we reuse it.
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    show_welcome_screen(page, file_picker)


if __name__ == "__main__":
    # Opens the app in a desktop window. Desktop mode is required: only there does the
    # file picker give the real file path that shutil.copy needs.
    ft.run(main)
