"""Prokly: schema migrations.

Отдельная SQLite БД (prokly.db) — намеренно изолирована от spy_data.db.
Если что-то сломается тут, основной бот не страдает.
"""
import sqlite3
from pathlib import Path

PROKLY_DATA = Path(__file__).parent.parent / "prokly_data"
PROKLY_DB = PROKLY_DATA / "prokly.db"
SNAPSHOTS_DIR = PROKLY_DATA / "snapshots"

PROKLY_DATA.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def connect():
    conn = sqlite3.connect(str(PROKLY_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # soft-FK к items
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Идемпотентная инициализация схемы."""
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prokly_proxies (
                geo TEXT PRIMARY KEY,
                proxy_url TEXT NOT NULL,
                label TEXT,
                last_tested_at TIMESTAMP,
                test_status TEXT DEFAULT 'untested',
                detected_country TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prokly_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prokly_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                job_id INTEGER,
                url TEXT NOT NULL,
                final_url TEXT,
                geo TEXT,
                status TEXT NOT NULL,
                screenshot_path TEXT,
                html_path TEXT,
                page_title TEXT,
                proxy_used TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration_ms INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prokly_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prokly_jobs_status ON prokly_jobs(status, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prokly_jobs_item ON prokly_jobs(item_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prokly_snap_item ON prokly_snapshots(item_id, fetched_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prokly_snap_geo ON prokly_snapshots(geo, fetched_at DESC)"
        )
        # Default: auto = off
        conn.execute(
            "INSERT OR IGNORE INTO prokly_settings (key, value) VALUES ('auto', 'off')"
        )
        # Migrations: add per-geo device-fingerprint columns to prokly_proxies
        for col_def in [
            "user_agent TEXT",
            "locale TEXT",
            "timezone TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE prokly_proxies ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Migrations: add archive + thumb columns to prokly_snapshots
        for col_def in [
            "archive_path TEXT",
            "thumb_path TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE prokly_snapshots ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def get_setting(key, default=None):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM prokly_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO prokly_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
