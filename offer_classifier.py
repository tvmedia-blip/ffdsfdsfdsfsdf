"""
Offer Classifier
================
Для каждого tracking_url: Playwright → GPT-4o-mini → offer_name.
Карточки с одинаковым offer_name = один кластер.

Использование:
    classify_url(url) -> str или None (None == UNKNOWN/whitepage/dead)
    bootstrap_all()   -> классифицирует все unique URLs в БД
"""
import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from openai import AsyncOpenAI
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from playwright_stealth import Stealth
    _STEALTH = Stealth()
except Exception:
    _STEALTH = None

log = logging.getLogger("offer_classifier")

DB_PATH = "/workspace/fb-bot/spy_data.db"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
FB_REFERER = "https://l.facebook.com/"
PAGE_TIMEOUT = 20000  # 20s
PAGE_WAIT = 2000      # 2s post-load for JS
TEXT_LIMIT = 3000

_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            # Fallback: достаём из app.py
            try:
                import app as _app
                key = getattr(_app, "OPENAI_API_KEY", None)
            except Exception:
                pass
        _openai_client = AsyncOpenAI(api_key=key or "")
    return _openai_client


def url_dedup_key(tracking_url):
    """Нормализует URL для дедупа: domain + path, query отбрасываем."""
    if not tracking_url:
        return None
    try:
        u = urlparse(tracking_url if tracking_url.startswith("http") else "http://" + tracking_url)
        host = (u.netloc or u.path.split("/")[0]).lower().lstrip("www.")
        path = u.path.rstrip("/") or "/"
        if not host:
            return None
        return host + path
    except Exception:
        return None


CF_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser",
    "cf-browser-verification",
    "cf-challenge",
    "ray id:",
)


def _looks_like_cf_challenge(text):
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in CF_MARKERS) and len(t) < 1500


