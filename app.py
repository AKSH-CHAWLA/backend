import os
import time
import shutil
import secrets
import tempfile
import subprocess
from functools import wraps
from pathlib import Path

from flask import (
    Flask, request, session, jsonify, send_file, send_from_directory,
    redirect,
)
from flask_cors import CORS
from werkzeug.security import check_password_hash

from db import get_db, init_db, BASE_DIR

UPLOAD_DIR = BASE_DIR / "uploads"
CONVERTED_DIR = BASE_DIR / "converted"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)
CONVERTED_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)
CORS(
    app,
    origins=["https://marvelous-heliotrope-053358.netlify.app/"],
    supports_credentials=True
)
app.secret_key = os.environ["SESSION_SECRET"]

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

OFFICE_EXTS = {".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".odt", ".odp", ".ods"}
CODE_EXTS = {
    ".js", ".ts", ".jsx", ".tsx", ".py", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".json", ".yml", ".yaml", ".html", ".css",
    ".sql", ".md", ".txt", ".xml", ".ini", ".toml", ".dockerfile", ".r", ".m",
    ".kt", ".swift", ".dart", ".lua", ".pl", ".scala", ".vue", ".bat", ".ps1",
    ".env", ".gitignore", ".yarnrc", ".cfg", ".conf",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}


def classify(ext):
    ext = ext.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in CODE_EXTS:
        return "code"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login.html")
        return f(*args, **kwargs)
    return wrapper


# ---------- Auth ----------

@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username, password = data.get("username"), data.get("password")
    if not username or not password:
        return jsonify({"error": "Username and password required."}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect username or password."}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session.permanent = True

    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"], "username": user["username"],
            "displayName": user["display_name"], "role": user["role"],
        },
    })


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def me():
    if not session.get("user_id"):
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({
        "id": session["user_id"], "username": session["username"], "role": session["role"],
    })


@app.get("/login.html")
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return send_from_directory(STATIC_DIR, "login.html")


@app.get("/")
@login_required
def dashboard():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.get("/css/<path:filename>")
def css(filename):
    return send_from_directory(STATIC_DIR / "css", filename)


@app.get("/js/<path:filename>")
def js(filename):
    return send_from_directory(STATIC_DIR / "js", filename)


# ---------- LibreOffice conversion ----------

def find_soffice():
    """Locate the soffice binary: env override > PATH > common install dirs."""
    override = os.environ.get("LIBREOFFICE_PATH")
    if override and Path(override).exists():
        return override

    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found

    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def convert_to_pdf(input_path: Path):
    """Runs headless LibreOffice conversion in an isolated profile so it can't
    collide with a LibreOffice window you already have open (a common cause
    of silent failures on Windows). Returns (output_path, None) on success,
    or (None, error_message) on failure."""
    soffice_bin = find_soffice()
    if not soffice_bin:
        return None, (
            "LibreOffice ('soffice') was not found on this machine. Set the "
            "LIBREOFFICE_PATH environment variable to the full path of "
            "soffice.exe (e.g. C:\\Program Files\\LibreOffice\\program\\soffice.exe), "
            "or add that folder to your PATH and restart the terminal."
        )

    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        profile_uri = Path(profile_dir).resolve().as_uri()
        result = subprocess.run(
            [
                soffice_bin, "--headless", "--norestore", "--nolockcheck",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to", "pdf", "--outdir", str(CONVERTED_DIR), str(input_path),
            ],
            timeout=120, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        return None, "Conversion timed out after 120 seconds."
    except FileNotFoundError:
        return None, f"Could not execute '{soffice_bin}'. Check the path is correct."
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    out_path = CONVERTED_DIR / f"{input_path.stem}.pdf"
    if out_path.exists():
        return out_path, None

    err = (result.stderr or result.stdout or "Unknown conversion error").strip()
    return None, err[:500]


# ---------- Files ----------

@app.post("/api/files/upload")
@login_required
def upload_files():
    files = request.files.getlist("files")
    conn = get_db()
    results = []

    for file in files:
        if not file or not file.filename:
            continue
        original_name = file.filename
        ext = Path(original_name).suffix
        kind = classify(ext)

        stored_name = f"{int(time.time() * 1000)}-{secrets.token_hex(8)}{ext}"
        dest = UPLOAD_DIR / stored_name
        file.save(dest)
        size = dest.stat().st_size

        converted_rel = None
        conversion_error = None
        if kind == "office":
            out_path, err = convert_to_pdf(dest)
            if out_path:
                converted_rel = str(out_path.relative_to(BASE_DIR))
            else:
                conversion_error = err
                print(f"Conversion failed for {original_name}: {err}")

        cur = conn.execute(
            """INSERT INTO files
               (original_name, stored_name, mime_type, ext, size, kind, converted_path, conversion_error, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (original_name, stored_name, file.mimetype, ext, size, kind,
             converted_rel, conversion_error, session["user_id"]),
        )
        results.append({
            "id": cur.lastrowid, "name": original_name, "kind": kind,
            "conversionError": conversion_error,
        })

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "files": results})


@app.get("/api/files")
@login_required
def list_files():
    conn = get_db()
    rows = conn.execute(
        """SELECT f.id, f.original_name, f.kind, f.ext, f.size, f.uploaded_at,
                  f.converted_path, f.conversion_error, u.username AS uploaded_by
           FROM files f LEFT JOIN users u ON f.uploaded_by = u.id
           ORDER BY f.uploaded_at DESC"""
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


def _get_file_row(file_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    return row


@app.get("/api/files/<int:file_id>/raw")
@login_required
def raw_file(file_id):
    row = _get_file_row(file_id)
    if not row:
        return "", 404
    return send_from_directory(
        UPLOAD_DIR, row["stored_name"],
        download_name=row["original_name"], as_attachment=False,
    )


@app.get("/api/files/<int:file_id>/view-pdf")
@login_required
def view_pdf(file_id):
    row = _get_file_row(file_id)
    if not row:
        return jsonify({"error": "File not found."}), 404

    if row["kind"] == "pdf":
        target = UPLOAD_DIR / row["stored_name"]
    elif row["converted_path"]:
        target = BASE_DIR / row["converted_path"]
        if not target.exists():
            return jsonify({"error": "The converted PDF is missing on disk. Try re-uploading."}), 410
    else:
        reason = row["conversion_error"] or "No PDF representation available for this file."
        return jsonify({"error": reason}), 415

    return send_file(target, mimetype="application/pdf")


@app.get("/api/files/<int:file_id>/text")
@login_required
def file_text(file_id):
    row = _get_file_row(file_id)
    if not row or row["kind"] != "code":
        return "", 404
    content = (UPLOAD_DIR / row["stored_name"]).read_text(encoding="utf-8", errors="replace")
    return jsonify({"content": content, "ext": row["ext"]})


@app.delete("/api/files/<int:file_id>")
@login_required
def delete_file(file_id):
    row = _get_file_row(file_id)
    if not row:
        return "", 404

    for p in [UPLOAD_DIR / row["stored_name"],
              (BASE_DIR / row["converted_path"]) if row["converted_path"] else None]:
        if p and p.exists():
            p.unlink()

    conn = get_db()
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    if not find_soffice():
        print(
            "⚠ LibreOffice ('soffice') was not found on this machine. PPT/DOC/XLS "
            "files will upload fine but will FAIL to preview until LibreOffice is "
            "installed and on PATH, or the LIBREOFFICE_PATH env var is set to the "
            "soffice binary. Download: https://www.libreoffice.org/download/download/"
        )
    port = int(os.environ.get("PORT", 3000))
    print(f"Portal running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
