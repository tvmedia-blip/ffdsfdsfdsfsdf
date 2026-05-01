"""Prokly: Playwright + proxy capture worker."""
import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import re
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

from . import db, jobs, proxies

log = logging.getLogger("prokly.crawler")

# Defaults — используются если у proxy_rec нет своих overrides
DEFAULT_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
)
DEFAULT_LOCALE = "en-GB"
DEFAULT_TIMEZONE = "Europe/London"
FB_REFERER = "https://l.facebook.com/"
NAV_TIMEOUT_MS = 30000
NETWORKIDLE_TIMEOUT_MS = 8000
POST_LOAD_WAIT_MS = 3000
RESTART_BROWSER_EVERY = 50  # против memory leak


def _parse_chrome_version(ua):
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else "147"


def build_device_config(user_agent):
    """Возвращает словарь под device-fingerprint, исходя из UA family.

    Поля:
        viewport, dpr, ua_platform, ua_data_platform, ua_data_platform_ver,
        gpu_vendor, gpu_renderer, concurrency, memory, model
    """
    ua = user_agent or ""
    chrome_ver = _parse_chrome_version(ua)
    if "Android" in ua:
        # Pixel-class. Chrome reduced UA скрывает реальное устройство → ставим
        # параметры, типичные для среднего Android-смартфона.
        m = re.search(r"Android (\d+)", ua)
        android_ver = m.group(1) if m else "10"
        return {
            "viewport": {"width": 412, "height": 915},
            "dpr": 3.0,
            "ua_platform": "Linux armv8l",
            "ua_data_platform": "Android",
            "ua_data_platform_ver": android_ver,
            "gpu_vendor": "Qualcomm",
            "gpu_renderer": "Adreno (TM) 740",
            "concurrency": 8,
            "memory": 4,
            "model": "",
            "chrome_ver": chrome_ver,
            "is_chromium": True,
        }
    if "iPhone" in ua or "iPad" in ua:
        m = re.search(r"OS (\d+)_", ua)
        ios_ver = (m.group(1) + ".0") if m else "17.0"
        return {
            "viewport": {"width": 390, "height": 844},
            "dpr": 3.0,
            "ua_platform": "iPhone",
            "ua_data_platform": "iOS",
            "ua_data_platform_ver": ios_ver,
            "gpu_vendor": "Apple Inc.",
            "gpu_renderer": "Apple GPU",
            "concurrency": 6,
            "memory": 4,
            "model": "iPhone",
            "chrome_ver": chrome_ver,
            "is_chromium": False,  # iPhone Safari, не Chromium
        }
    # default: Android Pixel
    return {
        "viewport": {"width": 412, "height": 915},
        "dpr": 3.0,
        "ua_platform": "Linux armv8l",
        "ua_data_platform": "Android",
        "ua_data_platform_ver": "10",
        "gpu_vendor": "Qualcomm",
        "gpu_renderer": "Adreno (TM) 740",
        "concurrency": 8,
        "memory": 4,
        "model": "",
        "chrome_ver": chrome_ver,
        "is_chromium": True,
    }


