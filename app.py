"""
Telegram-бот: Видео-переводчик + Facebook-скачиватель

Функции:
  1. Отправляете видео → бот транскрибирует, переводит, шлёт видео с переводом в caption
  2. Отправляете ссылку Facebook → бот скачивает через fdown.net, транскрибирует,
     переводит, шлёт видео с переводом + парсит трекинг-ссылку (LINK, 4sub, 5sub, pix)

УСТАНОВКА:
    pip install python-telegram-bot openai deep-translator requests beautifulsoup4 cloudscraper
    apt install ffmpeg
"""

import os, re, asyncio, logging, tempfile, subprocess, time, uuid, sqlite3, hashlib, threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

# Buyer fingerprint + Similar engine
try:
    from buyer_fingerprint import classify as classify_buyer, detect_agency
    from similar_engine import SimilarEngine, CardData, RELATIONSHIP_LABELS
    from unique_filter import UniqueIndex, compute_content_hash as new_content_hash
    from creative_lifespan import LifespanEngine
    import obsidian_sync
    import offer_classifier
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from buyer_fingerprint import classify as classify_buyer, detect_agency
    from similar_engine import SimilarEngine, CardData, RELATIONSHIP_LABELS
    from unique_filter import UniqueIndex, compute_content_hash as new_content_hash
    from creative_lifespan import LifespanEngine
    import obsidian_sync
    import offer_classifier

# Global engines
_similar_engine = None
_unique_index = None
_lifespan_engine = None

import requests as http_requests
import cloudscraper
from bs4 import BeautifulSoup
from aiohttp import web

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
)
from openai import OpenAI
from deep_translator import GoogleTranslator

# =====================================================
#  ВСТАВЬТЕ СВОИ КЛЮЧИ
# =====================================================

TELEGRAM_BOT_TOKEN = "REDACTED_SPY_BOT_TOKEN"
TRANSLATOR_BOT_TOKEN = "REDACTED_TRANSLATOR_BOT_TOKEN"  # Translator-only: не сохраняет в БД  # Translator-only: не сохраняет в БД

def is_translator_bot(update):
    """True если сообщение пришло во второй (translator-only) бот."""
    try:
        return update.get_bot().token == TRANSLATOR_BOT_TOKEN
    except Exception:
        return False
OPENAI_API_KEY     = "REDACTED_OPENAI_API_KEY"

# =====================================================

WORK_DIR = Path(tempfile.gettempdir()) / "translate_bot"
WORK_DIR.mkdir(exist_ok=True)

TELEGRAM_FILE_LIMIT = 2000 * 1024 * 1024  # 2 GB с Local Bot API
MIN_VIDEO_SIZE = 10 * 1024  # 10 KB
MAX_LINKS_PER_MSG = 10  # Максимум ссылок из одного сообщения

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# =====================================================
#  SQLite — хранение обработанных элементов
# =====================================================

DB_PATH = Path(__file__).parent / "spy_data.db"
VIDEOS_DIR = Path(__file__).parent / "videos"
THUMBS_DIR = Path(__file__).parent / "thumbs"
VIDEOS_DIR.mkdir(exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)

def _db_connect():
    """Создаёт соединение с WAL-режимом для безопасного параллельного доступа."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fb_url TEXT,
                translation TEXT,
                original_text TEXT,
                tracking_domain TEXT,
                sub4 TEXT,
                sub5 TEXT,
                pix TEXT
            )
        """)
        for col, coltype in [
            ("video_filename", "TEXT"),
            ("thumb_filename", "TEXT"),
            ("geo", "TEXT"),
            ("tracking_url", "TEXT"),
            ("content_hash", "TEXT"),
            ("uid", "TEXT"),
            ("buyer_id", "INTEGER"),
            ("offer_name", "TEXT"),
            ("offer_crawled_at", "TEXT"),
            ("bot_source", "TEXT DEFAULT 'spy'"),
        ]:
            try:
                conn.execute("ALTER TABLE items ADD COLUMN %s %s" % (col, coltype))
            except sqlite3.OperationalError:
                pass
        # Backfill: any pre-migration row gets bot_source='spy'
        conn.execute("UPDATE items SET bot_source='spy' WHERE bot_source IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_domain ON items(tracking_domain)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_hash ON items(content_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_bot_source ON items(bot_source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_offer ON items(offer_name)")
        conn.commit()
        # Rehash content_hash — только для строк где hash отсутствует или некорректной длины.
        # MD5 hex даёт ровно 32 символа; всё остальное — индикатор старого/битого хэша.
        rows = conn.execute(
            "SELECT id, translation FROM items "
            "WHERE translation IS NOT NULL AND (content_hash IS NULL OR LENGTH(content_hash) != 32)"
        ).fetchall()
        rehashed = 0
        for row_id, text in rows:
            h = compute_content_hash(text)
            conn.execute("UPDATE items SET content_hash=? WHERE id=?", (h, row_id))
            rehashed += 1
        if rehashed:
            conn.commit()
            log.info("[DB] Rehash content_hash: %d строк", rehashed)
        # Backfill UUID
        rows2 = conn.execute("SELECT id FROM items WHERE uid IS NULL").fetchall()
        if rows2:
            for (row_id,) in rows2:
                conn.execute("UPDATE items SET uid=? WHERE id=?", (uuid.uuid4().hex[:12], row_id))
            conn.commit()
            log.info("[DB] Backfill uid: %d строк", len(rows2))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uid ON items(uid)")
    finally:
        conn.close()
    log.info("[DB] База данных инициализирована: %s", DB_PATH)


def compute_content_hash(text):
    """MD5 первых 20 слов (normalized, без пунктуации) — через unique_filter."""
    if not text:
        return None
    return new_content_hash(text) or None

init_db()


# =====================================================
#  BUYER DETECTION — кластеризация по 4sub паттернам
# =====================================================

