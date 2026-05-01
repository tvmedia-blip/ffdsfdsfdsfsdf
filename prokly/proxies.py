"""Prokly: proxy CRUD + parsing + validation (SSRF block + ipinfo test)."""
import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

from . import db

log = logging.getLogger("prokly.proxies")

VALID_SCHEMES = ("http", "https", "socks5", "socks4")


def parse_proxy_url(raw):
    """Парсит разные форматы прокси-URL в dict пригодный для Playwright.

    Поддерживаемые форматы:
        http://user:pass@host:port
        https://user:pass@host:port
        socks5://user:pass@host:port
        host:port
        host:port:user:pass     (Bright Data style)
        user:pass@host:port

    Возвращает:
        {server, username, password, scheme} либо ValueError.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("empty proxy")
    s = raw.strip()

    # Если нет схемы — пробуем угадать
    if "://" not in s:
        # host:port:user:pass (Bright Data) — четыре сегмента через :
        parts = s.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, pwd = parts
            return {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": pwd,
                "scheme": "http",
            }
        # user:pass@host:port
        if "@" in s and s.count(":") >= 2:
            s = "http://" + s
        elif s.count(":") == 1:
            s = "http://" + s
        else:
            raise ValueError("cannot parse proxy without scheme")

    u = urlparse(s)
    scheme = (u.scheme or "http").lower()
    if scheme not in VALID_SCHEMES:
        raise ValueError(f"unsupported scheme: {scheme}")
    if not u.hostname:
        raise ValueError("missing host")
    port = u.port or (443 if scheme == "https" else 1080 if scheme.startswith("socks") else 8080)

    return {
        "server": f"{scheme}://{u.hostname}:{port}",
        "username": u.username or "",
        "password": u.password or "",
        "scheme": scheme,
    }


def is_public_host(host):
    """SSRF-block: запрет приватных и loopback IP-адресов.

    Возвращает (ok: bool, reason: str).
    """
    if not host:
        return False, "empty host"
    # Block obvious local hostnames
    low = host.lower()
    if low in ("localhost", "ip6-localhost", "ip6-loopback") or low.endswith(".local") or low.endswith(".internal"):
        return False, "local hostname"
    # Resolve all addresses
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "DNS lookup failed"
    for info in infos:
        ip_str = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "bad IP from DNS"
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False, f"private/restricted: {ip_str}"
    return True, "ok"


def to_playwright_proxy(parsed):
    """Конвертирует parse_proxy_url() результат в kwargs для Playwright."""
    out = {"server": parsed["server"]}
    if parsed["username"]:
        out["username"] = parsed["username"]
    if parsed["password"]:
        out["password"] = parsed["password"]
    return out


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────

def list_all():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT geo, proxy_url, label, last_tested_at, test_status, detected_country, "
            "       user_agent, locale, timezone, created_at "
            "FROM prokly_proxies ORDER BY geo"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_by_geo(geo):
    if not geo:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM prokly_proxies WHERE geo=?", (geo.upper(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert(geo, proxy_url, label="", user_agent=None, locale=None, timezone=None):
    """Валидирует и сохраняет/обновляет прокси для geo.

    Бросает ValueError при невалидных данных (включая SSRF-блок).
    """
    if not geo or not re.match(r"^[A-Z]{2,3}$", geo.upper()):
        raise ValueError("geo must be 2-3 letter code")
    geo = geo.upper()

    parsed = parse_proxy_url(proxy_url)
    # SSRF: блокируем private/loopback
    host = urlparse(parsed["server"]).hostname
    ok, reason = is_public_host(host)
    if not ok:
        raise ValueError(f"proxy host blocked: {reason}")

    ua = (user_agent or "").strip() or None
    loc = (locale or "").strip() or None
    tz = (timezone or "").strip() or None

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO prokly_proxies (geo, proxy_url, label, test_status, user_agent, locale, timezone) "
            "VALUES (?, ?, ?, 'untested', ?, ?, ?) "
            "ON CONFLICT(geo) DO UPDATE SET "
            "  proxy_url=excluded.proxy_url, "
            "  label=excluded.label, "
            "  test_status='untested', "
            "  last_tested_at=NULL, "
            "  detected_country=NULL, "
            "  user_agent=excluded.user_agent, "
            "  locale=excluded.locale, "
            "  timezone=excluded.timezone",
            (geo, proxy_url.strip(), (label or "").strip(), ua, loc, tz),
        )
        conn.commit()
    finally:
        conn.close()
    return get_by_geo(geo)


def delete(geo):
    if not geo:
        return False
    geo = geo.upper()
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM prokly_proxies WHERE geo=?", (geo,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Live test через прокси: ipinfo.io + ipapi.co (majority vote)
# ──────────────────────────────────────────────────────────────────────

async def _fetch_ip_info(playwright, proxy_dict, endpoint):
    """Возвращает country code через прокси, либо None."""
    browser = None
    try:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(proxy=proxy_dict, ignore_https_errors=True)
        page = await ctx.new_page()
        try:
            await page.goto(endpoint, timeout=15000, wait_until="domcontentloaded")
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        finally:
            await ctx.close()
        # Парсим JSON
        import json
        try:
            data = json.loads(text)
        except Exception:
            return None
        # ipinfo.io: {"country":"GB", ...}
        # ipapi.co:  {"country":"GB", "country_code":"GB", ...}
        country = data.get("country") or data.get("country_code")
        if isinstance(country, str) and len(country) >= 2:
            return country.upper()[:3]
        return None
    except Exception as e:
        log.debug("[test] %s failed: %s", endpoint, e)
        return None
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def test_proxy(geo):
    """Тестирует прокси записи geo: запросы через прокси к 2 source-of-truth, majority vote.

    Обновляет prokly_proxies.test_status и detected_country.
    Возвращает dict со статусом.
    """
    rec = get_by_geo(geo)
    if not rec:
        return {"ok": False, "error": "not found"}
    try:
        parsed = parse_proxy_url(rec["proxy_url"])
        proxy_dict = to_playwright_proxy(parsed)
    except ValueError as e:
        _save_test_result(geo, "parse_error", None)
        return {"ok": False, "error": str(e)}

    # Re-validate host (мог измениться DNS)
    host = urlparse(parsed["server"]).hostname
    ok, reason = is_public_host(host)
    if not ok:
        _save_test_result(geo, "blocked", None)
        return {"ok": False, "error": f"host blocked: {reason}"}

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        c1, c2 = await asyncio.gather(
            _fetch_ip_info(pw, proxy_dict, "https://ipinfo.io/json"),
            _fetch_ip_info(pw, proxy_dict, "https://ipapi.co/json/"),
            return_exceptions=False,
        )

    detected = None
    if c1 and c2 and c1 == c2:
        detected = c1
    elif c1 or c2:
        detected = c1 or c2  # один из двух ответил

    if detected is None:
        _save_test_result(geo, "timeout", None)
        return {"ok": False, "error": "no response from test endpoints", "c1": c1, "c2": c2}

    # Сравниваем с заявленным geo (UK ↔ GB и пр.)
    expected = geo.upper()
    aliases = {"GB": {"GB", "UK"}, "UK": {"GB", "UK"}}
    expected_set = aliases.get(expected, {expected})

    if detected in expected_set:
        _save_test_result(geo, "ok", detected)
        return {"ok": True, "detected": detected}
    else:
        _save_test_result(geo, "wrong_geo", detected)
        return {"ok": False, "error": f"expected {expected}, got {detected}", "detected": detected}


def _save_test_result(geo, status, detected_country):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE prokly_proxies SET "
            "  last_tested_at=CURRENT_TIMESTAMP, "
            "  test_status=?, "
            "  detected_country=? "
            "WHERE geo=?",
            (status, detected_country, geo.upper()),
        )
        conn.commit()
    finally:
        conn.close()