def build_stealth_init_script(cfg, locale):
    """Полный fingerprint-spoof для мобильного устройства. Прогоняется в каждом
    page context перед выполнением скриптов лендера.

    Покрывает: navigator.webdriver, platform, languages, plugins, maxTouchPoints,
    hardwareConcurrency, deviceMemory, userAgentData (Client Hints),
    Network Information API, Vibrate, Battery, WebGL renderer/vendor, screen,
    Permissions API, chrome.runtime.
    """
    base_lang = (locale or DEFAULT_LOCALE).split("-")[0]
    cfg_json = json.dumps(cfg)
    locale_json = json.dumps(locale or DEFAULT_LOCALE)
    base_lang_json = json.dumps(base_lang)
    return f"""
    (() => {{
      const CFG = {cfg_json};
      const LOC = {locale_json};
      const BASE_LANG = {base_lang_json};

      const define = (obj, name, getter) => {{
        try {{ Object.defineProperty(obj, name, {{get: getter, configurable: true}}); }} catch(e) {{}}
      }};

      // navigator.webdriver — НЕ определяем своим getter (будет палиться через _.has).
      // Полагаемся на --disable-blink-features=AutomationControlled (передан в launch),
      // плюс удаляем property с prototype если она там осталась.
      try {{ delete Navigator.prototype.webdriver; }} catch(e) {{}}
      try {{ delete navigator.webdriver; }} catch(e) {{}}

      // Notification.permission и PermissionStatus должны совпадать. Headless даёт
      // permission='denied', что палит несоответствие с query state='prompt'.
      try {{
        if (typeof Notification !== 'undefined') {{
          Object.defineProperty(Notification, 'permission', {{get: () => 'default', configurable: true}});
        }}
      }} catch(e) {{}}

      define(navigator, 'platform', () => CFG.ua_platform);
      define(navigator, 'maxTouchPoints', () => 5);
      define(navigator, 'hardwareConcurrency', () => CFG.concurrency);
      define(navigator, 'deviceMemory', () => CFG.memory);
      define(navigator, 'languages', () => [LOC, BASE_LANG]);

      // Chrome Mobile имеет 0 plugins — а headless-Chrome возвращает [] по дефолту,
      // что палится. Имитируем PluginArray-подобный объект пустой длины.
      try {{
        const pluginArr = Object.create(PluginArray.prototype);
        Object.defineProperty(pluginArr, 'length', {{value: 0}});
        define(navigator, 'plugins', () => pluginArr);
        const mimeArr = Object.create(MimeTypeArray.prototype);
        Object.defineProperty(mimeArr, 'length', {{value: 0}});
        define(navigator, 'mimeTypes', () => mimeArr);
      }} catch(e) {{}}

      // userAgentData — Client Hints (новый стандарт detection)
      const brands = [
        {{brand: 'Google Chrome', version: CFG.chrome_ver}},
        {{brand: 'Chromium', version: CFG.chrome_ver}},
        {{brand: 'Not?A_Brand', version: '24'}},
      ];
      const fullVersionList = [
        {{brand: 'Google Chrome', version: CFG.chrome_ver + '.0.6943.0'}},
        {{brand: 'Chromium', version: CFG.chrome_ver + '.0.6943.0'}},
        {{brand: 'Not?A_Brand', version: '24.0.0.0'}},
      ];
      const uaData = {{
        brands: brands,
        mobile: true,
        platform: CFG.ua_data_platform,
        getHighEntropyValues: function(hints) {{
          return Promise.resolve({{
            architecture: 'arm',
            bitness: '64',
            brands: brands,
            fullVersionList: fullVersionList,
            mobile: true,
            model: CFG.model || '',
            platform: CFG.ua_data_platform,
            platformVersion: CFG.ua_data_platform_ver,
            uaFullVersion: CFG.chrome_ver + '.0.6943.0',
            wow64: false,
          }});
        }},
        toJSON: function() {{
          return {{brands: brands, mobile: true, platform: CFG.ua_data_platform}};
        }}
      }};
      try {{ define(navigator, 'userAgentData', () => uaData); }} catch(e) {{}}

      // Network Information API — мобильник всегда 4g/cellular
      const conn = {{
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false,
        type: 'cellular',
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
        dispatchEvent: () => true,
      }};
      define(navigator, 'connection', () => conn);

      // Vibrate API — присутствует только на мобильных
      try {{ if (!navigator.vibrate) Navigator.prototype.vibrate = function() {{ return true; }}; }} catch(e) {{}}

      // Battery API — некоторые ленды читают
      if (!navigator.getBattery) {{
        navigator.getBattery = function() {{
          return Promise.resolve({{
            charging: true, chargingTime: 0, dischargingTime: Infinity, level: 0.85,
            addEventListener: () => {{}}, removeEventListener: () => {{}}, dispatchEvent: () => true,
          }});
        }};
      }}

      // WebGL fingerprint — частая проверка
      const overrideGL = (Proto) => {{
        if (!Proto || !Proto.prototype) return;
        const orig = Proto.prototype.getParameter;
        if (!orig) return;
        Proto.prototype.getParameter = function(p) {{
          // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
          if (p === 37445) return CFG.gpu_vendor;
          if (p === 37446) return CFG.gpu_renderer;
          // VENDOR/RENDERER (тоже опрашивают)
          if (p === 0x1F00) return 'WebKit';
          if (p === 0x1F01) return 'WebKit WebGL';
          return orig.apply(this, arguments);
        }};
      }};
      try {{ overrideGL(window.WebGLRenderingContext); }} catch(e) {{}}
      try {{ overrideGL(window.WebGL2RenderingContext); }} catch(e) {{}}

      // screen object
      try {{
        define(screen, 'width',       () => CFG.viewport.width);
        define(screen, 'height',      () => CFG.viewport.height);
        define(screen, 'availWidth',  () => CFG.viewport.width);
        define(screen, 'availHeight', () => CFG.viewport.height);
        define(screen, 'colorDepth',  () => 24);
        define(screen, 'pixelDepth',  () => 24);
      }} catch(e) {{}}

      // window.outer/inner размеры
      try {{
        define(window, 'innerWidth',  () => CFG.viewport.width);
        define(window, 'innerHeight', () => CFG.viewport.height);
        define(window, 'outerWidth',  () => CFG.viewport.width);
        define(window, 'outerHeight', () => CFG.viewport.height);
      }} catch(e) {{}}

      // devicePixelRatio
      try {{ define(window, 'devicePixelRatio', () => CFG.dpr); }} catch(e) {{}}

      // Permissions API — notifications обычно prompt на мобильных Chrome
      try {{
        const orig = navigator.permissions && navigator.permissions.query;
        if (orig) {{
          navigator.permissions.query = function(p) {{
            if (p && p.name === 'notifications') {{
              return Promise.resolve({{state: 'prompt', onchange: null}});
            }}
            return orig.apply(navigator.permissions, arguments);
          }};
        }}
      }} catch(e) {{}}

      // chrome.* (мобильный Chrome имеет ограниченный объект)
      if (CFG.is_chromium && !window.chrome) {{
        window.chrome = {{}};
      }}
      if (window.chrome && !window.chrome.runtime) {{
        try {{ window.chrome.runtime = {{ id: undefined, OnInstalledReason: {{}}, OnRestartRequiredReason: {{}} }}; }} catch(e) {{}}
      }}

      // navigator.bluetooth — есть на Android Chrome
      if (CFG.is_chromium && !navigator.bluetooth) {{
        try {{ define(navigator, 'bluetooth', () => ({{ getAvailability: () => Promise.resolve(true) }})); }} catch(e) {{}}
      }}

      // Anti-detection: убираем следы CDP
      try {{
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
      }} catch(e) {{}}
    }})();
    """