def init_buyers():
    """Кластеризация buyers через buyer_fingerprint.classify()."""
    from collections import defaultdict, Counter

    conn = _db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS buyers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prefix TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES items(id)
            )
        """)
        try:
            conn.execute("ALTER TABLE items ADD COLUMN buyer_id INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()

        # 1. Classify all sub4s
        rows = conn.execute(
            "SELECT id, sub4, tracking_domain, pix FROM items "
            "WHERE sub4 IS NOT NULL AND sub4 != 'unknown' AND LENGTH(sub4) > 3"
        ).fetchall()

        item_cluster = {}  # item_id -> cluster_id
        cluster_count = Counter()

        for item_id, sub4, domain, pix in rows:
            r = classify_buyer(sub4)
            cid = r['cluster']
            if cid and not cid.startswith('META_'):
                item_cluster[item_id] = cid
                cluster_count[cid] += 1

        # 2. Дополнительно: Union-Find по доменам/пикселям для items без cluster
        # Если item без cluster делит домен/пиксель с item у которого есть cluster — присоединяем
        domain_to_cluster = {}
        pix_to_cluster = {}
        for item_id, sub4, domain, pix_val in rows:
            cid = item_cluster.get(item_id)
            if not cid:
                continue
            if domain and domain not in domain_to_cluster:
                domain_to_cluster[domain] = cid
            if pix_val and pix_val != 'unknown' and pix_val not in pix_to_cluster:
                pix_to_cluster[pix_val] = cid

        # Assign unclassified items by domain/pixel
        for item_id, sub4, domain, pix_val in rows:
            if item_id in item_cluster:
                continue
            if domain and domain in domain_to_cluster:
                item_cluster[item_id] = domain_to_cluster[domain]
            elif pix_val and pix_val != 'unknown' and pix_val in pix_to_cluster:
                item_cluster[item_id] = pix_to_cluster[pix_val]

        # 3. Create/update buyers table
        all_clusters = set(item_cluster.values())
        inserted = 0
        cluster_buyer_map = {}
        for cid in all_clusters:
            existing = conn.execute("SELECT id FROM buyers WHERE prefix=?", (cid,)).fetchone()
            if not existing:
                buyer_name = cid.replace('_', ' ').title()
                conn.execute("INSERT OR IGNORE INTO buyers (name, prefix) VALUES (?, ?)",
                             (buyer_name, cid))
                conn.commit()
                inserted += 1
                existing = conn.execute("SELECT id FROM buyers WHERE prefix=?", (cid,)).fetchone()
            if existing:
                cluster_buyer_map[cid] = existing[0]

        # 4. Assign buyer_id to all items
        conn.execute("UPDATE items SET buyer_id = NULL")
        updated = 0
        for item_id, cid in item_cluster.items():
            bid = cluster_buyer_map.get(cid)
            if bid:
                conn.execute("UPDATE items SET buyer_id=? WHERE id=?", (bid, item_id))
                updated += 1
        conn.commit()

        log.info("[BUYERS] Кластеров: %d, новых: %d, привязано: %d/%d items",
                 len(all_clusters), inserted, updated, len(rows))

    finally:
        conn.close()

init_buyers()


def init_similar_engine():
    """Загружает все items в SimilarEngine при старте."""
    global _similar_engine
    _similar_engine = SimilarEngine()
    conn = _db_connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM items WHERE translation IS NOT NULL").fetchall()
        for r in rows:
            _similar_engine.add_card(CardData(
                id=str(r['id']),
                content_hash=r['content_hash'],
                tracking_domain=r['tracking_domain'],
                sub4=r['sub4'],
                sub5=r['sub5'],
                pixel=r['pix'],
                created_at=r['created_at'],
                source=r['bot_source'] if 'bot_source' in r.keys() else 'spy',
            ))
        log.info("[SIMILAR] Индекс загружен: %d items", len(rows))
    finally:
        conn.close()

init_similar_engine()


def init_unique_index():
    """Загружает UniqueIndex при старте."""
    global _unique_index
    _unique_index = UniqueIndex()
    conn = _db_connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, content_hash, created_at, bot_source FROM items").fetchall()
        items = [{'id': str(r['id']), 'content_hash': r['content_hash'] or '', 'created_at': r['created_at'], 'source': (r['bot_source'] if 'bot_source' in r.keys() else 'spy')} for r in rows]
        _unique_index.build(items)
        stats = _unique_index.get_stats()
        log.info("[UNIQUE] Index: %d items, %d unique, %d dupes (%.1f%%)",
                 stats['total_items'], stats['after_normal_filter'],
                 stats['total_copies'], stats['duplicate_rate'])
    finally:
        conn.close()

init_unique_index()


def init_lifespan_engine():
    """Загружает LifespanEngine при старте."""
    global _lifespan_engine
    _lifespan_engine = LifespanEngine()
    conn = _db_connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM items WHERE translation IS NOT NULL").fetchall()
        items = []
        for r in rows:
            date_str = (r['created_at'] or '').split(' ')[0] if r['created_at'] else ''
            items.append({
                'id': str(r['id']),
                'content_hash': r['content_hash'] or '',
                'date': date_str,
                'tracking_domain': r['tracking_domain'] or '',
                'sub4': r['sub4'] or '',
                'sub5': r['sub5'] or '',
                'pix': r['pix'] or '',
                'geo': r['geo'] or '',
                'transcription': (r['translation'] or '')[:200],
                'source': r['bot_source'] if 'bot_source' in r.keys() else 'spy',
            })
        _lifespan_engine.ingest(items)
        stats = _lifespan_engine.get_stats()
        log.info("[LIFESPAN] %d creatives, avg %.1fd, max %dd",
                 stats['total_creatives'], stats['avg_lifespan'], stats['max_lifespan'])
    finally:
        conn.close()

init_lifespan_engine()


# Lock для защиты shared engine state от race condition между worker-thread (save_item)
# и event-loop thread (web API). Используется в add_to_similar_index + при чтениях из API.
_engines_lock = threading.RLock()


def add_to_similar_index(item_id, content_hash=None, tracking_domain=None, sub4=None, sub5=None, pix=None, bot_source='spy'):
    """Добавляет новый item в similar + unique + lifespan index. Source-aware. Thread-safe."""
    global _similar_engine, _unique_index, _lifespan_engine
    with _engines_lock:
        if _similar_engine:
            _similar_engine.add_card(CardData(
                id=str(item_id),
                content_hash=content_hash,
                tracking_domain=tracking_domain,
                sub4=sub4,
                sub5=sub5,
                pixel=pix,
                source=bot_source,
            ))
        if _unique_index:
            _unique_index.add_item({
                'id': str(item_id),
                'content_hash': content_hash or '',
                'source': bot_source,
            })
        if _lifespan_engine:
            from datetime import datetime
            _lifespan_engine.add_item({
                'id': str(item_id),
                'content_hash': content_hash or '',
                'date': datetime.now().strftime('%Y-%m-%d'),
                'tracking_domain': tracking_domain or '',
                'sub4': sub4 or '',
                'sub5': sub5 or '',
                'pix': pix or '',
                'source': bot_source,
            })

    # Sync to Obsidian vault (async, non-blocking) — try/finally чтобы не лить connections
    conn_sync = None
    try:
        conn_sync = _db_connect()
        conn_sync.row_factory = sqlite3.Row
        item_row = conn_sync.execute(
            "SELECT items.*, buyers.name as buyer_name FROM items "
            "LEFT JOIN buyers ON items.buyer_id = buyers.id WHERE items.id=?",
            (item_id,)
        ).fetchone()
        if item_row:
            obsidian_sync.sync_item(dict(item_row))
    except Exception as e:
        log.warning("[OBSIDIAN] sync failed for item %d: %s", item_id, e)
    finally:
        if conn_sync is not None:
            try:
                conn_sync.close()
            except Exception:
                pass


def assign_buyer_to_item(sub4, tracking_domain=None, pix=None):
    """Определяет buyer_id для нового item через classify + domain/pixel fallback."""
    if not sub4 or sub4 == 'unknown':
        return None

    r = classify_buyer(sub4)
    cid = r['cluster']

    conn = _db_connect()
    try:
        # 1. Classify match
        if cid and not cid.startswith('META_'):
            row = conn.execute("SELECT id FROM buyers WHERE prefix=?", (cid,)).fetchone()
            if row:
                return row[0]
            # Create new buyer for this cluster
            buyer_name = cid.replace('_', ' ').title()
            try:
                conn.execute("INSERT INTO buyers (name, prefix) VALUES (?, ?)", (buyer_name, cid))
                conn.commit()
                row = conn.execute("SELECT id FROM buyers WHERE prefix=?", (cid,)).fetchone()
                if row:
                    log.info("[BUYER] Новый: %s (id=%d)", cid, row[0])
                    return row[0]
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT id FROM buyers WHERE prefix=?", (cid,)).fetchone()
                if row:
                    return row[0]

        # 2. Domain fallback
        if tracking_domain:
            row = conn.execute(
                "SELECT buyer_id FROM items WHERE tracking_domain=? AND buyer_id IS NOT NULL LIMIT 1",
                (tracking_domain,)
            ).fetchone()
            if row and row[0]:
                return row[0]

        # 3. Pixel fallback
        if pix and pix != 'unknown':
            row = conn.execute(
                "SELECT buyer_id FROM items WHERE pix=? AND buyer_id IS NOT NULL LIMIT 1",
                (pix,)
            ).fetchone()
            if row and row[0]:
                return row[0]

        return None
    finally:
        conn.close()


def save_item(translation, original_text=None, fb_url=None,
              tracking_domain=None, sub4=None, sub5=None, pix=None, geo=None, tracking_url=None,
              bot_source='spy'):
    """Возвращает ID вставленной строки или None."""
    conn = None
    try:
        conn = _db_connect()
        c_hash = compute_content_hash(translation)
        b_id = assign_buyer_to_item(sub4, tracking_domain=tracking_domain, pix=pix)
        item_uid = uuid.uuid4().hex[:12]
        cursor = conn.execute(
            "INSERT INTO items (fb_url, translation, original_text, tracking_domain, sub4, sub5, pix, geo, tracking_url, content_hash, buyer_id, uid, bot_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fb_url, translation, original_text, tracking_domain, sub4, sub5, pix, geo, tracking_url, c_hash, b_id, item_uid, bot_source),
        )
        conn.commit()
        row_id = cursor.lastrowid
        log.info("[DB] Элемент сохранён (id=%d, source=%s)", row_id, bot_source)
        add_to_similar_index(row_id, c_hash, tracking_domain, sub4, sub5, pix, bot_source=bot_source)
        # Live-классификация только для SPY (translator не имеет смысла)
        if bot_source == 'spy' and tracking_url:
            try:
                offer_classifier.schedule_live(row_id, tracking_url)
            except Exception as _e:
                log.warning("[offer] schedule failed: %s", _e)
        return row_id
    except Exception as e:
        log.error("[DB] Ошибка сохранения: %s", e)
        return None
    finally:
        if conn:
            conn.close()


def update_item_media(item_id, video_filename, thumb_filename):
    conn = None
    try:
        conn = _db_connect()
        conn.execute("UPDATE items SET video_filename=?, thumb_filename=? WHERE id=?",
                     (video_filename, thumb_filename, item_id))
        conn.commit()
    except Exception as e:
        log.error("[DB] Ошибка обновления медиа: %s", e)
    finally:
        if conn:
            conn.close()


def save_video_and_thumb(item_id, video_path):
    """Копирует видео и генерирует превью."""
    import shutil
    video_fn = "%d.mp4" % item_id
    thumb_fn = "%d.jpg" % item_id
    dst_video = str(VIDEOS_DIR / video_fn)
    dst_thumb = str(THUMBS_DIR / thumb_fn)
    try:
        shutil.copy2(video_path, dst_video)
        subprocess.run(
            ["ffmpeg", "-y", "-i", dst_video, "-ss", "00:00:02",
             "-vframes", "1", "-vf", "scale=320:-1", dst_thumb],
            capture_output=True, timeout=30)
        if not os.path.exists(dst_thumb) or os.path.getsize(dst_thumb) == 0:
            subprocess.run(
                ["ffmpeg", "-y", "-i", dst_video,
                 "-vframes", "1", "-vf", "scale=320:-1", dst_thumb],
                capture_output=True, timeout=30)
        update_item_media(item_id, video_fn, thumb_fn)
        log.info("[MEDIA] video=%s, thumb=%s", video_fn, thumb_fn)
    except Exception as e:
        log.error("[MEDIA] Ошибка: %s", e)

# [FIX #2] Регулярки — убираем хвостовую пунктуацию из URL
FB_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.|mbasic\.)?(?:facebook\.com|fb\.watch|fb\.com)/[^\s,;)\]}>\"\']+',
)
NON_FB_URL_PATTERN = re.compile(
    r'https?://(?!(?:www\.|m\.|web\.|mbasic\.)?(?:facebook\.com|fb\.watch|fb\.com))[^\s,;)\]}>\"\']+',
)


# ══════════════════════════════════════════════════════
# СКАЧИВАНИЕ ВИДЕО С FACEBOOK
# Порядок: fdown.net → getfvid.io → snapsave.app
# ══════════════════════════════════════════════════════

DOWNLOADERS = ["fdown", "getfvid", "snapsave"]


def download_fb_video(fb_url):
    # type: (str) -> Optional[str]
    """Пробует скачать видео через несколько сайтов по очереди."""
    for name in DOWNLOADERS:
        try:
            func = {"fdown": _try_fdown, "getfvid": _try_getfvid, "snapsave": _try_snapsave}[name]
            result = func(fb_url)
            if result:
                return result
        except Exception as e:
            log.warning("[%s] %s", name.upper(), e)
    log.error("[DOWNLOAD] Все загрузчики не смогли: %s", fb_url[:80])
    return None


def _download_and_validate(video_url, tag):
    # type: (str, str) -> Optional[str]
    """Скачивает видео по прямой ссылке и проверяет."""
    unique_id = uuid.uuid4().hex[:12]
    filepath = str(WORK_DIR / ("fb_%s.mp4" % unique_id))
    try:
        resp = http_requests.get(
            video_url, stream=True, timeout=180,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        )
        try:
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        f.write(chunk)
        finally:
            resp.close()

        size = os.path.getsize(filepath)
        if size < MIN_VIDEO_SIZE:
            log.warning("[%s] Файл слишком мал (%.1f KB)", tag, size / 1024)
            os.remove(filepath)
            return None

        with open(filepath, "rb") as f:
            header = f.read(32)
        if b"<html" in header.lower() or b"<!doctype" in header.lower():
            log.warning("[%s] Файл HTML, не видео", tag)
            os.remove(filepath)
            return None

        log.info("[%s] ✅ %.1f MB", tag, size / 1024 / 1024)
        return filepath
    except Exception as e:
        log.warning("[%s] Ошибка скачивания: %s", tag, e)
        if os.path.exists(filepath):
            os.remove(filepath)
        return None


def _extract_fbcdn_links(soup):
    # type: (BeautifulSoup) -> dict
    """Извлекает ссылки fbcdn/scontent из HTML."""
    links = {"hd": None, "sd": None}
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True).lower()
        if not href.startswith("http"):
            continue
        if "fbcdn" in href or "scontent" in href or "video" in href:
            if "hd" in text or "high" in text:
                links["hd"] = href
            elif "sd" in text or "normal" in text or "low" in text:
                links["sd"] = href
            elif not links["sd"]:
                links["sd"] = href
    return links


# ── fdown.net ──

def _try_fdown(fb_url):
    # type: (str) -> Optional[str]
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    try:
        scraper.get("https://fdown.net/", timeout=15)
        resp = scraper.post("https://fdown.net/download.php", data={"URLz": fb_url}, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        links = {"hd": None, "sd": None}

        hd_btn = soup.find("a", {"id": "hdlink"})
        if hd_btn and hd_btn.get("href", "").startswith("http"):
            links["hd"] = hd_btn["href"]

        sd_btn = soup.find("a", {"id": "sdlink"})
        if sd_btn and sd_btn.get("href", "").startswith("http"):
            links["sd"] = sd_btn["href"]

        if not links["hd"] and not links["sd"]:
            links = _extract_fbcdn_links(soup)

        video_url = links.get("hd") or links.get("sd")
        if not video_url:
            log.warning("[FDOWN] Нет ссылки")
            return None

        log.info("[FDOWN] Скачиваю (%s)...", "HD" if links.get("hd") else "SD")
        return _download_and_validate(video_url, "FDOWN")
    finally:
        scraper.close()


# ── getfvid.io ──

def _try_getfvid(fb_url):
    # type: (str) -> Optional[str]
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    try:
        scraper.get("https://getfvid.io/", timeout=15)
        resp = scraper.post(
            "https://getfvid.io/api/convert",
            data={"url": fb_url},
            timeout=30,
        )
        resp.raise_for_status()

        # Пробуем JSON ответ
        try:
            data = resp.json()
            video_url = None
            if isinstance(data, dict):
                video_url = data.get("hd") or data.get("sd") or data.get("url") or data.get("link")
                if not video_url and "links" in data:
                    lnk = data["links"]
                    if isinstance(lnk, dict):
                        video_url = lnk.get("hd") or lnk.get("sd") or lnk.get("Download in HD Quality") or lnk.get("Download in Normal Quality")
        except (ValueError, KeyError):
            video_url = None
        if video_url:
            log.info("[GETFVID] Скачиваю...")
            result = _download_and_validate(video_url, "GETFVID")
            if result:
                return result

        # Пробуем HTML
        soup = BeautifulSoup(resp.text, "html.parser")
        links = _extract_fbcdn_links(soup)

        # Поиск по download кнопкам
        if not links["hd"] and not links["sd"]:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if href.startswith("http") and ("fbcdn" in href or "scontent" in href):
                    links["sd"] = href
                    break

        video_url = links.get("hd") or links.get("sd")
        if not video_url:
            log.warning("[GETFVID] Нет ссылки")
            return None

        log.info("[GETFVID] Скачиваю...")
        return _download_and_validate(video_url, "GETFVID")
    finally:
        scraper.close()


# ── snapsave.app ──

def _try_snapsave(fb_url):
    # type: (str) -> Optional[str]
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    try:
        scraper.get("https://snapsave.app/", timeout=15)
        resp = scraper.post(
            "https://snapsave.app/action.php",
            data={"url": fb_url},
            timeout=30,
        )
        resp.raise_for_status()

        # Пробуем JSON
        try:
            data = resp.json()
            video_url = None
            if isinstance(data, dict):
                video_url = data.get("hd") or data.get("sd") or data.get("url")
                if not video_url and "data" in data:
                    inner = data["data"]
                    if isinstance(inner, list) and inner:
                        video_url = inner[0].get("url") if isinstance(inner[0], dict) else None
                    elif isinstance(inner, dict):
                        video_url = inner.get("hd") or inner.get("sd") or inner.get("url")
        except (ValueError, KeyError):
            video_url = None
        if video_url:
            log.info("[SNAPSAVE] Скачиваю...")
            result = _download_and_validate(video_url, "SNAPSAVE")
            if result:
                return result

        # HTML fallback
        soup = BeautifulSoup(resp.text, "html.parser")
        links = _extract_fbcdn_links(soup)
        if not links["hd"] and not links["sd"]:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if href.startswith("http") and ("fbcdn" in href or "scontent" in href):
                    links["sd"] = href
                    break

        video_url = links.get("hd") or links.get("sd")
        if not video_url:
            log.warning("[SNAPSAVE] Нет ссылки")
            return None

        log.info("[SNAPSAVE] Скачиваю...")
        return _download_and_validate(video_url, "SNAPSAVE")
    finally:
        scraper.close()


# ══════════════════════════════════════════════════════
# ПАРСИНГ ТРЕКИНГ-ССЫЛКИ ЧЕРЕЗ ChatGPT
# ══════════════════════════════════════════════════════

def parse_tracking_url(url):
    # type: (str) -> Optional[dict]
    """
    Отправляет трекинг-ссылку в ChatGPT для точного определения:
    - LINK (домен)
    - 4sub (название рекламной кампании)
    - 5sub (название креатива)
    - pix (ID pixel)

    Возвращает dict: {domain, sub4, sub5, pix, formatted}
    """
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        if not domain:
            return None

        prompt = (
            "Проанализируй эту трекинг-ссылку и извлеки данные.\n\n"
            "URL: {}\n\n"
            "Определи:\n"
            "1. 4sub — название рекламной кампании. Ищи в параметрах: campaign_id, utm_campaign, campaign_name, subid. Не путай с ad_id. Верни значение как есть.\n"
            "2. 5sub — название креатива. Ищи в параметрах: cr, creative_id, ad_name, creo, utm_creative. Если в utm_content есть двоеточие \":\", возьми текст после него. bu — запасной вариант.\n"
            "3. pix — ID пикселя (числовой, 10-17 цифр). Ищи в параметрах: pixel, pix, fbpx, fb_pixel, tt_pixel, gpixel.\n\n"
            "Ответь СТРОГО в формате (без пояснений, без кавычек):\n"
            "4sub: значение\n"
            "5sub: значение\n"
            "pix: значение\n\n"
            "Если параметр не найден, напиши: unknown"
        ).format(url)

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0,
        )

        answer = response.choices[0].message.content.strip()
        log.info("[GPT PARSE] %s", answer)

        sub4 = "unknown"
        sub5 = "unknown"
        pix = "unknown"

        for line in answer.split("\n"):
            line = line.strip()
            if line.lower().startswith("4sub:"):
                sub4 = line.split(":", 1)[1].strip()
            elif line.lower().startswith("5sub:"):
                sub5 = line.split(":", 1)[1].strip()
            elif line.lower().startswith("pix:"):
                pix = line.split(":", 1)[1].strip()

        return {
            "domain": domain, "sub4": sub4, "sub5": sub5, "pix": pix, "url": url,
            "formatted": "LINK - %s , 4sub - %s\n5sub - %s , pix - %s" % (domain, sub4, sub5, pix),
        }

    except Exception as e:
        log.warning("[GPT PARSE] Ошибка: %s, пробую регулярки...", e)
        return _parse_tracking_url_fallback(url)


def _parse_tracking_url_fallback(url):
    # type: (str) -> Optional[dict]
    """Запасной парсер на регулярках если ChatGPT недоступен."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        domain = parsed.hostname or ""
        if not domain:
            return None

        sub4 = "unknown"
        for key in ["campaign_id", "utm_campaign", "campaign_name", "subid"]:
            if key in params and params[key][0]:
                sub4 = params[key][0]
                break

        sub5 = "unknown"
        for key in ["cr", "creative_id", "ad_name", "creo", "utm_creative"]:
            if key in params and params[key][0]:
                sub5 = params[key][0]
                break
        if sub5 == "unknown" and "utm_content" in params:
            val = params["utm_content"][0]
            if ":" in val:
                sub5 = val.split(":")[-1]
        if sub5 == "unknown" and "bu" in params and params["bu"][0]:
            sub5 = params["bu"][0]

        pix = "unknown"
        m = re.search(r'(?:\?|&)(?:pixel|pix|fbpx|fb_pixel|tt_pixel|gpixel)=([0-9]{10,17})', url)
        if m:
            pix = m.group(1)

        return {
            "domain": domain, "sub4": sub4, "sub5": sub5, "pix": pix, "url": url,
            "formatted": "LINK - %s , 4sub - %s\n5sub - %s , pix - %s" % (domain, sub4, sub5, pix),
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════
# АУДИО / ТРАНСКРИБЦИЯ / ПЕРЕВОД
# ══════════════════════════════════════════════════════

def extract_audio(video_path):
    # type: (str) -> Optional[str]
    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-vn", "-acodec", "libmp3lame", "-ab", "64k", "-ar", "16000",
             audio_path],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            log.error("[FFMPEG] %s", result.stderr[:500])
            if os.path.exists(audio_path):
                os.remove(audio_path)
            return None
        size = Path(audio_path).stat().st_size
        if size == 0:
            log.error("[FFMPEG] Аудио пустое")
            os.remove(audio_path)
            return None
        log.info("[FFMPEG] ✅ Аудио: %.1f MB", size / 1024 / 1024)
        return audio_path
    except subprocess.TimeoutExpired:
        log.error("[FFMPEG] Таймаут извлечения аудио")
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return None
    except Exception as e:
        log.error("[FFMPEG] %s", e)
        if os.path.exists(audio_path):
            os.remove(audio_path)
        return None


