"""Prokly: aiohttp routes."""
import asyncio
import logging
from pathlib import Path

from aiohttp import web

from . import db, jobs, proxies

log = logging.getLogger("prokly.routes")


def _unauth():
    return web.json_response({"error": "unauthorized"}, status=401)


def register_routes(app, check_auth):
    """Регистрирует все /api/prokly/* эндпоинты + воркер."""

    db.init_db()

    # ─── Auth helper ────────────────────────────────────────────────
    def auth_ok(request):
        a = check_auth(request)
        return a if a.get("ok") else None

    # ─── /api/prokly/proxies ────────────────────────────────────────
    async def list_proxies_h(request):
        if not auth_ok(request):
            return _unauth()
        return web.json_response(proxies.list_all())

    async def save_proxy_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        geo = data.get("geo", "")
        proxy_url = data.get("proxy_url", "")
        label = data.get("label", "")
        user_agent = data.get("user_agent", "")
        locale = data.get("locale", "")
        timezone = data.get("timezone", "")
        try:
            rec = proxies.upsert(
                geo, proxy_url, label,
                user_agent=user_agent, locale=locale, timezone=timezone,
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(rec)

    async def delete_proxy_h(request):
        if not auth_ok(request):
            return _unauth()
        geo = request.match_info.get("geo", "")
        ok = await asyncio.to_thread(proxies.delete, geo)
        return web.json_response({"deleted": bool(ok)})

    async def test_proxy_h(request):
        if not auth_ok(request):
            return _unauth()
        geo = request.match_info.get("geo", "")
        try:
            res = await proxies.test_proxy(geo)
        except Exception as e:
            log.exception("test_proxy")
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response(res)

    # ─── /api/prokly/snapshot/{item_id} (POST) ──────────────────────
    async def enqueue_snapshot_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            item_id = int(request.match_info.get("item_id", "0"))
        except (ValueError, TypeError):
            return web.json_response({"error": "bad id"}, status=400)
        if item_id <= 0:
            return web.json_response({"error": "bad id"}, status=400)
        job_id = await asyncio.to_thread(jobs.enqueue, item_id, True)
        if job_id is None:
            return web.json_response({"queued": False, "reason": "duplicate within 60s"})
        return web.json_response({"queued": True, "job_id": job_id})

    # ─── /api/prokly/job/{id}/status ────────────────────────────────
    async def job_status_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            job_id = int(request.match_info.get("id", "0"))
        except (ValueError, TypeError):
            return web.json_response({"error": "bad id"}, status=400)
        info = await asyncio.to_thread(jobs.get_status, job_id)
        if not info:
            return web.json_response({"error": "not found"}, status=404)
        # Вернём также последний snapshot для item_id (если есть)
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT id, status, screenshot_path FROM prokly_snapshots "
                "WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            snapshot = dict(row) if row else None
        finally:
            conn.close()
        return web.json_response({"job": info, "snapshot": snapshot})

    # ─── /api/prokly/snapshots ──────────────────────────────────────
    async def list_snapshots_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            page = max(1, int(request.query.get("page", 1)))
            per_page = min(max(1, int(request.query.get("per_page", 30))), 100)
        except (ValueError, TypeError):
            page, per_page = 1, 30
        geo = request.query.get("geo", "").strip()
        status = request.query.get("status", "").strip()
        item_id = request.query.get("item_id", "").strip()

        where = []
        params = []
        if geo:
            where.append("geo=?")
            params.append(geo)
        if status:
            where.append("status=?")
            params.append(status)
        if item_id:
            try:
                params.append(int(item_id))
                where.append("item_id=?")
            except ValueError:
                pass

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * per_page

        conn = db.connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM prokly_snapshots" + where_sql, params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, item_id, job_id, url, final_url, geo, status, "
                "screenshot_path, thumb_path, html_path, archive_path, page_title, fetched_at, duration_ms "
                "FROM prokly_snapshots" + where_sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()
            items = [dict(r) for r in rows]
        finally:
            conn.close()

        return web.json_response({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if total else 0,
        })

    # ─── /api/prokly/file/{snapshot_id}/{kind} ──────────────────────
    # kind: 'screenshot' | 'thumb' | 'html' | 'archive'
    async def serve_file_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            snap_id = int(request.match_info.get("snapshot_id", "0"))
        except (ValueError, TypeError):
            return web.Response(status=400)
        kind = request.match_info.get("kind", "")

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT screenshot_path, thumb_path, html_path, archive_path FROM prokly_snapshots WHERE id=?",
                (snap_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return web.Response(status=404)

        if kind == "screenshot":
            rel = row["screenshot_path"]
            ctype = "image/jpeg"
        elif kind == "thumb":
            # Fallback на screenshot если thumb ещё не сгенерён (старые снимки)
            rel = row["thumb_path"] or row["screenshot_path"]
            ctype = "image/jpeg"
        elif kind == "html":
            rel = row["html_path"]
            ctype = "text/html; charset=utf-8"
        elif kind == "archive":
            rel = row["archive_path"]
            ctype = "application/zip"
        else:
            return web.Response(status=400)

        if not rel:
            return web.Response(status=404)
        full = db.PROKLY_DATA / rel
        if not full.exists():
            return web.Response(status=404)
        # Картинки/архивы immutable — ID стабильный, файл не меняется → можно кэшить надолго.
        # HTML — без кэша (CSP + isolation от localStorage важнее).
        if kind == "html":
            headers = {
                "Content-Type": ctype,
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; img-src * data:; style-src * 'unsafe-inline'; "
                    "font-src * data:; media-src *;"
                ),
                "X-Frame-Options": "SAMEORIGIN",
            }
        else:
            headers = {
                "Content-Type": ctype,
                "Cache-Control": "public, max-age=2592000, immutable",
            }
            if kind == "archive":
                headers["Content-Disposition"] = f'attachment; filename="snapshot_{snap_id}.zip"'
        return web.FileResponse(full, headers=headers)

    # ─── /api/prokly/auto (GET/POST) ────────────────────────────────
    async def get_auto_h(request):
        if not auth_ok(request):
            return _unauth()
        return web.json_response({"auto": db.get_setting("auto", "off")})

    async def set_auto_h(request):
        if not auth_ok(request):
            return _unauth()
        try:
            data = await request.json()
        except Exception:
            data = {}
        val = "on" if data.get("auto") in (True, "on", "true", 1) else "off"
        await asyncio.to_thread(db.set_setting, "auto", val)
        return web.json_response({"auto": val})

    # ─── /api/prokly/stats ──────────────────────────────────────────
    async def stats_h(request):
        if not auth_ok(request):
            return _unauth()
        conn = db.connect()
        try:
            queue = jobs.stats()
            snap_count = conn.execute(
                "SELECT COUNT(*) FROM prokly_snapshots"
            ).fetchone()[0]
            by_status = {
                r["status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) cnt FROM prokly_snapshots GROUP BY status"
                ).fetchall()
            }
            by_geo = {
                r["geo"]: r["cnt"]
                for r in conn.execute(
                    "SELECT geo, COUNT(*) cnt FROM prokly_snapshots WHERE geo IS NOT NULL GROUP BY geo"
                ).fetchall()
            }
        finally:
            conn.close()
        return web.json_response({
            "queue": queue,
            "snapshots_total": snap_count,
            "snapshots_by_status": by_status,
            "snapshots_by_geo": by_geo,
            "auto": db.get_setting("auto", "off"),
        })

    # ─── Snapshot delete (admin only) ───────────────────────────────
    async def delete_snapshot_h(request):
        a = auth_ok(request)
        if not a:
            return _unauth()
        if not a.get("is_admin"):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            snap_id = int(request.match_info["id"])
        except (ValueError, TypeError):
            return web.json_response({"error": "bad request"}, status=400)
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT screenshot_path, html_path FROM prokly_snapshots WHERE id=?",
                [snap_id],
            ).fetchone()
            if not row:
                return web.json_response({"error": "not found"}, status=404)
            for p in (row["screenshot_path"], row["html_path"]):
                if not p:
                    continue
                try:
                    fp = Path(p)
                    if fp.exists():
                        fp.unlink()
                except Exception:
                    pass
            conn.execute("DELETE FROM prokly_snapshots WHERE id=?", [snap_id])
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"ok": True, "id": snap_id})

    # ─── Маршруты ──────────────────────────────────────────────────
    app.router.add_get("/api/prokly/proxies", list_proxies_h)
    app.router.add_post("/api/prokly/proxies", save_proxy_h)
    app.router.add_delete("/api/prokly/proxies/{geo}", delete_proxy_h)
    app.router.add_post("/api/prokly/proxies/{geo}/test", test_proxy_h)
    app.router.add_post("/api/prokly/snapshot/{item_id}", enqueue_snapshot_h)
    app.router.add_get("/api/prokly/job/{id}/status", job_status_h)
    app.router.add_get("/api/prokly/snapshots", list_snapshots_h)
    app.router.add_get("/api/prokly/file/{snapshot_id}/{kind}", serve_file_h)
    app.router.add_get("/api/prokly/auto", get_auto_h)
    app.router.add_post("/api/prokly/auto", set_auto_h)
    app.router.add_get("/api/prokly/stats", stats_h)
    app.router.add_delete("/api/prokly/snapshots/{id}", delete_snapshot_h)

    # ─── Воркер ─────────────────────────────────────────────────────
    async def _start_worker(_app):
        from .crawler import worker_loop_main
        _app["_prokly_stop"] = asyncio.Event()
        _app["_prokly_worker"] = asyncio.create_task(
            worker_loop_main(_app["_prokly_stop"])
        )
        log.info("[PROKLY] worker started")

    async def _stop_worker(_app):
        ev = _app.get("_prokly_stop")
        if ev:
            ev.set()
        task = _app.get("_prokly_worker")
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app.on_startup.append(_start_worker)
    app.on_cleanup.append(_stop_worker)