# ──────────────────────────────────────────────────────────────────────
# Archive builder — захватывает все subresources (как «Save Page As»)
# ──────────────────────────────────────────────────────────────────────

_EXT_BY_CT = {
    "text/html": ".html",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}


def _filename_from_url(u, content_type, used_names):
    """Подбирает уникальное имя файла для ZIP archive.

    Старается сохранить оригинальное имя; конфликты разрешает через хеш-префикс.
    """
    p = urlparse(u)
    base = unquote(Path(p.path).name) or "file"
    # Удалим query из имени
    base = re.sub(r"[^A-Za-z0-9._\-]", "_", base)[:80] or "file"
    # Если нет расширения — пытаемся восстановить по content-type
    if "." not in base or base.startswith("."):
        ct = (content_type or "").split(";")[0].strip().lower()
        ext = _EXT_BY_CT.get(ct, "") or mimetypes.guess_extension(ct) or ""
        if ext:
            base = base.rstrip(".") + ext
    # Уникальность
    name = base
    if name in used_names:
        h = hashlib.md5(u.encode("utf-8")).hexdigest()[:6]
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            name = f"{stem}_{h}.{ext}"
        else:
            name = f"{name}_{h}"
    return name


def _rewrite_html_links(html_text, page_url, filemap):
    """Парсит HTML и переписывает абсолютные/относительные URL ассетов на локальные."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html_text  # без bs4 — оставляем как есть
    soup = BeautifulSoup(html_text, "html.parser")

    def resolve(u):
        if not u or u.startswith("data:") or u.startswith("javascript:") or u.startswith("about:") or u.startswith("mailto:") or u.startswith("#"):
            return None
        try:
            return urljoin(page_url, u)
        except Exception:
            return None

    # src/href атрибуты
    attrs_by_tag = {
        "img": ("src", "data-src"),
        "source": ("src", "srcset"),
        "video": ("src", "poster"),
        "audio": ("src",),
        "iframe": ("src",),
        "embed": ("src",),
        "track": ("src",),
        "link": ("href",),
        "script": ("src",),
        "use": ("href", "xlink:href"),
        "object": ("data",),
    }
    for tag_name, attrs in attrs_by_tag.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                v = tag.get(attr)
                if not v:
                    continue
                if attr == "srcset":
                    # "url1 1x, url2 2x" — переписываем каждую часть
                    parts = []
                    for piece in v.split(","):
                        piece = piece.strip()
                        if not piece:
                            continue
                        bits = piece.split()
                        u = bits[0]
                        absolute = resolve(u)
                        if absolute and absolute in filemap:
                            bits[0] = "files/" + filemap[absolute]
                        parts.append(" ".join(bits))
                    tag[attr] = ", ".join(parts)
                else:
                    absolute = resolve(v)
                    if absolute and absolute in filemap:
                        tag[attr] = "files/" + filemap[absolute]

    # inline style="background:url(...)" — упрощённо
    for tag in soup.find_all(style=True):
        s = tag["style"]
        s = _rewrite_css_urls(s, page_url, filemap, in_subdir=False)
        tag["style"] = s

    # <style>...</style>
    for st in soup.find_all("style"):
        if st.string:
            st.string.replace_with(_rewrite_css_urls(st.string, page_url, filemap, in_subdir=False))

    return str(soup)


def _rewrite_css_urls(css_text, base_url, filemap, in_subdir):
    """Переписывает url(...) внутри CSS. Если CSS в files/, ссылается просто как «file.ext»;
    если CSS инлайн в HTML — то «files/file.ext»."""
    prefix = "" if in_subdir else "files/"

    def repl(m):
        u = m.group(1).strip().strip('"').strip("'")
        if not u or u.startswith("data:") or u.startswith("#"):
            return m.group(0)
        try:
            absolute = urljoin(base_url, u)
        except Exception:
            return m.group(0)
        if absolute in filemap:
            return f"url('{prefix}{filemap[absolute]}')"
        return m.group(0)

    return re.sub(r"url\(([^)]+)\)", repl, css_text or "")


def build_archive(html_text, page_url, responses):
    """Собирает ZIP-архив со структурой:
        index.html
        files/
            *.css, *.js, *.png, *.jpg, *.svg, *.woff2 ...

    `responses` — dict url → {body: bytes, content_type: str}.

    Возвращает bytes ZIP'а.
    """
    # Шаг 1: нормализуем URL → имя в files/
    filemap = {}
    used = set()
    page_url_normalized = page_url.split("#")[0]

    for url, resp in responses.items():
        if not resp.get("body"):
            continue
        if url == page_url_normalized:
            continue
        # Только GET-ресурсы (по сути все, что page.on('response') нам отдал)
        name = _filename_from_url(url, resp.get("content_type", ""), used)
        filemap[url] = name
        used.add(name)

    # Шаг 2: переписываем HTML
    new_html = _rewrite_html_links(html_text or "", page_url, filemap)

    # Шаг 3: переписываем url(...) внутри CSS-файлов
    rewritten_bodies = {}
    for url, resp in responses.items():
        ct = (resp.get("content_type") or "").lower()
        if "css" in ct and resp.get("body"):
            try:
                txt = resp["body"].decode("utf-8", errors="ignore")
                txt = _rewrite_css_urls(txt, url, filemap, in_subdir=True)
                rewritten_bodies[url] = txt.encode("utf-8")
            except Exception:
                rewritten_bodies[url] = resp["body"]
        else:
            rewritten_bodies[url] = resp.get("body") or b""

    # Шаг 4: ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("index.html", (new_html or "").encode("utf-8"))
        for url, fname in filemap.items():
            body = rewritten_bodies.get(url) or b""
            if body:
                zf.writestr(f"files/{fname}", body)
    return buf.getvalue()


def make_thumbnail(screenshot_bytes, max_width=480, quality=72):
    """Создаёт thumbnail для карточки галереи: top-16:9 crop + resize.

    Полный full-page скриншот ~1.5 MB. Thumb ~10-25 KB — на 50-100x легче.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        # Crop top 16:9 (выше первой "складки" мобильного экрана)
        target_h = int(w * 9 / 16)
        if h > target_h:
            img = img.crop((0, 0, w, target_h))
        # Resize до max_width — для retina 480 px достаточно
        if img.size[0] > max_width:
            new_h = int(img.size[1] * max_width / img.size[0])
            img = img.resize((max_width, new_h), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("[crawler] thumb gen failed: %s", e)
        return None


CF_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser",
    "cf-challenge",
)