def transcribe_audio(audio_path):
    # type: (str) -> Optional[str]
    file_size = Path(audio_path).stat().st_size

    whisper_prompt = (
        "Transcribe the speech accurately, preserving punctuation, "
        "proper nouns, and natural sentence boundaries."
    )

    if file_size <= 24 * 1024 * 1024:
        try:
            with open(audio_path, "rb") as f:
                result = openai_client.audio.transcriptions.create(
                    model="whisper-1", file=f, response_format="text",
                    prompt=whisper_prompt,
                )
            # [FIX #6] Проверяем что Whisper вернул непустой результат
            if not result or not result.strip():
                log.warning("[WHISPER] Пустая транскрибция")
                return None
            log.info("[WHISPER] ✅ %d символов", len(result))
            return result
        except Exception as e:
            log.error("[WHISPER] %s", e)
            return None

    log.info("[WHISPER] Большой файл (%.1f MB), разбиваю...", file_size / 1024 / 1024)
    # [FIX #7] Уникальный dir для избежания коллизий при параллельных задачах
    parts_dir = Path(audio_path).parent / ("parts_%s" % uuid.uuid4().hex[:8])
    parts_dir.mkdir(exist_ok=True)

    try:
        seg_result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-f", "segment", "-segment_time", "600",
             "-acodec", "libmp3lame", "-ab", "64k", "-ar", "16000",
             str(parts_dir / "part_%03d.mp3")],
            capture_output=True, text=True, timeout=600,
        )
        if seg_result.returncode != 0:
            log.error("[FFMPEG SPLIT] %s", seg_result.stderr[:500])

        texts = []
        for pf in sorted(parts_dir.glob("part_*.mp3")):
            try:
                with open(pf, "rb") as f:
                    part_result = openai_client.audio.transcriptions.create(
                        model="whisper-1", file=f, response_format="text",
                        prompt=whisper_prompt,
                    )
                if part_result and part_result.strip():
                    texts.append(part_result)
            except Exception as e:
                log.error("[WHISPER] %s: %s", pf.name, e)
            finally:
                pf.unlink(missing_ok=True)
    finally:
        # [FIX #8] Гарантируем очистку папки с частями
        for leftover in parts_dir.glob("*"):
            leftover.unlink(missing_ok=True)
        try:
            parts_dir.rmdir()
        except Exception:
            pass

    return "\n".join(texts) if texts else None


