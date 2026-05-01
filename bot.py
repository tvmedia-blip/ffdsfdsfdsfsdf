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

import os, re, asyncio, logging, tempfile, subprocess, time, uuid, sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

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

TELEGRAM_BOT_TOKEN = "ВСТАВЬТЕ_ТОКЕН_БОТА"
OPENAI_API_KEY     = "ВСТАВЬТЕ_КЛЮЧ_OPENAI"

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_domain ON items(tracking_domain)")
        conn.commit()
    finally:
        conn.close()
    log.info("[DB] База данных инициализирована: %s", DB_PATH)

init_db()


def save_item(translation, original_text=None, fb_url=None,
              tracking_domain=None, sub4=None, sub5=None, pix=None):
    conn = None
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO items (fb_url, translation, original_text, tracking_domain, sub4, sub5, pix) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fb_url, translation, original_text, tracking_domain, sub4, sub5, pix),
        )
        conn.commit()
        log.info("[DB] Элемент сохранён")
    except Exception as e:
        log.error("[DB] Ошибка сохранения: %s", e)
    finally:
        if conn:
            conn.close()

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
            "domain": domain, "sub4": sub4, "sub5": sub5, "pix": pix,
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
            "domain": domain, "sub4": sub4, "sub5": sub5, "pix": pix,
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
        for f in WORK_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime) > 3600:
                f.unlink(missing_ok=True)
                log.info("[CLEANUP] Удалён старый файл: %s", f.name)
    except Exception as e:
        log.warning("[CLEANUP] %s", e)


# ══════════════════════════════════════════════════════
# ПОЛНЫЙ ПАЙПЛАЙН: видео → аудио → текст → перевод
# ══════════════════════════════════════════════════════

async def transcribe_and_translate(video_path):
    # type: (str) -> Optional[str]
    """Возвращает перевод речи из видео."""
    audio_path = None
    try:
        audio_path = await asyncio.to_thread(extract_audio, video_path)
        if not audio_path:
            return None

        transcript = await asyncio.to_thread(transcribe_audio, audio_path)
        if not transcript:
            return None

        translation = await asyncio.to_thread(translate_to_russian, transcript)
        return translation if translation else None
    finally:
        cleanup(audio_path)


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
        translation = await transcribe_and_translate(video_path)

        if not translation:
            await status.edit_text("❌ Не удалось распознать или перевести речь.")
            return

        # Сохраняем в БД
        await asyncio.to_thread(save_item, translation=translation)

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
        # [FIX #9] Периодически чистим tmp
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
            translation = await transcribe_and_translate(video_path)

            # Собираем caption
            tracking_formatted = tracking_data.get("formatted", "") if tracking_data else ""
            caption_parts = []
            if translation:
                caption_parts.append(translation)
            if tracking_formatted:
                caption_parts.append(tracking_formatted)

            caption = "\n\n".join(caption_parts) if caption_parts else ""

            # Сохраняем в БД
            if translation:
                await asyncio.to_thread(
                    save_item,
                    translation=translation,
                    fb_url=fb_url,
                    tracking_domain=tracking_data.get("domain") if tracking_data else None,
                    sub4=tracking_data.get("sub4") if tracking_data else None,
                    sub5=tracking_data.get("sub5") if tracking_data else None,
                    pix=tracking_data.get("pix") if tracking_data else None,
                )

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


async def api_items(request):
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

    where_clauses = []
    params = []

    def _escape_like(s):
        """Escape SQL LIKE wildcards so % and _ in input are treated as literals."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if q:
        where_clauses.append("translation LIKE ? ESCAPE '\\'")
        params.append("%%%s%%" % _escape_like(q))
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

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = await asyncio.to_thread(
        _db_scalar, "SELECT COUNT(*) FROM items" + where_sql, params
    )

    offset = (page - 1) * per_page
    items = await asyncio.to_thread(
        _db_query,
        "SELECT * FROM items" + where_sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
    total = await asyncio.to_thread(_db_scalar, "SELECT COUNT(*) FROM items")
    domains = await asyncio.to_thread(
        _db_query,
        "SELECT DISTINCT tracking_domain FROM items WHERE tracking_domain IS NOT NULL AND tracking_domain != '' ORDER BY tracking_domain",
    )
    return web.json_response({
        "total": total,
        "domains": [d["tracking_domain"] for d in domains],
    })


async def api_top_domains(request):
    try:
        days = int(request.query.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    if days not in (2, 5, 7, 14, 30):
        days = 30
    try:
        limit = min(max(1, int(request.query.get("limit", 10))), 50)
    except (ValueError, TypeError):
        limit = 10
    rows = await asyncio.to_thread(
        _db_query,
        "SELECT tracking_domain AS domain, COUNT(*) AS count "
        "FROM items "
        "WHERE tracking_domain IS NOT NULL AND tracking_domain != '' "
        "AND created_at >= datetime('now', ?) "
        "GROUP BY tracking_domain ORDER BY count DESC LIMIT ?",
        [f"-{days} days", limit],
    )
    return web.json_response({"days": days, "items": rows})


async def serve_index(request):
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Frontend not found", status=404)


def create_web_app():
    app = web.Application()
    app.router.add_get("/api/items", api_items)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/top-domains", api_top_domains)
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
    site = web.TCPSite(_web_runner, "0.0.0.0", 8080)
    await site.start()
    log.info("[WEB] Сервер запущен на http://0.0.0.0:8080")


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
    LOCAL_API_URL = "http://localhost:8081/bot"
    LOCAL_FILE_URL = "http://localhost:8081/file/bot"

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

    print("  ✅ Web dashboard: http://localhost:8080")
    print("")
    print("  🟢 Бот запущен!")
    print("     Ctrl+C для остановки")
    print("=" * 55)

    # Запускаем web-сервер и Telegram-бот в одном event loop
    async def run_all():
        await start_web_server()
        try:
            async with tg_app:
                await tg_app.start()
                await tg_app.updater.start_polling(drop_pending_updates=True)
                # Бесконечный цикл — ждём Ctrl+C
                try:
                    while True:
                        await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass
                finally:
                    await tg_app.updater.stop()
                    await tg_app.stop()
        finally:
            await stop_web_server()

    asyncio.run(run_all())

if __name__ == "__main__":
    main()