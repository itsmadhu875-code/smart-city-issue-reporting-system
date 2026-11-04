
import os
from functools import wraps

import psycopg
from psycopg.rows import dict_row

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from PIL import Image


app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable set nahi hai.")

csrf = CSRFProtect(app)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER") == "true"
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 500 * 1024

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# Neon PostgreSQL connection
def get_db():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable set nahi hai.")

    return psycopg.connect(database_url, row_factory=dict_row)


# Create PostgreSQL table
def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                category TEXT NOT NULL,
                location TEXT NOT NULL,
                description TEXT NOT NULL,
                photo TEXT,
                status TEXT NOT NULL DEFAULT 'Pending'
            )
        """)


# Validate uploaded file
def allowed_file(filename):
    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_EXTENSIONS


def validate_image(file):
    try:
        image = Image.open(file)

        if image.width > 5000 or image.height > 5000:
            return False

        image.verify()
        file.seek(0)

        return image.format in ("JPEG", "PNG", "WEBP")

    except Exception:
        file.seek(0)
        return False


# Home Page
@app.route("/")
def home():
    return render_template("index.html")


# Complaint Form
@app.route("/report")
def report():
    return render_template("report.html")


# Submit Complaint
@app.route("/submit-complaint", methods=["POST"])
def submit_complaint():

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    category = request.form.get("category", "").strip()
    location = request.form.get("location", "").strip()
    description = request.form.get("description", "").strip()

    if not all([name, email, category, location, description]):
        return "Please fill in all required fields.", 400

    photo_filename = None
    photo = request.files.get("photo")

    if photo and photo.filename:

        if not allowed_file(photo.filename):
            return "Invalid photo format. Use JPG, JPEG, PNG or WEBP.", 400

        if not validate_image(photo):
            return "Invalid image file.", 400

        photo_filename = secure_filename(photo.filename)

        original_name, extension = os.path.splitext(photo_filename)
        counter = 1

        while os.path.exists(
            os.path.join(UPLOAD_FOLDER, photo_filename)
        ):
            photo_filename = f"{original_name}_{counter}{extension}"
            counter += 1

        photo.save(os.path.join(UPLOAD_FOLDER, photo_filename))

    with get_db() as conn:
        conn.execute("""
            INSERT INTO complaints
            (name, email, category, location, description, photo)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            name, email, category, location,
            description, photo_filename
        ))

    return redirect(url_for("view_complaints"))


# View Complaints
@app.route("/complaints")
def view_complaints():

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()

    query = "SELECT * FROM complaints WHERE 1=1"
    params = []

    if search:
        query += """
            AND (
                name ILIKE %s
                OR location ILIKE %s
                OR description ILIKE %s
            )
        """
        search_value = f"%{search}%"
        params.extend([search_value, search_value, search_value])

    if category:
        query += " AND category = %s"
        params.append(category)

    if status in ("Pending", "In Progress", "Resolved"):
       query += " AND status = %s"
       params.append(status)

    query += " ORDER BY id DESC"

    with get_db() as conn:
        complaints = conn.execute(query, params).fetchall()

    return render_template(
        "complaints.html",
        complaints=complaints,
        search=search,
        selected_category=category,
        selected_status=status
    )


# Dashboard
@app.route("/dashboard")
def dashboard():

    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints"
        ).fetchone()["count"]

        pending = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints WHERE status = 'Pending'"
        ).fetchone()["count"]

        resolved = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints WHERE status = 'Resolved'"
        ).fetchone()["count"]
        in_progress = conn.execute(
    "SELECT COUNT(*) AS count FROM complaints WHERE status = 'In Progress'"
).fetchone()["count"]

    return render_template(
        "dashboard.html",
        total=total,
        pending=pending,
        in_progress=in_progress,
        resolved=resolved
    )


# Admin Login Required
def admin_required(view_function):

    @wraps(view_function)
    def wrapped_view(*args, **kwargs):

        if not session.get("admin_logged_in"):
            flash("Please login as admin first.", "error")
            return redirect(url_for("admin_login"))

        return view_function(*args, **kwargs)

    return wrapped_view


# Admin Login
@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        correct_username = os.environ.get("ADMIN_USERNAME")
        password_hash = os.environ.get("ADMIN_PASSWORD_HASH")

        if (
            correct_username
            and password_hash
            and username == correct_username
            and check_password_hash(password_hash, password)
        ):
            session.clear()
            session["admin_logged_in"] = True

            flash("Login successful!", "success")
            return redirect(url_for("admin_panel"))

        flash("Invalid username or password.", "error")

    return render_template("admin_login.html")


# Admin Dashboard
@app.route("/admin")
@admin_required
def admin_panel():

    with get_db() as conn:

        total = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints"
        ).fetchone()["count"]

        pending = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints WHERE status = 'Pending'"
        ).fetchone()["count"]

        in_progress = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints WHERE status = 'In Progress'"
        ).fetchone()["count"]

        resolved = conn.execute(
            "SELECT COUNT(*) AS count FROM complaints WHERE status = 'Resolved'"
        ).fetchone()["count"]

        complaints = conn.execute(
            "SELECT * FROM complaints ORDER BY id DESC"
        ).fetchall()

    return render_template(
        "admin_panel.html",
        total=total,
        pending=pending,
        in_progress=in_progress,
        resolved=resolved,
        complaints=complaints
    )

# Update Complaint Status
@app.route("/admin/update-status/<int:complaint_id>", methods=["POST"])
@admin_required
def admin_update_status(complaint_id):

    new_status = request.form.get("status", "")

    if new_status not in ("Pending", "In Progress", "Resolved"):
        flash("Invalid complaint status.", "error")
        return redirect(url_for("admin_panel"))

    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE complaints SET status = %s WHERE id = %s",
            (new_status, complaint_id)
        )

        updated = cursor.rowcount

    if updated == 0:
        flash("Complaint not found.", "error")
    else:
        flash("Complaint status updated successfully!", "success")

    return redirect(url_for("admin_panel"))


# Admin Logout
@app.route("/admin-logout", methods=["POST"])
@admin_required
def admin_logout():

    session.clear()
    flash("You have logged out.", "success")

    return redirect(url_for("admin_login"))


# Initialize database when application starts
init_db()


if __name__ == "__main__":
    app.run(debug=False)