def translate_to_russian(text):
    # type: (str) -> str
    """Переводит текст на русский через GPT-4o-mini."""
    if not text or not text.strip():
        return ""
    try:
        chunks = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= 8000:
                chunks.append(remaining)
                break
            sp = remaining.rfind('. ', 0, 8000)
            if sp == -1:
                sp = remaining.rfind(' ', 0, 8000)
            if sp == -1:
                sp = 8000
            chunks.append(remaining[:sp + 1])
            remaining = remaining[sp + 1:]

        translated_parts = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content":
                        "Ты профессиональный переводчик. Переведи текст на русский язык. "
                        "Требования: точный смысловой перевод, естественный русский язык, "
                        "сохраняй имена собственные и бренды как есть, "
                        "не добавляй пояснений — только перевод."},
                    {"role": "user", "content": chunk.strip()},
                ],
                max_tokens=4000,
                temperature=0.3,
            )
            result = response.choices[0].message.content.strip()
            if result:
                translated_parts.append(result)

        translation = " ".join(translated_parts)
        log.info("[GPT TRANSLATE] ✅ %d → %d символов", len(text), len(translation))
        return translation

    except Exception as e:
        log.error("[GPT TRANSLATE] Ошибка: %s, пробую Google Translate...", e)
        return _translate_fallback(text)


def _translate_fallback(text):
    # type: (str) -> str
    """Запасной перевод через Google Translate."""
    try:
        translator = GoogleTranslator(source='auto', target='ru')
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= 4500:
                chunks.append(remaining)
                break
            sp = remaining.rfind('. ', 0, 4500)
            if sp == -1:
                sp = remaining.rfind(' ', 0, 4500)
            if sp == -1:
                sp = 4500
            chunks.append(remaining[:sp + 1])
            remaining = remaining[sp + 1:]
        translated = []
        for c in chunks:
            if not c.strip():
                continue
            try:
                translated.append(translator.translate(c.strip()))
            except Exception as e:
                log.warning("[TRANSLATE FALLBACK] Чанк пропущен: %s", e)
        return " ".join(translated)
    except Exception as e:
        log.error("[TRANSLATE FALLBACK] %s", e)
        return ""


def cleanup(*paths):
    for p in paths:
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


# [FIX #9] Периодическая очистка старых tmp-файлов (>1 час)
def cleanup_old_temp_files():
    try:
        now = time.time()
        # Очистка tmp (>1 час)
        for f in WORK_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime) > 3600:
                f.unlink(missing_ok=True)
                log.info("[CLEANUP] Удалён tmp: %s", f.name)
        # Очистка только видео (>180 дней). Thumbnails не трогаем — они копеечные.
        video_retention = 180 * 24 * 3600
        for f in VIDEOS_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime) > video_retention:
                f.unlink(missing_ok=True)
                log.info("[CLEANUP] Удалён старый видео: %s", f.name)
    except Exception as e:
        log.warning("[CLEANUP] %s", e)


# ══════════════════════════════════════════════════════
# ПОЛНЫЙ ПАЙПЛАЙН: видео → аудио → текст → перевод
# ══════════════════════════════════════════════════════

async def transcribe_and_translate(video_path):
    # type: (str) -> Optional[tuple]
    """Возвращает (original_transcript, translation, geo) или None."""
    audio_path = None
    try:
        audio_path = await asyncio.to_thread(extract_audio, video_path)
        if not audio_path:
            return None

        transcript = await asyncio.to_thread(transcribe_audio, audio_path)
        if not transcript:
            return None

        translation = await asyncio.to_thread(translate_to_russian, transcript)
        if not translation:
            return None

        geo = await asyncio.to_thread(detect_geo, transcript)
        return (transcript, translation, geo)
    finally:
        cleanup(audio_path)


