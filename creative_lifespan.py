"""
Creative Lifespan Engine
========================

Определяет сколько дней льётся креатив, отслеживая его через смену доменов
в пределах одного GEO.

Ключевой принцип: один и тот же текст (content_hash по первым 20 словам)
в одном GEO = один креатив, даже если домен, пиксель и баер менялись.

Модель данных:
    CreativeLife — полная жизнь креатива:
        - first_seen / last_seen / lifespan_days
        - days_active (кол-во уникальных дней с сайтингами)
        - domain_timeline (какой домен когда использовался)
        - buyer_timeline (какой баер когда запускал)
        - status: running / paused / stopped

Использование:
    engine = LifespanEngine()
    engine.ingest(items)  # массовая загрузка
    
    info = engine.get_lifespan("card_123")
    # → {lifespan_days: 14, days_active: 8, status: "running", ...}
    
    # Для UI-карточки
    badge = engine.get_badge("card_123")
    # → {text: "14 дней", color: "orange", icon: "🔥"}

    from creative_lifespan import compute_content_hash
    hash = compute_content_hash("Текст транскрипции...")
"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from collections import defaultdict, Counter
from enum import Enum

__version__ = "1.0.0"

# =============================================================================
# CONTENT HASH (shared with unique_filter)
# =============================================================================

def compute_content_hash(text: str, n_words: int = 20) -> str:
    """MD5 от первых N слов нормализованного текста."""
    if not text:
        return ""
    t = re.sub(r'<[^>]+>', ' ', text)
    t = re.sub(r'&\w+;', ' ', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip().lower()
    words = t.split()[:n_words]
    first_n = ' '.join(words)
    if not first_n:
        return ""
    return hashlib.md5(first_n.encode('utf-8')).hexdigest()


# =============================================================================
# GEO DETECTION
# =============================================================================

_GEO_FROM_META = [
    (r'\bDE\b', 'DE'), (r'\b(UK|GB)\b', 'GB'), (r'\bFR\b', 'FR'),
    (r'\bES\b', 'ES'), (r'\bIN\b', 'IN'), (r'\bCA\b', 'CA'),
    (r'\bPL\b', 'PL'), (r'\bRO\b', 'RO'), (r'\bDK\b', 'DK'),
    (r'\bTR\b', 'TR'), (r'\bCH\b', 'CH'),
]

_GEO_FROM_CONTENT = [
    (r'немц|герман|deutsch|bafin|bundestag', 'DE'),
    (r'индии|индий|рупий|lakh|crore|намасте', 'IN'),
    (r'британ|pound|£|british|england', 'GB'),
    (r'франц|français|france', 'FR'),
    (r'испан|españa|spanish', 'ES'),
    (r'канад|canada|canadian', 'CA'),
    (r'польш|polsk|złot', 'PL'),
    (r'румын|român|lei\b', 'RO'),
    (r'датск|dansk|kroner|danmark', 'DK'),
    (r'турц|türk|lira\b', 'TR'),
]


def detect_geo(sub4: str = "", sub5: str = "", content: str = "") -> Optional[str]:
    """Определяет GEO по метаданным и контенту."""
    for source in [sub4, sub5]:
        if not source:
            continue
        for pattern, geo in _GEO_FROM_META:
            if re.search(pattern, source):
                return geo
    for pattern, geo in _GEO_FROM_CONTENT:
        if re.search(pattern, content or '', re.I):
            return geo
    return None


# =============================================================================
# STATUS LOGIC
# =============================================================================

class Status(Enum):
    RUNNING = "running"     # Последний сайтинг < 3 дней назад
    PAUSED = "paused"       # Последний сайтинг 3-7 дней назад
    STOPPED = "stopped"     # Последний сайтинг > 7 дней назад
    FRESH = "fresh"         # Только первый день
    UNKNOWN = "unknown"


def _compute_status(last_seen: datetime, now: datetime) -> Status:
    """Определяет текущий статус креатива."""
    gap = (now - last_seen).days
    if gap <= 0:
        return Status.FRESH
    elif gap <= 3:
        return Status.RUNNING
    elif gap <= 7:
        return Status.PAUSED
    else:
        return Status.STOPPED


# =============================================================================
# BADGE (для UI карточки)
# =============================================================================

BADGE_CONFIG = {
    # (min_days, max_days) → {text, color, icon}
    (0, 0):    {"color": "#94a3b8", "icon": "🆕", "label_template": "Новый"},
    (1, 3):    {"color": "#22c55e", "icon": "🟢", "label_template": "{days}д"},
    (4, 7):    {"color": "#eab308", "icon": "🟡", "label_template": "{days}д"},
    (8, 14):   {"color": "#f97316", "icon": "🟠", "label_template": "{days}д"},
    (15, 30):  {"color": "#ef4444", "icon": "🔴", "label_template": "{days}д"},
    (31, 60):  {"color": "#dc2626", "icon": "🔥", "label_template": "{days}д"},
    (61, 9999): {"color": "#991b1b", "icon": "💀", "label_template": "{days}д"},
}


def _get_badge(lifespan_days: int, status: Status) -> Dict:
    """Генерирует badge для UI-карточки."""
    for (lo, hi), cfg in BADGE_CONFIG.items():
        if lo <= lifespan_days <= hi:
            return {
                "text": cfg["label_template"].format(days=lifespan_days),
                "color": cfg["color"],
                "icon": cfg["icon"],
                "lifespan_days": lifespan_days,
                "status": status.value,
            }
    return {"text": "?", "color": "#6b7280", "icon": "❓", "lifespan_days": lifespan_days, "status": status.value}


# =============================================================================
# CREATIVE LIFE DATA
# =============================================================================

@dataclass
class CreativeLife:
    """Полная жизнь одного креатива в одном GEO."""
    content_hash: str
    geo: Optional[str]
    
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    
    sighting_dates: Set[datetime] = field(default_factory=set)
    domains: Dict[str, Set[datetime]] = field(default_factory=lambda: defaultdict(set))  # domain → dates
    buyers: Dict[str, Set[datetime]] = field(default_factory=lambda: defaultdict(set))    # buyer_cluster → dates
    pixels: Set[str] = field(default_factory=set)
    item_ids: Set[str] = field(default_factory=set)
    
    first20_text: str = ""
    
    def add_sighting(self, date: datetime, domain: str = "", buyer: str = "",
                     pixel: str = "", item_id: str = ""):
        """Добавляет один сайтинг."""
        self.sighting_dates.add(date)
        
        if self.first_seen is None or date < self.first_seen:
            self.first_seen = date
        if self.last_seen is None or date > self.last_seen:
            self.last_seen = date
        
        if domain:
            self.domains[domain].add(date)
        if buyer:
            self.buyers[buyer].add(date)
        if pixel and pixel not in ('unknown', 'Unknown', ''):
            self.pixels.add(pixel)
        if item_id:
            self.item_ids.add(item_id)
    
    @property
    def lifespan_days(self) -> int:
        """Дни от первого до последнего сайтинга."""
        if not self.first_seen or not self.last_seen:
            return 0
        return (self.last_seen - self.first_seen).days
    
    @property
    def days_active(self) -> int:
        """Кол-во уникальных дней с сайтингами."""
        return len(self.sighting_dates)
    
    @property
    def n_domains(self) -> int:
        return len(self.domains)
    
    @property
    def n_buyers(self) -> int:
        return len(self.buyers)
    
    @property
    def n_sightings(self) -> int:
        return len(self.item_ids)
    
    def get_status(self, now: Optional[datetime] = None) -> Status:
        if now is None:
            now = datetime.now()
        if not self.last_seen:
            return Status.UNKNOWN
        return _compute_status(self.last_seen, now)
    
    def get_domain_timeline(self) -> List[Dict]:
        """Хронология смены доменов."""
        all_dates = sorted(self.sighting_dates)
        timeline = []
        for dt in all_dates:
            doms = [d for d, dates in self.domains.items() if dt in dates]
            timeline.append({
                "date": str(dt.date()),
                "domains": doms,
            })
        return timeline
    
    def to_dict(self, now: Optional[datetime] = None) -> Dict:
        """Полное представление для API."""
        status = self.get_status(now)
        return {
            "content_hash": self.content_hash,
            "geo": self.geo,
            "first_seen": str(self.first_seen.date()) if self.first_seen else None,
            "last_seen": str(self.last_seen.date()) if self.last_seen else None,
            "lifespan_days": self.lifespan_days,
            "days_active": self.days_active,
            "n_sightings": self.n_sightings,
            "n_domains": self.n_domains,
            "n_buyers": self.n_buyers,
            "n_pixels": len(self.pixels),
            "status": status.value,
            "badge": _get_badge(self.lifespan_days, status),
            "domain_timeline": self.get_domain_timeline(),
            "domains": list(self.domains.keys()),
            "buyers": list(self.buyers.keys()),
            "first20": self.first20_text,
        }


# =============================================================================
# ENGINE
# =============================================================================

class LifespanEngine:
    """
    Движок определения lifespan креативов.
    
    Usage:
        engine = LifespanEngine()
        engine.ingest(items)
        info = engine.get_lifespan("card_123")
        badge = engine.get_badge("card_123")
    """
    
    def __init__(self):
        # Primary index: (content_hash, geo, source) → CreativeLife
        self._lives: Dict[Tuple[str, Optional[str], str], CreativeLife] = {}
        # Cross-geo index: (content_hash, source) → CreativeLife
        self._cross_geo: Dict[Tuple[str, str], CreativeLife] = {}
        # Reverse: item_id → (content_hash, source) for cross-geo lookup
        self._item_to_cross_key: Dict[str, Tuple[str, str]] = {}
        # Reverse: item_id → (content_hash, geo, source)
        self._item_to_key: Dict[str, Tuple[str, Optional[str], str]] = {}
        # (content_hash, source) → set of geos
        self._hash_geos: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    def ingest(self, items: List[Dict]) -> None:
        """
        Массовая загрузка items.
        
        Args:
            items: список dict с полями:
                id: str
                content_hash: str
                date: str (DD.MM.YYYY) или datetime
                tracking_domain: str (опционально)
                sub4: str (опционально)
                sub5: str (опционально)
                pix: str (опционально)
                geo: str (опционально, иначе определяется автоматически)
                transcription: str (опционально, для first20)
        """
        for item in items:
            self._ingest_one(item)
    
    def add_item(self, item: Dict) -> Dict:
        """Добавляет один item, возвращает текущий lifespan."""
        self._ingest_one(item)
        return self.get_lifespan(str(item['id']))
    
    def _ingest_one(self, item: Dict):
        """Internal: обработка одного item."""
        item_id = str(item.get('id', ''))
        content_hash = item.get('content_hash', '')
        
        if not content_hash:
            if item.get('transcription'):
                content_hash = compute_content_hash(item['transcription'])
            else:
                return
        
        if not content_hash:
            return
        
        # Parse date
        date = item.get('date')
        if isinstance(date, str):
            for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y'):
                try:
                    date = datetime.strptime(date, fmt)
                    break
                except ValueError:
                    continue
            if isinstance(date, str):
                return
        elif not isinstance(date, datetime):
            return
        
        # Detect GEO
        geo = item.get('geo')
        if not geo:
            geo = detect_geo(
                item.get('sub4', ''),
                item.get('sub5', ''),
                item.get('transcription', '') or item.get('first20', ''),
            )
        
        # Buyer cluster
        buyer = item.get('buyer_cluster', '')
        if not buyer and item.get('sub4'):
            try:
                from buyer_fingerprint import classify
                r = classify(item['sub4'])
                buyer = r.get('cluster', '')
                if buyer and buyer.startswith('META_'):
                    buyer = ''
            except ImportError:
                pass
        
        # Извлекаем source — изоляция SPY от Translator
        source = item.get('source', 'spy')

        # Get or create CreativeLife (per-source, per-geo)
        key = (content_hash, geo, source)
        if key not in self._lives:
            self._lives[key] = CreativeLife(
                content_hash=content_hash,
                geo=geo,
                first20_text=item.get('first20', '')[:100],
            )

        life = self._lives[key]
        life.add_sighting(
            date=date,
            domain=item.get('tracking_domain', '') or item.get('link', ''),
            buyer=buyer,
            pixel=item.get('pix', ''),
            item_id=item_id,
        )

        # Update indexes
        cross_key = (content_hash, source)
        self._item_to_key[item_id] = key
        self._item_to_cross_key[item_id] = cross_key
        if geo:
            self._hash_geos[cross_key].add(geo)

        # Cross-geo per source
        if cross_key not in self._cross_geo:
            self._cross_geo[cross_key] = CreativeLife(
                content_hash=content_hash, geo=None,
                first20_text=item.get('first20', '')[:100],
            )
        self._cross_geo[cross_key].add_sighting(
            date=date,
            domain=item.get('tracking_domain', '') or item.get('link', ''),
            buyer=buyer,
            pixel=item.get('pix', ''),
            item_id=item_id,
        )
    
    def get_lifespan(self, item_id: str, now: Optional[datetime] = None) -> Optional[Dict]:
        """
        Возвращает полную информацию о lifespan (cross-geo) для карточки.
        Изолировано по bot_source (spy/translator не смешиваются).
        """
        cross_key = self._item_to_cross_key.get(item_id)
        if not cross_key:
            return None

        life = self._cross_geo.get(cross_key)
        if not life:
            return None

        result = life.to_dict(now)
        result["item_id"] = item_id

        geos = self._hash_geos.get(cross_key, set())
        result["geos"] = sorted(geos)

        return result

    def get_badge(self, item_id: str, now: Optional[datetime] = None) -> Dict:
        """Cross-geo badge, изолированный по bot_source."""
        cross_key = self._item_to_cross_key.get(item_id)
        if not cross_key:
            return _get_badge(0, Status.UNKNOWN)

        life = self._cross_geo.get(cross_key)
        if not life:
            return _get_badge(0, Status.UNKNOWN)

        status = life.get_status(now)
        return _get_badge(life.lifespan_days, status)

    def get_top_runners(self, limit: int = 20, geo: Optional[str] = None,
                        min_days: int = 1) -> List[Dict]:
        """
        Топ креативов по lifespan.
        
        Args:
            limit: максимум результатов
            geo: фильтр по GEO
            min_days: минимальный lifespan
        """
        candidates = []
        for key, life in self._lives.items():
            if life.lifespan_days < min_days:
                continue
            if geo and life.geo != geo:
                continue
            candidates.append(life)
        
        candidates.sort(key=lambda l: -l.lifespan_days)
        return [l.to_dict() for l in candidates[:limit]]
    
    def get_stats(self, now: Optional[datetime] = None) -> Dict:
        """Общая статистика."""
        if now is None:
            now = datetime.now()
        
        lives = list(self._lives.values())
        if not lives:
            return {"total": 0}
        
        lifespans = [l.lifespan_days for l in lives]
        statuses = Counter(l.get_status(now).value for l in lives)
        
        geo_avg = defaultdict(list)
        for l in lives:
            if l.geo:
                geo_avg[l.geo].append(l.lifespan_days)
        
        return {
            "total_creatives": len(lives),
            "avg_lifespan": round(sum(lifespans) / len(lifespans), 1),
            "max_lifespan": max(lifespans),
            "multi_day": sum(1 for d in lifespans if d > 0),
            "multi_day_pct": round(sum(1 for d in lifespans if d > 0) / len(lifespans) * 100, 1),
            "avg_lifespan_multi_day": round(
                sum(d for d in lifespans if d > 0) / max(1, sum(1 for d in lifespans if d > 0)), 1
            ),
            "domain_rotators": sum(1 for l in lives if l.n_domains >= 2),
            "multi_buyer": sum(1 for l in lives if l.n_buyers >= 2),
            "statuses": dict(statuses),
            "by_geo": {
                geo: {
                    "avg": round(sum(days) / len(days), 1),
                    "max": max(days),
                    "count": len(days),
                }
                for geo, days in sorted(geo_avg.items(), key=lambda x: -sum(x[1]) / len(x[1]))
            },
        }


# =============================================================================
# SQL SUPPORT
# =============================================================================

POSTGRES_LIFESPAN_VIEW = """
-- View: creative lifespan per GEO
CREATE MATERIALIZED VIEW IF NOT EXISTS creative_lifespans AS
WITH sightings AS (
    SELECT 
        content_hash,
        geo,
        DATE(created_at) as seen_date,
        tracking_domain,
        sub4,
        pix
    FROM items
    WHERE content_hash IS NOT NULL AND content_hash != ''
),
life AS (
    SELECT
        content_hash,
        geo,
        MIN(seen_date) as first_seen,
        MAX(seen_date) as last_seen,
        (MAX(seen_date) - MIN(seen_date)) as lifespan_days,
        COUNT(DISTINCT seen_date) as days_active,
        COUNT(*) as n_sightings,
        COUNT(DISTINCT tracking_domain) as n_domains,
        COUNT(DISTINCT sub4) as n_buyers,
        COUNT(DISTINCT pix) as n_pixels,
        ARRAY_AGG(DISTINCT tracking_domain) as domains
    FROM sightings
    GROUP BY content_hash, geo
)
SELECT 
    *,
    CASE 
        WHEN (CURRENT_DATE - last_seen) <= 3 THEN 'running'
        WHEN (CURRENT_DATE - last_seen) <= 7 THEN 'paused'
        ELSE 'stopped'
    END as status
