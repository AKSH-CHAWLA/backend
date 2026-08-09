import sqlite3
from pathlib import Path


# ==========================================================
# DATABASE PATHS
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "portal.db"


# ==========================================================
# DATABASE CONNECTION
# ==========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ==========================================================
# INITIALIZE DATABASE
# ==========================================================

def init_db():
    conn = get_db()

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            role TEXT DEFAULT 'member',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT,
            ext TEXT,
            size INTEGER,
            kind TEXT,
            converted_path TEXT,
            uploaded_by INTEGER,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (uploaded_by) REFERENCES users(id)
        );
        """
    )

    # Migration:
    # Add conversion_error column if an older database
    # does not already contain it.
    cols = [
        r["name"]
        for r in conn.execute(
            "PRAGMA table_info(files)"
        ).fetchall()
    ]

    if "conversion_error" not in cols:
        conn.execute(
            "ALTER TABLE files ADD COLUMN conversion_error TEXT"
        )

    conn.commit()
    conn.close()


# ==========================================================
# OPTIONAL: INITIALIZE DATABASE WHEN RUN DIRECTLY
# ==========================================================

if __name__ == "__main__":
    init_db()
    print(f"✓ Database initialized at {DB_PATH}")