def detect_geo(transcript):
    """Определяет целевое гео по транскрипции через GPT."""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content":
                    "You analyze ad video transcriptions translated to Russian. "
                    "Determine the TARGET COUNTRY of the ad (where the ad audience lives). "
                    "The text is ALWAYS in Russian because it was translated — this does NOT mean the target is Russia. "
                    "Look for: currency (£=GB, €=DE/FR/IT, ₹/lakh/crore=IN, $=CA/AU), "
                    "politician/celebrity names, institutions, country mentions. "
                    "NEVER reply RU. Reply with ONLY the ISO 2-letter country code. "
                    "Common targets: GB, IN, CA, DE, FR, IT, PL, AU, ES, TR. If unclear: XX"},
                {"role": "user", "content": transcript[:2000]},
            ],
            max_tokens=5,
            temperature=0,
        )
        geo = response.choices[0].message.content.strip().upper()[:2]
        if len(geo) == 2 and geo.isalpha() and geo != 'RU':
            log.info("[GEO] %s", geo)
            return geo
        return "XX"
    except Exception as e:
        log.warning("[GEO] Ошибка: %s", e)
        return "XX"


# ══════════════════════════════════════════════════════
# TELEGRAM — ОБРАБОТЧИКИ
# ══════════════════════════════════════════════════════

async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 Привет! Я бот-переводчик видео.\n\n"
        "📌 Что я умею:\n"
        "1. Отправьте видео — получите его с переводом\n"
        "2. Отправьте ссылку Facebook — скачаю видео, переведу\n\n"
        "Если в сообщении есть трекинг-ссылка — распарсю LINK, 4sub, 5sub, pix."
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "📎 Видео — отправьте файл, получите перевод\n"
        "🔗 Ссылка Facebook — скачаю + переведу\n"
        "📊 Трекинг-ссылка — распарсю параметры"
    )


# ── Обработка видео-файла ──

async def handle_video(update, context):
    if update.message.video:
        file_obj = update.message.video
    elif update.message.document:
        file_obj = update.message.document
    elif update.message.video_note:
        file_obj = update.message.video_note
    else:
        return

    if file_obj.file_size and file_obj.file_size > 2000 * 1024 * 1024:
        await update.message.reply_text("⚠️ Видео слишком большое (>2 GB).")
        return

    status = await update.message.reply_text("⏳ Обрабатываю...")

    job_id = "job_%s_%s" % (update.message.chat_id, uuid.uuid4().hex[:10])
    video_path = str(WORK_DIR / ("%s.mp4" % job_id))

    try:
        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(video_path)
        log.info("[VIDEO] %.1f MB", Path(video_path).stat().st_size / 1024 / 1024)

        await status.edit_text("⏳ Распознаю речь и перевожу...")
        result = await transcribe_and_translate(video_path)

        if not result:
            await status.edit_text("❌ Не удалось распознать или перевести речь.")
            return

        transcript, translation, geo = result

        # Сохраняем в БД для обоих ботов с меткой bot_source
        bot_src = 'translator' if is_translator_bot(update) else 'spy'
        item_id = await asyncio.to_thread(
            save_item, translation=translation, original_text=transcript, geo=geo,
            bot_source=bot_src)

        if item_id:
            await asyncio.to_thread(save_video_and_thumb, item_id, video_path)

        await status.edit_text("⏳ Отправляю...")
        await send_video_with_caption(update, video_path, translation)

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        log.error("Ошибка: %s", e, exc_info=True)
        try:
            await status.edit_text("❌ Ошибка: %s" % str(e)[:200])
        except Exception:
            pass
    finally:
        cleanup(video_path)
        await asyncio.to_thread(cleanup_old_temp_files)


# ── Обработка ссылок ──

async def handle_text(update, context):
    text = update.message.text or ""

    # Ищем Facebook-ссылки
    fb_urls = FB_URL_PATTERN.findall(text)
    seen = set()
    unique_fb = []
    for u in fb_urls:
        if u not in seen:
            seen.add(u)
            unique_fb.append(u)

    if not unique_fb:
        return

    # Лимит ссылок — чтобы один пользователь не блокировал остальных
    if len(unique_fb) > MAX_LINKS_PER_MSG:
        await update.message.reply_text(
            "⚠️ Максимум %d ссылок за раз. Отправьте остальные отдельным сообщением." % MAX_LINKS_PER_MSG
        )
        unique_fb = unique_fb[:MAX_LINKS_PER_MSG]

    # Ищем трекинг-ссылку (не-Facebook URL)
    non_fb_urls = NON_FB_URL_PATTERN.findall(text)

    total = len(unique_fb)
    status = await update.message.reply_text(
        "⏳ Найдено ссылок: %d. Обрабатываю..." % total
    )

    # [FIX #10] Парсим трекинг-ссылку ПАРАЛЛЕЛЬНО со скачиванием, а не заранее
    tracking_data = None  # dict: {domain, sub4, sub5, pix, formatted}

    for i, fb_url in enumerate(unique_fb, 1):
        video_path = None
        try:
            # Парсим трекинг только один раз
            if tracking_data is None and non_fb_urls:
                for url in non_fb_urls:
                    info = parse_tracking_url(url)
                    if info:
                        tracking_data = info
                        break
                if tracking_data is None:
                    tracking_data = {}  # Пометка что уже пробовали

            await status.edit_text("⏳ [%d/%d] Скачиваю видео..." % (i, total))

            video_path = await asyncio.to_thread(download_fb_video, fb_url)
            if not video_path:
                await update.message.reply_text(
                    "❌ [%d/%d] Не удалось скачать видео\n🔗 %s" % (i, total, fb_url)
                )
                continue

            file_size = os.path.getsize(video_path)

            # [FIX #11] Проверяем что файл является видео (существование уже подтверждено getsize)
            if file_size < MIN_VIDEO_SIZE:
                await update.message.reply_text(
                    "❌ [%d/%d] Скачанный файл повреждён\n🔗 %s" % (i, total, fb_url)
                )
                continue

            # Транскрибция + перевод
            await status.edit_text("⏳ [%d/%d] Распознаю речь и перевожу..." % (i, total))
            result = await transcribe_and_translate(video_path)

            transcript = result[0] if result else None
            translation = result[1] if result else None
            geo = result[2] if result else None

            # Собираем caption
            tracking_formatted = tracking_data.get("formatted", "") if tracking_data else ""
            caption_parts = []
            if translation:
                caption_parts.append(translation)
            if tracking_formatted:
                caption_parts.append(tracking_formatted)

            caption = "\n\n".join(caption_parts) if caption_parts else ""

            # Сохраняем в БД для обоих ботов с меткой bot_source
            bot_src = 'translator' if is_translator_bot(update) else 'spy'
            item_id = None
            if translation:
                item_id = await asyncio.to_thread(
                    save_item,
                    translation=translation,
                    original_text=transcript,
                    fb_url=fb_url,
                    tracking_domain=tracking_data.get("domain") if tracking_data else None,
                    sub4=tracking_data.get("sub4") if tracking_data else None,
                    sub5=tracking_data.get("sub5") if tracking_data else None,
                    pix=tracking_data.get("pix") if tracking_data else None,
                    geo=geo,
                    tracking_url=tracking_data.get("url") if tracking_data else None,
                    bot_source=bot_src,
                )

            # Сохраняем видео и превью
            if item_id:
                await asyncio.to_thread(save_video_and_thumb, item_id, video_path)

            # Отправляем
            if file_size <= TELEGRAM_FILE_LIMIT:
                await status.edit_text("⏳ [%d/%d] Отправляю..." % (i, total))
                await send_video_with_caption(update, video_path, caption)
            else:
                size_mb = file_size / (1024 * 1024)
                msg = "⚠️ Видео слишком большое (%.1f MB)" % size_mb
                if caption:
                    msg += "\n\n%s" % caption[:4000]
                await update.message.reply_text(msg)

        except Exception as e:
            log.error("Ошибка %d/%d: %s", i, total, e, exc_info=True)
            try:
                await update.message.reply_text(
                    "❌ [%d/%d] Ошибка: %s" % (i, total, str(e)[:200])
                )
            except Exception:
                pass
        finally:
            if video_path:
                cleanup(video_path)

    try:
        await status.delete()
    except Exception:
        pass

    # [FIX #9] Периодически чистим tmp
    await asyncio.to_thread(cleanup_old_temp_files)


# ── Отправка видео с caption ──

async def send_video_with_caption(update, video_path, caption):
    # type: (Update, str, str) -> None
    """
    Caption лимит: 1024. Текст лимит: 4096.
    ≤1024 → всё в caption.
    ≤4096 → видео без caption + один текст.
    >4096 → видео + несколько текстовых.
    """
    # [FIX #12] Проверяем что файл существует перед отправкой
    if not os.path.exists(video_path):
        log.error("[SEND] Файл не найден: %s", video_path)
        if caption:
            await update.message.reply_text(caption[:4096])
        return

    try:
        with open(video_path, "rb") as f:
            if len(caption) <= 1024:
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                )
            elif len(caption) <= 4096:
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                )
                await update.message.reply_text(caption)
            else:
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                )
                rest = caption
                while rest:
                    if len(rest) <= 4096:
                        await update.message.reply_text(rest)
                        break
                    # Ищем пробел для аккуратного разреза
                    cut = rest.rfind(' ', 0, 4096)
                    if cut < 3000:
                        cut = 4096
                    await update.message.reply_text(rest[:cut])
                    rest = rest[cut:].strip()
    except Exception as e:
        log.error("[SEND] Ошибка отправки: %s", e)
        # Пробуем отправить хотя бы текст
        if caption:
            try:
                await update.message.reply_text(caption[:4096])
            except Exception:
                pass