def _looks_like_cf(text):
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in CF_MARKERS) and len(t) < 1500


def _get_item_from_main_db(item_id):
    """Читает item из ОСНОВНОЙ spy_data.db. Возвращает (tracking_url, geo, bot_source) или None."""
    try:
        from app import DB_PATH  # type: ignore
    except Exception:
        DB_PATH = Path(__file__).parent.parent / "spy_data.db"
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        row = conn.execute(
            "SELECT tracking_url, geo, bot_source FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        return row  # tuple or None
    finally:
        conn.close()


def _save_snapshot_row(*, item_id, job_id, url, final_url, geo, status,
                       screenshot_path, html_path, page_title, proxy_used, duration_ms):
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO prokly_snapshots "
            "(item_id, job_id, url, final_url, geo, status, screenshot_path, html_path, "
            " page_title, proxy_used, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, job_id, url, final_url, geo, status,
             screenshot_path, html_path, page_title, proxy_used, duration_ms),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class Crawler:
    """Долгоживущий Playwright. Контекст создаётся per-snapshot, browser переиспользуется."""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.snapshots_done = 0
        self._stealth = None

    async def start(self):
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import Stealth
            self._stealth = Stealth()
        except Exception:
            self._stealth = None

        self._pw_ctx = async_playwright()
        if self._stealth:
            self._pw_ctx = self._stealth.use_async(self._pw_ctx)
        self.pw = await self._pw_ctx.__aenter__()
        self.browser = await self.pw.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        log.info("[crawler] browser launched")

    async def stop(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self._pw_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self.browser = None
        self.pw = None

    async def maybe_restart(self):
        if self.snapshots_done >= RESTART_BROWSER_EVERY:
            log.info("[crawler] restart browser after %d snapshots", self.snapshots_done)
            await self.stop()
            await self.start()
            self.snapshots_done = 0

    async def capture(self, url, proxy_dict, geo, *,
                      user_agent=None, locale=None, timezone=None):
        """Делает снимок страницы с per-geo device emulation.

        Поля результата: status, page_title, html_bytes, screenshot_bytes, final_url
        """
        from playwright.async_api import TimeoutError as PWTimeout

        ua = user_agent or DEFAULT_MOBILE_UA
        loc = locale or DEFAULT_LOCALE
        tz = timezone or DEFAULT_TIMEZONE
        cfg = build_device_config(ua)
        accept_lang = f"{loc},{loc.split('-')[0]};q=0.9"

        ctx_kwargs = dict(
            proxy=proxy_dict,
            user_agent=ua,
            viewport=cfg["viewport"],
            screen=cfg["viewport"],
            device_scale_factor=cfg["dpr"],
            is_mobile=True,
            has_touch=True,
            locale=loc,
            timezone_id=tz,
            extra_http_headers={
                "Referer": FB_REFERER,
                "Accept-Language": accept_lang,
                "sec-ch-ua": (
                    f'"Google Chrome";v="{cfg["chrome_ver"]}", '
                    f'"Chromium";v="{cfg["chrome_ver"]}", "Not?A_Brand";v="24"'
                ),
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": f'"{cfg["ua_data_platform"]}"',
            },
            ignore_https_errors=True,
        )
        ctx = await self.browser.new_context(**ctx_kwargs)
        # Полный mobile-fingerprint stealth-инжект (выполняется до скриптов лендера)
        await ctx.add_init_script(build_stealth_init_script(cfg, loc))

        # Перехватываем все subresources для построения архива «как Save Page As»
        captured_responses = {}

        async def _on_response(resp):
            try:
                u = resp.url
                if not u or u.startswith("data:") or u.startswith("about:") or u.startswith("blob:"):
                    return
                # Не сохраняем ответы > 5 MB (видео, тяжёлые медиа) — не нужны для архива
                try:
                    body = await resp.body()
                except Exception:
                    return
                if not body or len(body) > 5 * 1024 * 1024:
                    return
                ct = (resp.headers or {}).get("content-type", "")
                # Пропускаем не-200 (404 не нужны в архиве)
                if resp.status != 200:
                    return
                captured_responses[u] = {"body": body, "content_type": ct, "status": resp.status}
            except Exception:
                pass

        result = {"status": "unknown", "page_title": "", "html_bytes": b"",
                  "screenshot_bytes": b"", "archive_bytes": b"", "final_url": url}
        try:
            page = await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(_on_response(r)))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                result["status"] = "timeout"
                return result
            except Exception as e:
                result["status"] = "nav_error"
                result["error"] = str(e)[:200]
                return result

            # Networkidle опционально
            try:
                await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
            except Exception:
                pass
            await page.wait_for_timeout(POST_LOAD_WAIT_MS)

            # Детект CF challenge — ждём дополнительно
            try:
                title0 = await page.title()
                body0 = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''")
            except Exception:
                title0 = ""
                body0 = ""
            if _looks_like_cf((title0 or "") + " " + (body0 or "")):
                log.info("[crawler] CF detected, waiting up to 15s...")
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    try:
                        body0 = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''")
                        title0 = await page.title()
                    except Exception:
                        break
                    if not _looks_like_cf((title0 or "") + " " + (body0 or "")):
                        break

            # Если всё ещё CF — фиксируем
            if _looks_like_cf((title0 or "") + " " + (body0 or "")):
                result["status"] = "cf_blocked"
                result["page_title"] = title0[:200]
                # делаем скриншот всё равно — для дебага
                try:
                    result["screenshot_bytes"] = await page.screenshot(
                        full_page=False, type="jpeg", quality=70
                    )
                except Exception:
                    pass
                result["final_url"] = page.url
                return result

            # Capture
            try:
                title = await page.title()
            except Exception:
                title = ""
            try:
                html = await page.content()
            except Exception:
                html = ""
            try:
                screenshot = await page.screenshot(
                    full_page=True, type="jpeg", quality=70
                )
            except Exception:
                screenshot = b""

            result["status"] = "ok"
            result["page_title"] = (title or "")[:300]
            result["html_bytes"] = html.encode("utf-8") if isinstance(html, str) else html
            result["screenshot_bytes"] = screenshot
            result["final_url"] = page.url

            # Подождём ещё немного чтобы дотянуть оставшиеся ответы которые ещё в полёте
            try:
                await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Сборка архива (HTML + все ассеты, как Save Page As)
            try:
                if html and captured_responses:
                    archive = build_archive(html, page.url, captured_responses)
                    result["archive_bytes"] = archive
                    log.info(
                        "[crawler] archive built: %d files, %d KB",
                        len(captured_responses), len(archive) // 1024,
                    )
            except Exception as e:
                log.warning("[crawler] archive build failed: %s", e)

            return result
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            self.snapshots_done += 1


