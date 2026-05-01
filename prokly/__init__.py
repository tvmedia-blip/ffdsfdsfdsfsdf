"""Prokly: snapshot landings via per-geo proxy.

Public API (всё что должен знать app.py):
    register_routes(app, check_auth) — добавляет /api/prokly/* + воркер
    maybe_capture(item_id)            — auto-snapshot хук (no-op если auto=off)
"""
import logging

log = logging.getLogger("prokly")

# Re-export
try:
    from .routes import register_routes  # noqa: F401
except Exception as e:
    log.warning("[prokly] register_routes import failed: %s", e)
    def register_routes(app, check_auth):  # type: ignore
        log.warning("[prokly] disabled — register_routes is no-op")


def maybe_capture(item_id):
    """Hook: вызывается из save_item() после INSERT.

    Если auto-режим включён — добавляет item в очередь снимков.
    Никогда не бросает наружу — основной save_item не должен страдать.
    """
    try:
        from . import db, jobs
        if db.get_setting("auto", "off") != "on":
            return None
        return jobs.enqueue(int(item_id), dedup=True)
    except Exception as e:
        log.debug("[prokly] maybe_capture(%s) failed: %s", item_id, e)
        return None