# ══════════════════════════════════════════════════════
# WEB SERVER — aiohttp API + static frontend
# ══════════════════════════════════════════════════════

WEB_DIR = Path(__file__).parent / "web"


def _db_query(sql, params=()):
    conn = _db_connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _db_scalar(sql, params=()):
    conn = _db_connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════
# Dual-portal AUTH + source filter
# ══════════════════════════════════════════════════════

# Пароли. Бэкенд проверяет токен (через X-Auth header или ?auth= query).
# ssteam   → видит только spy (legacy SPY-портал)
# ssteam2  → видит spy + translator (расширенный портал)
PASSWORDS = {
    'REDACTED_PASSWORD_1':  {'allowed_sources': ['spy']},
    'REDACTED_PASSWORD_2': {'allowed_sources': ['spy', 'translator']},
}


def _check_auth(request):
    """Возвращает dict: {ok: bool, allowed_sources: list, requested_source: str}.
    Берёт токен из X-Auth header или ?auth= query.
    """
    token = request.headers.get('X-Auth') or request.query.get('auth', '')
    cfg = PASSWORDS.get(token)
    if not cfg:
        return {'ok': False, 'allowed_sources': ['spy'], 'requested_source': 'spy'}
    requested = request.query.get('source', '').strip()
    allowed = cfg['allowed_sources']
    # Если ssteam (1 источник) — игнорируем requested, используем единственный allowed
    if len(allowed) == 1:
        return {'ok': True, 'allowed_sources': allowed, 'requested_source': allowed[0]}
    # ssteam2 — может выбирать конкретный источник или 'both'
    if requested in ('spy', 'translator'):
        return {'ok': True, 'allowed_sources': allowed, 'requested_source': requested}
    # default = both
    return {'ok': True, 'allowed_sources': allowed, 'requested_source': 'both'}


def _source_where_clause(auth):
    """Строит WHERE-клаузу + параметры для фильтрации по bot_source."""
    req = auth['requested_source']
    if req == 'both':
        sources = auth['allowed_sources']
    else:
        sources = [req]
    placeholders = ",".join("?" for _ in sources)
    return f"bot_source IN ({placeholders})", list(sources)


def _assert_item_visible(item_id, auth):
    """Проверяет что item с данным id виден текущему юзеру по его allowed_sources.
    Возвращает True/False. Используется во всех per-id endpoints для
    предотвращения cross-source enumeration.
    """
    if not auth or not auth.get('ok'):
        return False
    try:
        iid = int(item_id)
    except (ValueError, TypeError):
        return False
    src_clause, src_params = _source_where_clause(auth)
    row = _db_query(
        "SELECT 1 FROM items WHERE id=? AND " + src_clause + " LIMIT 1",
        [iid] + src_params,
    )
    return bool(row)