async def _process_one_job(crawler, job):
    """Обрабатывает один job. Не должен бросать наружу."""
    job_id = job["id"]
    item_id = job["item_id"]

    item_row = _get_item_from_main_db(item_id)
    if not item_row:
        log.warning("[worker] job %d: item %d not found", job_id, item_id)
        jobs.mark_failed(job_id, "item not found")
        return

    tracking_url, geo, bot_source = item_row[0], item_row[1], item_row[2]

    if not tracking_url:
        jobs.mark_failed(job_id, "no tracking_url")
        _save_snapshot_row(
            item_id=item_id, job_id=job_id, url="", final_url=None,
            geo=geo, status="no_url",
            screenshot_path=None, html_path=None,
            page_title=None, proxy_used=None, duration_ms=0,
        )
        return

    if not geo or geo == "XX":
        jobs.mark_failed(job_id, "no geo")
        _save_snapshot_row(
            item_id=item_id, job_id=job_id, url=tracking_url, final_url=None,
            geo=geo, status="no_geo",
            screenshot_path=None, html_path=None,
            page_title=None, proxy_used=None, duration_ms=0,
        )
        return

    proxy_rec = proxies.get_by_geo(geo)
    if not proxy_rec:
        jobs.mark_failed(job_id, f"no proxy for {geo}")
        _save_snapshot_row(
            item_id=item_id, job_id=job_id, url=tracking_url, final_url=None,
            geo=geo, status="no_proxy",
            screenshot_path=None, html_path=None,
            page_title=None, proxy_used=None, duration_ms=0,
        )
        return

    try:
        parsed = proxies.parse_proxy_url(proxy_rec["proxy_url"])
        # Re-validate SSRF на каждом запуске
        from urllib.parse import urlparse as _u
        host = _u(parsed["server"]).hostname
        ok, reason = proxies.is_public_host(host)
        if not ok:
            jobs.mark_failed(job_id, f"proxy host blocked: {reason}")
            return
        proxy_dict = proxies.to_playwright_proxy(parsed)
    except Exception as e:
        jobs.mark_failed(job_id, f"proxy parse: {e}")
        return

    await crawler.maybe_restart()

    proxy_label = parsed["server"]  # без credentials
    started = time.time()
    try:
        result = await crawler.capture(
            tracking_url, proxy_dict, geo,
            user_agent=proxy_rec.get("user_agent"),
            locale=proxy_rec.get("locale"),
            timezone=proxy_rec.get("timezone"),
        )
    except Exception as e:
        log.exception("[worker] capture failed")
        jobs.mark_failed(job_id, f"capture error: {e}")
        return

    duration_ms = int((time.time() - started) * 1000)

    # Save files (если есть что сохранять)
    snap_id_pre = _save_snapshot_row(
        item_id=item_id, job_id=job_id, url=tracking_url,
        final_url=result.get("final_url"),
        geo=geo, status=result["status"],
        screenshot_path=None, html_path=None,
        page_title=result.get("page_title"),
        proxy_used=proxy_label, duration_ms=duration_ms,
    )

    snap_dir = db.SNAPSHOTS_DIR
    snap_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = None
    html_path = None

    thumb_path = None
    if result.get("screenshot_bytes"):
        sp = snap_dir / f"{snap_id_pre}.jpg"
        try:
            sp.write_bytes(result["screenshot_bytes"])
            screenshot_path = str(sp.relative_to(db.PROKLY_DATA))
        except Exception as e:
            log.warning("write screenshot: %s", e)
        # Thumb: top-16:9 crop, 480px wide, для лёгкой галереи
        thumb_bytes = make_thumbnail(result["screenshot_bytes"])
        if thumb_bytes:
            tp = snap_dir / f"{snap_id_pre}_thumb.jpg"
            try:
                tp.write_bytes(thumb_bytes)
                thumb_path = str(tp.relative_to(db.PROKLY_DATA))
            except Exception as e:
                log.warning("write thumb: %s", e)

    if result.get("html_bytes"):
        hp = snap_dir / f"{snap_id_pre}.html"
        try:
            hp.write_bytes(result["html_bytes"])
            html_path = str(hp.relative_to(db.PROKLY_DATA))
        except Exception as e:
            log.warning("write html: %s", e)

    archive_path = None
    if result.get("archive_bytes"):
        ap = snap_dir / f"{snap_id_pre}.zip"
        try:
            ap.write_bytes(result["archive_bytes"])
            archive_path = str(ap.relative_to(db.PROKLY_DATA))
        except Exception as e:
            log.warning("write archive: %s", e)

    # Update paths
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE prokly_snapshots SET screenshot_path=?, html_path=?, archive_path=?, thumb_path=? WHERE id=?",
            (screenshot_path, html_path, archive_path, thumb_path, snap_id_pre),
        )
        conn.commit()
    finally:
        conn.close()

    if result["status"] == "ok":
        jobs.mark_done(job_id)
    else:
        jobs.mark_failed(job_id, result["status"])


async def worker_loop_main(stop_event=None):
    """Главный цикл воркера — читает prokly_jobs, обрабатывает по одной."""
    crawler = Crawler()
    try:
        await crawler.start()
    except Exception as e:
        log.exception("[worker] crawler start failed: %s", e)
        return

    log.info("[worker] started")
    last_recovery = 0
    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            # Periodic stale recovery
            now = time.time()
            if now - last_recovery > 60:
                recovered = jobs.stale_recovery()
                if recovered:
                    log.info("[worker] recovered %d stale jobs", recovered)
                last_recovery = now

            try:
                job = jobs.claim_next()
            except Exception as e:
                log.warning("[worker] claim error: %s", e)
                await asyncio.sleep(5)
                continue

            if not job:
                await asyncio.sleep(2)
                continue

            log.info("[worker] processing job %d (item %d)", job["id"], job["item_id"])
            try:
                await _process_one_job(crawler, job)
            except asyncio.CancelledError:
                # job останется in_progress — stale_recovery вернёт его
                raise
            except Exception as e:
                log.exception("[worker] unhandled: %s", e)
                jobs.mark_failed(job["id"], str(e))
    finally:
        await crawler.stop()
        log.info("[worker] stopped")