FROM life;

-- Index
CREATE INDEX IF NOT EXISTS idx_cl_hash ON creative_lifespans (content_hash);
CREATE INDEX IF NOT EXISTS idx_cl_lifespan ON creative_lifespans (lifespan_days DESC);
CREATE INDEX IF NOT EXISTS idx_cl_geo ON creative_lifespans (geo);

-- Join with items for card badge
-- SELECT i.*, cl.lifespan_days, cl.status, cl.n_domains
-- FROM items i
-- LEFT JOIN creative_lifespans cl 
--   ON i.content_hash = cl.content_hash AND i.geo = cl.geo;

-- Refresh daily
-- REFRESH MATERIALIZED VIEW creative_lifespans;
"""


# =============================================================================
# TESTS
# =============================================================================

def run_tests():
    """Встроенные тесты."""
    from datetime import datetime
    
    engine = LifespanEngine()
    
    # Scenario: один креатив, один GEO, три домена за 10 дней
    items = [
        {'id': '1', 'content_hash': 'aaa', 'date': '01.04.2026',
         'tracking_domain': 'domain1.com', 'sub4': '', 'pix': 'px1', 'geo': 'DE'},
        {'id': '2', 'content_hash': 'aaa', 'date': '03.04.2026',
         'tracking_domain': 'domain1.com', 'sub4': '', 'pix': 'px1', 'geo': 'DE'},
        {'id': '3', 'content_hash': 'aaa', 'date': '05.04.2026',
         'tracking_domain': 'domain2.com', 'sub4': '', 'pix': 'px1', 'geo': 'DE'},
        {'id': '4', 'content_hash': 'aaa', 'date': '10.04.2026',
         'tracking_domain': 'domain3.com', 'sub4': '', 'pix': 'px2', 'geo': 'DE'},
        # Тот же хэш, другой GEO → отдельный lifespan
        {'id': '5', 'content_hash': 'aaa', 'date': '08.04.2026',
         'tracking_domain': 'domain-fr.com', 'sub4': '', 'pix': 'px3', 'geo': 'FR'},
        # Другой креатив
        {'id': '6', 'content_hash': 'bbb', 'date': '01.04.2026',
         'tracking_domain': 'other.com', 'sub4': '', 'pix': 'px4', 'geo': 'DE'},
    ]
    
    engine.ingest(items)
    
    now = datetime(2026, 4, 12)
    
    # Test 1: lifespan DE
    info = engine.get_lifespan('1', now)
    assert info is not None
    # Cross-geo: first=01.04, last=10.04 = 9d, 5 active days, 4 domains
    assert info['lifespan_days'] == 9, f"Expected 9 days, got {info['lifespan_days']}"
    assert info['days_active'] == 5, f"Expected 5 active days, got {info['days_active']}"
    assert info['n_domains'] == 4, f"Expected 4 domains, got {info['n_domains']}"
    assert info['status'] == 'running'
    assert 'FR' in info.get('geos', [])
    
    # Test 2: FR item — now gets cross-geo lifespan (same hash as DE)
    info_fr = engine.get_lifespan('5', now)
    assert info_fr['lifespan_days'] == 9, f"FR item should see 9d cross-geo, got {info_fr['lifespan_days']}"
    assert 'FR' in info_fr.get('geos', [])
    
    # Test 3: single-day creative
    info_b = engine.get_lifespan('6', now)
    assert info_b['lifespan_days'] == 0
    assert info_b['status'] == 'stopped'  # 11 days ago
    
    # Test 4: badge (cross-geo: 10d)
    badge = engine.get_badge('1', now)
    assert badge['lifespan_days'] == 9, f"Expected 9, got {badge['lifespan_days']}"
    assert badge['icon'] == '🟠'
    
    badge_new = engine.get_badge('6', now)
    assert badge_new['icon'] == '🆕'
    
    # Test 5: top runners
    top = engine.get_top_runners(limit=5, min_days=1)
    assert len(top) == 1
    assert top[0]['lifespan_days'] == 9
    
    # Test 6: stats
    stats = engine.get_stats(now)
    assert stats['total_creatives'] == 3  # aaa-DE, aaa-FR, bbb-DE
    assert stats['multi_day'] == 1
    assert stats['domain_rotators'] == 1
    
    # Test 7: add_item (cross-geo: now spans 01.04 to 14.04 = 13d + FR on 08.04 already counted)
    result = engine.add_item({
        'id': '7', 'content_hash': 'aaa', 'date': '14.04.2026',
        'tracking_domain': 'domain4.com', 'geo': 'DE',
    })
    assert result['lifespan_days'] == 13, f"Expected 13, got {result['lifespan_days']}"
    assert result['n_domains'] == 5, f"Expected 5, got {result['n_domains']}"
    
    # Test 8: domain timeline
    info2 = engine.get_lifespan('1', now)
    timeline = info2['domain_timeline']
    assert len(timeline) >= 4
    
    print("All 8 tests passed ✅")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_tests()
    elif len(sys.argv) > 1 and sys.argv[1] == "--sql":
        print(POSTGRES_LIFESPAN_VIEW)
    else:
        print(__doc__)
