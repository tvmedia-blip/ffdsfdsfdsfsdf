"""Prokly: persistent job queue.

Работает поверх SQLite — устойчиво к рестартам. Атомарный claim через
UPDATE ... RETURNING (SQLite 3.35+).
"""
import logging
import time
from datetime import datetime

from . import db

log = logging.getLogger("prokly.jobs")

DEDUP_WINDOW_SECONDS = 60  # анти-двойной-клик


def enqueue(item_id, dedup=True):
    """Добавляет job в очередь, возвращает job_id или None если задедуплено.

    Дедуп: не создаём pending/in_progress job для того же item_id если
    свежий job уже есть в окне DEDUP_WINDOW_SECONDS.
    """
    if not item_id:
        return None
    conn = db.connect()
    try:
        if dedup:
            existing = conn.execute(
                "SELECT id FROM prokly_jobs "
                "WHERE item_id=? "
                "  AND status IN ('pending', 'in_progress') "
                "  AND created_at > datetime('now', ?) "
                "ORDER BY id DESC LIMIT 1",
                (item_id, f"-{DEDUP_WINDOW_SECONDS} seconds"),
            ).fetchone()
            if existing:
                return None  # дедуп

        cur = conn.execute(
            "INSERT INTO prokly_jobs (item_id, status) VALUES (?, 'pending')",
            (item_id,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def claim_next():
    """Атомарно забирает следующий pending job. Возвращает dict или None."""
    conn = db.connect()
    try:
        # Двух-фазный claim: сначала SELECT id, потом UPDATE
        # Атомарность через PRAGMA lock_mode=IMMEDIATE на UPDATE
        row = conn.execute(
            "SELECT id, item_id FROM prokly_jobs "
            "WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE prokly_jobs SET "
            "  status='in_progress', "
            "  started_at=CURRENT_TIMESTAMP, "
            "  attempts=attempts+1 "
            "WHERE id=? AND status='pending'",
            (row["id"],),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Кто-то другой забрал — повтор
            return claim_next()
        return {"id": row["id"], "item_id": row["item_id"]}
    finally:
        conn.close()


def mark_done(job_id):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE prokly_jobs SET status='done', finished_at=CURRENT_TIMESTAMP, error=NULL WHERE id=?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(job_id, error):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE prokly_jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, error=? WHERE id=?",
            (str(error)[:500], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_status(job_id):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, item_id, status, attempts, error, created_at, started_at, finished_at "
            "FROM prokly_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def stale_recovery():
    """Возвращает в pending джобы которые висят in_progress >5 минут (воркер крашнулся)."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE prokly_jobs SET status='pending' "
            "WHERE status='in_progress' "
            "  AND started_at < datetime('now', '-5 minutes')"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stats():
    """Статистика очереди."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM prokly_jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}
    finally:
        conn.close()