async def api_items(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    try:
        page = max(1, int(request.query.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(max(1, int(request.query.get("per_page", 30))), 100)
    except (ValueError, TypeError):
        per_page = 30
    q = request.query.get("q", "").strip()
    date_from = request.query.get("date_from", "").strip()
    date_to = request.query.get("date_to", "").strip()
    domain = request.query.get("domain", "").strip()
    sub4 = request.query.get("sub4", "").strip()
    sub5 = request.query.get("sub5", "").strip()
    geo = request.query.get("geo", "").strip()
    sort = request.query.get("sort", "newest").strip()
    unique = request.query.get("unique", "").strip()
    diamond = request.query.get("diamond", "").strip()

    where_clauses = []
    params = []

    # Source filter (изоляция SPY от Translator)
    src_clause, src_params = _source_where_clause(auth)
    where_clauses.append(src_clause)
    params.extend(src_params)

    def _escape_like(s):
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if q:
        where_clauses.append(
            "(translation LIKE ? ESCAPE '\\' OR uid LIKE ? ESCAPE '\\' "
            "OR tracking_url LIKE ? ESCAPE '\\' OR fb_url LIKE ? ESCAPE '\\' "
            "OR sub4 LIKE ? ESCAPE '\\' OR sub5 LIKE ? ESCAPE '\\' "
            "OR original_text LIKE ? ESCAPE '\\')"
        )
        escaped_q = "%%%s%%" % _escape_like(q)
        params.extend([escaped_q] * 7)
    if date_from:
        where_clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("created_at <= ?")
        params.append(date_to + " 23:59:59")
    if domain:
        where_clauses.append("tracking_domain = ?")
        params.append(domain)
    if sub4:
        where_clauses.append("sub4 LIKE ? ESCAPE '\\'")
        params.append("%%%s%%" % _escape_like(sub4))
    if sub5:
        where_clauses.append("sub5 LIKE ? ESCAPE '\\'")
        params.append("%%%s%%" % _escape_like(sub5))
    if geo:
        where_clauses.append("geo = ?")
        params.append(geo)
    pix = request.query.get("pix", "").strip()
    if pix:
        where_clauses.append("pix LIKE ? ESCAPE '\\'")
        params.append("%%%s%%" % _escape_like(pix))
    offer = request.query.get("offer", "").strip()
    if offer:
        where_clauses.append("offer_name = ?")
        params.append(offer)
    buyer = request.query.get("buyer", "").strip()
    if buyer:
        try:
            where_clauses.append("buyer_id = ?")
            params.append(int(buyer))
        except (ValueError, TypeError):
            pass

    # Diamond filter: ?media_type=video& in tracking_url
    if diamond == "only":
        where_clauses.append("tracking_url LIKE '%?media\\_type=video&%' ESCAPE '\\'")
    elif diamond == "hide":
        where_clauses.append("(tracking_url IS NULL OR tracking_url NOT LIKE '%?media\\_type=video&%' ESCAPE '\\')")

    # Unique filter via UniqueIndex (normal=first of each group, strict=only truly unique)
    if unique in ("1", "normal", "strict"):
        mode = "strict" if unique == "strict" else "normal"
        if _unique_index:
            with _engines_lock:
                uids = _unique_index.get_unique_ids(mode, allowed_sources=(auth['allowed_sources'] if auth['requested_source'] == 'both' else [auth['requested_source']]))
            if uids:
                # Use content_hash based SQL for efficiency
                if mode == "strict":
                    where_clauses.append(f"content_hash IN (SELECT content_hash FROM items WHERE {src_clause} AND content_hash IS NOT NULL GROUP BY content_hash HAVING COUNT(*)=1)")
                    params.extend(src_params)
                else:
                    # Earliest item per content_hash group (by date, not id — id ≠ date order
                    # after import_chatexport which inserts historical rows with newer ids).
                    where_clauses.append(
                        f"items.id IN (SELECT id FROM items i1 WHERE {src_clause} AND content_hash IS NOT NULL "
                        f"AND created_at = (SELECT MIN(created_at) FROM items i2 WHERE i2.content_hash = i1.content_hash AND i2.{src_clause}))"
                    )
                    # The src_clause appears twice in the subquery
                    params.extend(src_params)
                    params.extend(src_params)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    order = "ASC" if sort == "oldest" else "DESC"

    total = await asyncio.to_thread(
        _db_scalar, "SELECT COUNT(*) FROM items" + where_sql, params
    )

    offset = (page - 1) * per_page
    items = await asyncio.to_thread(
        _db_query,
        "SELECT items.*, buyers.name as buyer_name FROM items LEFT JOIN buyers ON items.buyer_id = buyers.id" + where_sql + " ORDER BY items.created_at " + order + " LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )

    return web.json_response({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total else 0,
    })


async def api_stats(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    src_clause, src_params = _source_where_clause(auth)
    where_sql = " WHERE " + src_clause
    total = await asyncio.to_thread(_db_scalar, "SELECT COUNT(*) FROM items" + where_sql, src_params)
    domains = await asyncio.to_thread(
        _db_query,
        "SELECT DISTINCT tracking_domain FROM items WHERE " + src_clause + " AND tracking_domain IS NOT NULL AND tracking_domain != '' ORDER BY tracking_domain",
        src_params,
    )
    geos = await asyncio.to_thread(
        _db_query,
        "SELECT DISTINCT geo FROM items WHERE " + src_clause + " AND geo IS NOT NULL AND geo != '' AND geo != 'XX' ORDER BY geo",
        src_params,
    )
    offers = await asyncio.to_thread(
        _db_query,
        "SELECT offer_name, COUNT(*) as cnt FROM items WHERE " + src_clause + " AND offer_name IS NOT NULL AND offer_name != '' "
        "GROUP BY offer_name ORDER BY cnt DESC",
        src_params,
    )
    return web.json_response({
        "total": total,
        "domains": [d["tracking_domain"] for d in domains],
        "geos": [g["geo"] for g in geos],
        "offers": [{"name": o["offer_name"], "count": o["cnt"]} for o in offers],
    })


async def api_top_domains(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    src_clause, src_params = _source_where_clause(auth)
    try:
        limit = min(int(request.query.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20
    try:
        days = int(request.query.get("days", 30))
    except (ValueError, TypeError):
        days = 30

    sql = f"""
        SELECT tracking_domain, COUNT(*) as cnt,
               MAX(created_at) as last_seen,
               GROUP_CONCAT(DISTINCT geo) as geos
        FROM items
        WHERE {src_clause} AND tracking_domain IS NOT NULL AND tracking_domain != ''
              AND created_at >= datetime('now', ?)
        GROUP BY tracking_domain
        ORDER BY cnt DESC
        LIMIT ?
    """
    rows = await asyncio.to_thread(_db_query, sql, src_params + ["-%d days" % days, limit])
    return web.json_response([{
        "domain": r["tracking_domain"],
        "count": r["cnt"],
        "last_seen": r["last_seen"],
        "geos": r["geos"].split(",") if r["geos"] else [],
    } for r in rows])


async def api_top_offers(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    src_clause, src_params = _source_where_clause(auth)
    try:
        limit = min(int(request.query.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20
    try:
        days = int(request.query.get("days", 30))
    except (ValueError, TypeError):
        days = 30

    sql = f"""
        SELECT offer_name, COUNT(*) as cnt,
               COUNT(DISTINCT tracking_domain) as domains,
               COUNT(DISTINCT buyer_id) as buyers,
               MAX(created_at) as last_seen,
               GROUP_CONCAT(DISTINCT geo) as geos
        FROM items
        WHERE {src_clause} AND offer_name IS NOT NULL AND offer_name != ''
              AND created_at >= datetime('now', ?)
        GROUP BY offer_name
        ORDER BY cnt DESC
        LIMIT ?
    """
    rows = await asyncio.to_thread(_db_query, sql, src_params + ["-%d days" % days, limit])
    return web.json_response([{
        "offer": r["offer_name"],
        "count": r["cnt"],
        "domains": r["domains"],
        "buyers": r["buyers"],
        "last_seen": r["last_seen"],
        "geos": r["geos"].split(",") if r["geos"] else [],
    } for r in rows])


async def api_lifespan(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Badge для одного item."""
    try:
        item_id = request.match_info["id"]
    except KeyError:
        return web.json_response({}, status=400)
    # Source-isolation: убеждаемся что item виден этому юзеру
    if not await asyncio.to_thread(_assert_item_visible, item_id, auth):
        return web.json_response({}, status=404)
    if _lifespan_engine:
        with _engines_lock:
            info = _lifespan_engine.get_lifespan(item_id)
        if info:
            return web.json_response(info)
    return web.json_response({})


async def api_lifespan_top(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Top running creatives."""
    try:
        limit = min(int(request.query.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20
    geo = request.query.get("geo", "").strip() or None
    if _lifespan_engine:
        with _engines_lock:
            top = _lifespan_engine.get_top_runners(limit=limit, geo=geo, min_days=1)
        # Source-filter: оставляем только items видимые юзеру
        # NB: get_top_runners возвращает агрегаты; фильтруем сюда же по item_ids
        if top:
            ids_in_top = []
            for entry in top:
                ids_in_top.extend(entry.get('item_ids', [])[:5])
            if ids_in_top:
                src_clause, src_params = _source_where_clause(auth)
                placeholders = ",".join("?" * len(ids_in_top))
                visible_rows = await asyncio.to_thread(
                    _db_query,
                    f"SELECT id FROM items WHERE id IN ({placeholders}) AND " + src_clause,
                    [int(i) for i in ids_in_top] + src_params,
                )
                visible = {str(r['id']) for r in visible_rows}
                top = [t for t in top if any(str(i) in visible for i in t.get('item_ids', [])[:5])]
        return web.json_response(top)
    return web.json_response([])


async def api_lifespan_badges(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Badges для списка item_ids (batch). GET /api/lifespan/badges?ids=1,2,3"""
    ids_str = request.query.get("ids", "")
    if not ids_str:
        return web.json_response({})
    ids = [i.strip() for i in ids_str.split(",") if i.strip()]
    # Source-isolation: фильтруем ids по тем что видны юзеру
    if ids:
        try:
            int_ids = [int(i) for i in ids[:100]]
            src_clause, src_params = _source_where_clause(auth)
            placeholders = ",".join("?" * len(int_ids))
            visible_rows = await asyncio.to_thread(
                _db_query,
                f"SELECT id FROM items WHERE id IN ({placeholders}) AND " + src_clause,
                int_ids + src_params,
            )
            visible = {str(r['id']) for r in visible_rows}
            ids = [i for i in ids if i in visible]
        except (ValueError, TypeError):
            return web.json_response({})
    result = {}
    if _lifespan_engine:
        with _engines_lock:
            for iid in ids[:100]:
                result[iid] = _lifespan_engine.get_badge(iid)
    return web.json_response(result)


async def api_favorites(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Список избранных items, отфильтрованных по source."""
    src_clause, src_params = _source_where_clause(auth)
    rows = await asyncio.to_thread(
        _db_query,
        "SELECT items.*, buyers.name as buyer_name FROM favorites "
        "JOIN items ON favorites.item_id = items.id "
        "LEFT JOIN buyers ON items.buyer_id = buyers.id "
        "WHERE " + src_clause + " "
        "ORDER BY favorites.created_at DESC",
        src_params,
    )
    return web.json_response(rows)


async def api_favorite_toggle(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Добавить/удалить из избранного. POST /api/favorites/toggle с JSON {item_id: N}."""
    data = await request.json()
    item_id = data.get("item_id")
    if not item_id:
        return web.json_response({"error": "item_id required"}, status=400)
    # Source-isolation: убеждаемся что item виден юзеру (защита от enumeration)
    if not await asyncio.to_thread(_assert_item_visible, item_id, auth):
        return web.json_response({"error": "not found"}, status=404)
    conn = _db_connect()
    try:
        existing = conn.execute("SELECT id FROM favorites WHERE item_id=?", (item_id,)).fetchone()
        if existing:
            conn.execute("DELETE FROM favorites WHERE item_id=?", (item_id,))
            conn.commit()
            return web.json_response({"status": "removed"})
        else:
            conn.execute("INSERT INTO favorites (item_id) VALUES (?)", (item_id,))
            conn.commit()
            return web.json_response({"status": "added"})
    finally:
        conn.close()


async def api_favorite_ids(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Возвращает список item_id в избранном (для быстрой проверки на фронте)."""
    rows = await asyncio.to_thread(_db_query, "SELECT item_id FROM favorites")
    return web.json_response([r["item_id"] for r in rows])


async def api_buyers(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Список всех buyers с количеством items."""
    rows = await asyncio.to_thread(_db_query, """
        SELECT b.id, b.name, b.prefix, COUNT(i.id) as cnt
        FROM buyers b LEFT JOIN items i ON i.buyer_id = b.id
        GROUP BY b.id ORDER BY cnt DESC
    """)
    return web.json_response(rows)


async def api_buyer_rename(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Переименовать buyer. POST /api/buyers/{id}/rename с JSON {name: "..."}."""
    try:
        buyer_id = int(request.match_info["id"])
    except (ValueError, TypeError):
        return web.json_response({"error": "bad id"}, status=400)
    data = await request.json()
    new_name = data.get("name", "").strip()
    if not new_name:
        return web.json_response({"error": "name required"}, status=400)
    conn = _db_connect()
    try:
        conn.execute("UPDATE buyers SET name=? WHERE id=?", (new_name, buyer_id))
        conn.commit()
    finally:
        conn.close()
    return web.json_response({"ok": True})


async def api_top_pixels(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    src_clause, src_params = _source_where_clause(auth)
    try:
        limit = min(int(request.query.get("limit", 20)), 50)
    except (ValueError, TypeError):
        limit = 20
    try:
        days = int(request.query.get("days", 30))
    except (ValueError, TypeError):
        days = 30

    sql = f"""
        SELECT pix, COUNT(*) as cnt,
               MAX(created_at) as last_seen,
               GROUP_CONCAT(DISTINCT tracking_domain) as domains
        FROM items
        WHERE {src_clause} AND pix IS NOT NULL AND pix != '' AND pix != 'unknown'
              AND created_at >= datetime('now', ?)
        GROUP BY pix
        ORDER BY cnt DESC
        LIMIT ?
    """
    rows = await asyncio.to_thread(_db_query, sql, src_params + ["-%d days" % days, limit])
    return web.json_response([{
        "pix": r["pix"],
        "count": r["cnt"],
        "last_seen": r["last_seen"],
        "domains": r["domains"].split(",")[:5] if r["domains"] else [],
    } for r in rows])


async def api_item(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    """Один элемент по ID."""
    try:
        item_id = int(request.match_info["id"])
    except (ValueError, TypeError):
        return web.json_response(None, status=400)
    src_clause, src_params = _source_where_clause(auth)
    rows = await asyncio.to_thread(
        _db_query, "SELECT * FROM items WHERE id=? AND " + src_clause, [item_id] + src_params)
    if not rows:
        return web.json_response(None, status=404)
    return web.json_response(rows[0])


async def api_similar(request):
    auth = _check_auth(request)
    if not auth['ok']:
        return web.json_response({'error': 'unauthorized'}, status=401)
    try:
        item_id = int(request.match_info["id"])
    except (ValueError, TypeError):
        return web.json_response([], status=400)
    # Source-isolation: queried item must be visible to user
    if not await asyncio.to_thread(_assert_item_visible, item_id, auth):
        return web.json_response([], status=404)
    # no hard cap — user wants full similar list
    try:
        limit = int(request.query.get("limit", 0))
    except (ValueError, TypeError):
        limit = 0

    global _similar_engine
    if not _similar_engine:
        return web.json_response([])

    # limit=0 means unlimited — pass huge number to engine
    effective_limit = limit if limit > 0 else 10_000
    # Source-aware: spy-юзер видит только spy кандидатов, ssteam2 — оба
    allowed = auth['allowed_sources'] if auth['requested_source'] == 'both' else [auth['requested_source']]
    with _engines_lock:
        results = _similar_engine.find_similar(str(item_id), limit=effective_limit, allowed_sources=allowed)

    # Enrich with DB data (thumbnail, translation preview, etc.)
    if results:
        ids = [int(r.card_id) for r in results]
        placeholders = ",".join("?" * len(ids))
        rows = await asyncio.to_thread(
            _db_query,
            "SELECT id, created_at, translation, tracking_domain, thumb_filename, video_filename, geo, sub4 "
            "FROM items WHERE id IN (%s)" % placeholders,
            ids,
        )
        db_map = {str(r['id']): r for r in rows}
    else:
        db_map = {}

    out = []
    for r in results:
        db_row = db_map.get(r.card_id, {})
        label_info = RELATIONSHIP_LABELS.get(r.relationship, {})
        out.append({
            "id": int(r.card_id) if r.card_id.isdigit() else r.card_id,
            "score": r.score,
            "relationship": r.relationship,
            "rel_label": label_info.get("ru", r.relationship),
            "rel_icon": label_info.get("icon", ""),
            "signals": r.signals,
            "created_at": db_row.get("created_at"),
            "translation": db_row.get("translation"),
            "tracking_domain": db_row.get("tracking_domain"),
            "thumb_filename": db_row.get("thumb_filename"),
            "video_filename": db_row.get("video_filename"),
            "geo": db_row.get("geo"),
            "sub4": db_row.get("sub4"),
        })

    return web.json_response(out)


async def serve_index(request):
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Frontend not found", status=404)


def _media_auth_check(request, filename):
    """Returns (auth_dict, item_id_int) or (None, None) if denied.
    Filename is `{id}.mp4` or `{id}.jpg`; we extract id and verify
    the item belongs to user's allowed_sources.
    """
    auth = _check_auth(request)
    if not auth['ok']:
        return None, None
    # Extract numeric id from filename (basename only, no path traversal)
    stem = Path(filename).stem
    try:
        item_id = int(stem)
    except (ValueError, TypeError):
        return None, None
    # Verify item exists and is in allowed sources
    src_clause, src_params = _source_where_clause(auth)
    row = _db_query(
        "SELECT 1 FROM items WHERE id=? AND " + src_clause + " LIMIT 1",
        [item_id] + src_params,
    )
    if not row:
        return None, None
    return auth, item_id


async def serve_video(request):
    filename = request.match_info["filename"]
    auth, _ = await asyncio.to_thread(_media_auth_check, request, filename)
    if not auth:
        return web.Response(status=404)  # 404 not 401: avoid revealing existence
    filepath = VIDEOS_DIR / Path(filename).name  # strip any path component
    if not filepath.exists():
        return web.Response(status=404)
    return web.FileResponse(filepath, headers={
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
    })


async def serve_thumb(request):
    filename = request.match_info["filename"]
    auth, _ = await asyncio.to_thread(_media_auth_check, request, filename)
    if not auth:
        return web.Response(status=404)
    filepath = THUMBS_DIR / Path(filename).name
    if not filepath.exists():
        return web.Response(status=404)
    return web.FileResponse(filepath, headers={
        "Content-Type": "image/jpeg",
        "Cache-Control": "private, max-age=86400",
    })


def create_web_app():
    app = web.Application()
    async def _capture_loop(_app):
        offer_classifier.MAIN_LOOP = asyncio.get_running_loop()
        log.info("[offer] captured main loop for live classification")
    app.on_startup.append(_capture_loop)
    app.router.add_get("/api/items", api_items)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/top-domains", api_top_domains)
    app.router.add_get("/api/top-offers", api_top_offers)
    app.router.add_get("/api/top-pixels", api_top_pixels)
    app.router.add_get("/api/buyers", api_buyers)
    app.router.add_get("/api/favorites", api_favorites)
    # Literal-suffix routes BEFORE param route (aiohttp matches in order)
    app.router.add_get("/api/lifespan/top", api_lifespan_top)
    app.router.add_get("/api/lifespan/badges", api_lifespan_badges)
    app.router.add_get("/api/lifespan/{id}", api_lifespan)
    app.router.add_get("/api/favorites/ids", api_favorite_ids)
    app.router.add_post("/api/favorites/toggle", api_favorite_toggle)
    app.router.add_post("/api/buyers/{id}/rename", api_buyer_rename)
    app.router.add_get("/api/item/{id}", api_item)
    app.router.add_get("/api/similar/{id}", api_similar)
    app.router.add_get("/api/videos/{filename}", serve_video)
    app.router.add_get("/api/thumbs/{filename}", serve_thumb)
    app.router.add_get("/", serve_index)
    if WEB_DIR.exists():
        app.router.add_static("/static/", WEB_DIR, show_index=False)
    return app


_web_runner = None

async def start_web_server():
    global _web_runner
    app = create_web_app()
    _web_runner = web.AppRunner(app)
    await _web_runner.setup()
    site = web.TCPSite(_web_runner, "0.0.0.0", 5555)
    await site.start()
    log.info("[WEB] Сервер запущен на http://0.0.0.0:5555")


async def stop_web_server():
    global _web_runner
    if _web_runner:
        await _web_runner.cleanup()
        _web_runner = None
        log.info("[WEB] Сервер остановлен")


# ══════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print("  🎬 Video Translator + Facebook Downloader Bot")
    print("=" * 55)

    errors = []
    if "ВСТАВЬТЕ" in TELEGRAM_BOT_TOKEN:
        errors.append("❌ Вставьте TELEGRAM_BOT_TOKEN")
    if "ВСТАВЬТЕ" in OPENAI_API_KEY:
        errors.append("❌ Вставьте OPENAI_API_KEY")
    if errors:
        for e in errors:
            print(e)
        return

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        print("  ✅ ffmpeg найден")
    except FileNotFoundError:
        print("  ❌ ffmpeg НЕ найден!")
        return

    # [FIX #9] Очистка при старте
    cleanup_old_temp_files()

    print("  ✅ OpenAI: настроен")
    print("  ✅ Telegram: настроен")
    print("  ✅ fdown.net: готов")

    # Local Bot API — лимит 2 GB вместо 20/50 MB
    LOCAL_API_URL = "http://localhost:8082/bot"
    LOCAL_FILE_URL = "http://localhost:8082/file/bot"

    tg_app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .base_url(LOCAL_API_URL)
        .base_file_url(LOCAL_FILE_URL)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.VIDEO_NOTE, handle_video))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Второй бот — translator-only (не сохраняет в БД и не появляется на сайте)
    tg_app_translator = (
        ApplicationBuilder()
        .token(TRANSLATOR_BOT_TOKEN)
        .base_url(LOCAL_API_URL)
        .base_file_url(LOCAL_FILE_URL)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )
    tg_app_translator.add_handler(CommandHandler("start", cmd_start))
    tg_app_translator.add_handler(CommandHandler("help", cmd_help))
    tg_app_translator.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.VIDEO_NOTE, handle_video))
    tg_app_translator.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("  ✅ Web dashboard: http://localhost:5555")
    print("")
    print("  🟢 Бот запущен!")
    print("     Ctrl+C для остановки")
    print("=" * 55)

    # Запускаем web-сервер и оба Telegram-бота в одном event loop
    async def run_all():
        await start_web_server()
        try:
            async with tg_app:
                async with tg_app_translator:
                    await tg_app.start()
                    await tg_app_translator.start()
                    await tg_app.updater.start_polling(drop_pending_updates=True)
                    await tg_app_translator.updater.start_polling(drop_pending_updates=True)
                    log.info("[BOT] SPY bot + Translator bot оба запущены")
                    # Бесконечный цикл — ждём Ctrl+C
                    try:
                        while True:
                            await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        pass
                    finally:
                        try: await tg_app_translator.updater.stop()
                        except Exception: pass
                        try: await tg_app.updater.stop()
                        except Exception: pass
                        try: await tg_app_translator.stop()
                        except Exception: pass
                        try: await tg_app.stop()
                        except Exception: pass
        finally:
            await stop_web_server()

    asyncio.run(run_all())

if __name__ == "__main__":
    main()