async def _fetch_page_text(url):
    """Открывает ленд через Playwright, возвращает (text, final_url) или (None, None)."""
    try:
        pw_ctx = async_playwright()
        if _STEALTH is not None:
            pw_ctx = _STEALTH.use_async(pw_ctx)
        async with pw_ctx as p:
            browser = await p.chromium.launch(
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            ctx = await browser.new_context(
                user_agent=MOBILE_UA,
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="en-US",
                extra_http_headers={
                    "Referer": FB_REFERER,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Лёгкий stealth: убрать webdriver
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    pass
                await page.wait_for_timeout(PAGE_WAIT)

                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                title = await page.title()

                # Если CF challenge — ждём до 15 сек пока пройдёт
                if _looks_like_cf_challenge((title or "") + "\n" + (text or "")):
                    log.info("[crawl] CF challenge detected, waiting...")
                    for _ in range(15):
                        await page.wait_for_timeout(1000)
                        try:
                            text = await page.evaluate(
                                "() => document.body ? document.body.innerText : ''"
                            )
                            title = await page.title()
                        except Exception:
                            break
                        if not _looks_like_cf_challenge((title or "") + "\n" + (text or "")):
                            break

                final_url = page.url
            finally:
                await browser.close()

            combined = ((title or "") + "\n\n" + (text or ""))[:TEXT_LIMIT]
            return combined, final_url
    except Exception as e:
        log.warning("[crawl] %s -> %s", url[:80], e)
        return None, None


def _clean_name(raw):
    """Очищает ответ GPT: убирает кавычки, точки, приводит к нормальному виду."""
    if not raw:
        return None
    s = raw.strip().strip("\"'`")
    s = re.sub(r"\s+", " ", s)
    s = s.strip(".,;: ")
    if not s:
        return None
    up = s.upper()
    if up in ("UNKNOWN", "WHITEPAGE", "DEAD", "BLOCKED", "N/A", "NONE"):
        return None
    # защита от слишком длинных ответов
    if len(s) > 80:
        return None
    # защита от многословного мусора
    if len(s.split()) > 8:
        return None
    return s


async def _gpt_name(page_text):
    """Отправляет текст в GPT-4o-mini, возвращает очищенное имя оффера или None."""
    if not page_text or len(page_text.strip()) < 30:
        return None

    prompt = f"""Determine the offer name shown on this landing page.

Rules:
- Return a short English name, 2 to 6 words.
- Focus on the PRODUCT/OFFER (e.g. "Global Nordic Invest", "Investing for beginners in Canada", "Quantum AI Trading").
- If the page is cookies/privacy/terms/blocked/404/empty/generic blog with no specific offer — return exactly: UNKNOWN
- Return ONLY the name, nothing else. No quotes, no explanation.

PAGE CONTENT:
{page_text}

Offer name:"""

    try:
        client = _get_openai()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=40,
        )
        raw = resp.choices[0].message.content
        return _clean_name(raw)
    except Exception as e:
        log.warning("[gpt] %s", e)
        return None


async def classify_url(tracking_url):
    """Полный pipeline: URL -> offer_name или None."""
    if not tracking_url:
        return None
    text, final_url = await _fetch_page_text(tracking_url)
    if not text:
        return None
    return await _gpt_name(text)


# ==============================================================
# Bootstrap: классификация всех уникальных URLs в БД
# ==============================================================

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _load_pending_urls():
    """Возвращает {dedup_key: (sample_tracking_url, [item_ids])} — только неклассифицированные."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, tracking_url FROM items "
            "WHERE tracking_url IS NOT NULL AND tracking_url != '' "
            "AND (offer_crawled_at IS NULL)"
        ).fetchall()
    finally:
        conn.close()

    groups = {}
    for item_id, url in rows:
        key = url_dedup_key(url)
        if not key:
            continue
        if key not in groups:
            groups[key] = [url, []]
        groups[key][1].append(item_id)
    return groups


def _persist(item_ids, offer_name):
    conn = _db()
    try:
        now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(
            f"UPDATE items SET offer_name = ?, offer_crawled_at = ? WHERE id IN ({placeholders})",
            [offer_name, now, *item_ids],
        )
        conn.commit()
    finally:
        conn.close()


async def _worker(name, queue, stats):
    while True:
        task = await queue.get()
        if task is None:
            queue.task_done()
            return
        key, url, item_ids = task
        try:
            offer = await classify_url(url)
            _persist(item_ids, offer)
            stats["done"] += 1
            if offer:
                stats["named"] += 1
            log.info(
                "[%s] %d/%d | %s | %s -> %s",
                name, stats["done"], stats["total"], key[:50], url[:60], offer or "UNKNOWN"
            )
        except Exception as e:
            log.error("[%s] failed %s: %s", name, key, e)
        finally:
            queue.task_done()


async def bootstrap_all(workers=3, limit=None):
    """Классифицирует все неклассифицированные URLs. Возвращает stats."""
    groups = _load_pending_urls()
    tasks = [(k, v[0], v[1]) for k, v in groups.items()]
    if limit:
        tasks = tasks[:limit]
    if not tasks:
        log.info("[bootstrap] nothing to classify")
        return {"total": 0, "done": 0, "named": 0}

    stats = {"total": len(tasks), "done": 0, "named": 0}
    log.info("[bootstrap] %d unique URLs (covering %d items) with %d workers",
             len(tasks), sum(len(t[2]) for t in tasks), workers)

    queue = asyncio.Queue()
    for t in tasks:
        await queue.put(t)
    for _ in range(workers):
        await queue.put(None)

    ws = [asyncio.create_task(_worker(f"w{i}", queue, stats)) for i in range(workers)]
    await queue.join()
    await asyncio.gather(*ws, return_exceptions=True)
    return stats


# ==============================================================
# Live: классифицировать одну карточку после save_item
# ==============================================================

# Захватывается при старте web-сервера в app.py (on_startup hook).
# Позволяет тред-безопасно ставить корутины из save_item (который бежит в worker-thread).
MAIN_LOOP = None


def schedule_live(item_id, tracking_url):
    """Тред-безопасный планировщик live-классификации.
    Вызывается из save_item (может быть в любом потоке)."""
    if MAIN_LOOP is None:
        log.warning("[offer] MAIN_LOOP not set — skipping item=%s", item_id)
        return
    asyncio.run_coroutine_threadsafe(
        classify_item_live(item_id, tracking_url),
        MAIN_LOOP,
    )


async def classify_item_live(item_id, tracking_url):
    """Вызывается асинхронно после save_item. Сохраняет результат в БД."""
    offer = await classify_url(tracking_url)
    _persist([item_id], offer)
    log.info("[live] item=%d -> %s", item_id, offer or "UNKNOWN")
    return offer


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        workers = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        stats = asyncio.run(bootstrap_all(workers=workers, limit=limit))
        print(f"\nDone: {stats['done']}/{stats['total']} crawled, {stats['named']} named")
    elif len(sys.argv) > 2 and sys.argv[1] == "classify":
        url = sys.argv[2]
        result = asyncio.run(classify_url(url))
        print(f"Offer: {result or 'UNKNOWN'}")
    else:
        print("Usage:")
        print("  python3 offer_classifier.py bootstrap [limit] [workers]")
        print("  python3 offer_classifier.py classify <url>")
