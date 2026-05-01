"""
Enhanced Similar Creatives Engine
=================================

Расширенный алгоритм поиска похожих креативов, объединяющий:
- content_hash (тот же ролик)
- tracking_domain (тот же ленд)  
- buyer fingerprint (тот же баер по 4sub)
- pixel match (тот же пиксель)
- 5sub match (тот же adset-группа)
- agency match (тот же селлер)

Каждый сигнал имеет вес и confidence. Итоговый score определяет ранжирование.
Результат группируется по типу связи для UI.

Использование:
    engine = SimilarEngine(items_index)
    results = engine.find_similar(card_id, limit=6)
"""

import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from buyer_fingerprint import classify, detect_agency

__version__ = "1.0.0"


# =============================================================================
# CONFIGURATION
# =============================================================================

class Signal(Enum):
    """Типы сигналов схожести."""
    CONTENT_HASH = "content_hash"
    TRACKING_DOMAIN = "tracking_domain"
    BUYER_CLUSTER = "buyer_cluster"
    PIXEL = "pixel"
    SUB5 = "sub5"
    AGENCY = "agency"


# Веса сигналов (можно настраивать)
DEFAULT_WEIGHTS = {
    Signal.CONTENT_HASH:     10,   # Тот же ролик — максимальный приоритет
    Signal.PIXEL:             7,   # Тот же пиксель — очень сильный сигнал
    Signal.TRACKING_DOMAIN:   6,   # Тот же ленд
    Signal.BUYER_CLUSTER:     5,   # Тот же «почерк» баера
    Signal.SUB5:              4,   # Тот же adset-группа
    Signal.AGENCY:            1,   # Тот же селлер (слабый сигнал)
}

# 5sub значения которые не информативны (generic)
GENERIC_5SUB = {
    'unknown', 'Unknown', 'UNKNOWN',
    '1', '2', '3',
    'Facebook_Mobile_Feed',
    'test', 'Test',
}

# pix значения которые не информативны
GENERIC_PIX = {
    'unknown', 'Unknown', 'UNKNOWN', '',
}

# Минимальный score для включения в результат
MIN_SCORE = 4


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class CardData:
    """Данные карточки для similarity matching."""
    id: str
    content_hash: Optional[str] = None
    tracking_domain: Optional[str] = None
    sub4: Optional[str] = None             # 4sub (campaign name)
    sub5: Optional[str] = None             # 5sub (adset name)
    pixel: Optional[str] = None            # pix
    created_at: Optional[str] = None       # дата для сортировки
    source: Optional[str] = 'spy'          # bot_source: 'spy' | 'translator'
    
    # Computed (заполняются при индексации)
    buyer_cluster: Optional[str] = None
    buyer_confidence: Optional[str] = None
    agency: Optional[str] = None
    geo: Optional[str] = None


@dataclass
class SimilarResult:
    """Один результат похожего креатива."""
    card_id: str
    score: float
    signals: List[Dict]          # [{signal: str, weight: int, detail: str}]
    relationship: str            # primary relationship type for UI
    card_data: Optional[Dict] = None


# =============================================================================
# GEO EXTRACTION
# =============================================================================

GEO_PATTERNS = [
    (r'\b(DE|DE_|_DE_|_DE$)', 'DE'),
    (r'\b(UK|GB|_UK_|_GB_|_UK$|_GB$)', 'GB'),
    (r'\b(FR|_FR_|_FR$)', 'FR'),
    (r'\b(ES|_ES_|_ES$)', 'ES'),
    (r'\b(IN|_IN_|_IN$)', 'IN'),
    (r'\b(CA|_CA_|_CA$)', 'CA'),
    (r'\b(PL|_PL_|_PL$)', 'PL'),
    (r'\b(RO|_RO_|_RO$)', 'RO'),
    (r'\b(DK|_DK_|_DK$)', 'DK'),
    (r'\b(TR|_TR_|_TR$)', 'TR'),
    (r'\b(CH|_CH_|_CH$)', 'CH'),
]

def extract_geo(text: str) -> Optional[str]:
    """Извлекает GEO код из 4sub/5sub."""
    if not text:
        return None
    for pattern, geo in GEO_PATTERNS:
        if re.search(pattern, text):
            return geo
    return None


# =============================================================================
# DOMAIN NORMALIZATION
# =============================================================================

def normalize_domain(domain: str) -> str:
    """Нормализует домен для сравнения."""
    if not domain:
        return ""
    d = domain.lower().strip()
    d = re.sub(r'^https?://', '', d)
    d = re.sub(r'^www\.', '', d)
    d = re.sub(r'[/\s]+$', '', d)
    return d


# =============================================================================
# SIMILARITY ENGINE
# =============================================================================

class SimilarEngine:
    """
    Движок поиска похожих креативов.
    
    Usage:
        engine = SimilarEngine()
        engine.index_cards(cards_list)  # или add_card по одной
        results = engine.find_similar("card_123", limit=6)
    """
    
    def __init__(self, weights: Optional[Dict] = None):
        self.weights = weights or DEFAULT_WEIGHTS
        self.cards: Dict[str, CardData] = {}
        
        # Inverted indexes for fast lookup
        self._idx_content_hash: Dict[str, set] = {}
        self._idx_domain: Dict[str, set] = {}
        self._idx_cluster: Dict[str, set] = {}
        self._idx_pixel: Dict[str, set] = {}
        self._idx_sub5: Dict[str, set] = {}
        self._idx_agency: Dict[str, set] = {}
    
    def add_card(self, card: CardData) -> None:
        """Добавляет карточку в индекс."""
        # Compute buyer fingerprint
        if card.sub4:
            result = classify(card.sub4)
            card.buyer_cluster = result['cluster']
            card.buyer_confidence = result['confidence']
            card.agency = result['agency']
        
        # Extract geo
        card.geo = extract_geo(card.sub4 or '') or extract_geo(card.sub5 or '')
        
        # Store
        self.cards[card.id] = card
        
        # Update indexes
        if card.content_hash:
            self._idx_content_hash.setdefault(card.content_hash, set()).add(card.id)
        
        domain = normalize_domain(card.tracking_domain or '')
        if domain:
            self._idx_domain.setdefault(domain, set()).add(card.id)
        
        if card.buyer_cluster:
            self._idx_cluster.setdefault(card.buyer_cluster, set()).add(card.id)
        
        if card.pixel and card.pixel not in GENERIC_PIX:
            self._idx_pixel.setdefault(card.pixel, set()).add(card.id)
        
        if card.sub5 and card.sub5 not in GENERIC_5SUB:
            self._idx_sub5.setdefault(card.sub5, set()).add(card.id)
        
        if card.agency:
            self._idx_agency.setdefault(card.agency, set()).add(card.id)
    
    def index_cards(self, cards: List[CardData]) -> None:
        """Индексирует список карточек."""
        for card in cards:
            self.add_card(card)
    
    def find_similar(
        self,
        card_id: str,
        limit: int = 6,
        min_score: float = None,
        allowed_sources: Optional[List[str]] = None,
    ) -> List[SimilarResult]:
        """
        Находит похожие креативы для данной карточки.
        
        Args:
            card_id: ID текущей карточки
            limit: Максимум результатов
            min_score: Минимальный score (default: MIN_SCORE)
        
        Returns:
            Список SimilarResult, отсортированный по score DESC
        """
        if min_score is None:
            min_score = MIN_SCORE
            
        card = self.cards.get(card_id)
        if not card:
            return []
        
        # Collect candidate IDs and their signals
        candidates: Dict[str, List[Dict]] = {}
        
        def add_signal(candidate_id: str, signal: Signal, detail: str = ""):
            if candidate_id == card_id:
                return
            if candidate_id not in candidates:
                candidates[candidate_id] = []
            candidates[candidate_id].append({
                'signal': signal,
                'weight': self.weights[signal],
                'detail': detail,
            })
        
        # 1. Content hash match
        if card.content_hash and card.content_hash in self._idx_content_hash:
            for cid in self._idx_content_hash[card.content_hash]:
                add_signal(cid, Signal.CONTENT_HASH, f"hash={card.content_hash[:16]}")
        
        # 2. Tracking domain match
        domain = normalize_domain(card.tracking_domain or '')
        if domain and domain in self._idx_domain:
            for cid in self._idx_domain[domain]:
                add_signal(cid, Signal.TRACKING_DOMAIN, f"domain={domain}")
        
        # 3. Buyer cluster match
        if card.buyer_cluster and card.buyer_cluster in self._idx_cluster:
            # Skip META clusters (pure FBID, unknown, etc.) — too generic
            if not card.buyer_cluster.startswith('META_'):
                for cid in self._idx_cluster[card.buyer_cluster]:
                    add_signal(cid, Signal.BUYER_CLUSTER, f"cluster={card.buyer_cluster}")
        
        # 4. Pixel match
        if card.pixel and card.pixel not in GENERIC_PIX and card.pixel in self._idx_pixel:
            for cid in self._idx_pixel[card.pixel]:
                add_signal(cid, Signal.PIXEL, f"pix={card.pixel}")
        
        # 5. 5sub match
        if card.sub5 and card.sub5 not in GENERIC_5SUB and card.sub5 in self._idx_sub5:
            for cid in self._idx_sub5[card.sub5]:
                add_signal(cid, Signal.SUB5, f"5sub={card.sub5}")
        
        # 6. Agency match
        if card.agency and card.agency in self._idx_agency:
            for cid in self._idx_agency[card.agency]:
                add_signal(cid, Signal.AGENCY, f"agency={card.agency}")
        
        # Source-isolation: фильтруем кандидатов по разрешённым источникам
        if allowed_sources is None:
            # default: same source as queried card
            allowed_sources = [card.source or 'spy']
        if allowed_sources and 'all' not in allowed_sources:
            candidates = {
                cid: sigs for cid, sigs in candidates.items()
                if (self.cards.get(cid).source or 'spy') in allowed_sources
            }

        # Score & rank
        results = []
        for cid, signals in candidates.items():
            score = sum(s['weight'] for s in signals)
            
            if score < min_score:
                continue
            
            # Determine primary relationship
            signal_types = {s['signal'] for s in signals}
            relationship = _determine_relationship(signal_types)
            
            # Boost: если совпадает И баер И домен — сильный сигнал
            if Signal.BUYER_CLUSTER in signal_types and Signal.TRACKING_DOMAIN in signal_types:
                score *= 1.3
            # Boost: баер + пиксель = почти наверняка один и тот же запуск
            if Signal.BUYER_CLUSTER in signal_types and Signal.PIXEL in signal_types:
                score *= 1.5
            
            results.append(SimilarResult(
                card_id=cid,
                score=round(score, 1),
                signals=[{
                    'signal': s['signal'].value,
                    'weight': s['weight'],
                    'detail': s['detail'],
                } for s in signals],
                relationship=relationship,
                card_data=_card_to_dict(self.cards.get(cid)),
            ))
        
        # Sort by score DESC, then by date
        results.sort(key=lambda r: (-r.score,))
        
        return results[:limit]
    
    def find_buyer_campaigns(self, card_id: str, limit: int = 20) -> List[SimilarResult]:
        """
        Находит ВСЕ кампании того же баера.
        Полезно для анализа масштаба работы конкретного баера.
        """
        card = self.cards.get(card_id)
        if not card or not card.buyer_cluster:
            return []
        
        if card.buyer_cluster.startswith('META_'):
            return []
        
        results = []
        for cid in self._idx_cluster.get(card.buyer_cluster, set()):
            if cid == card_id:
                continue
            other = self.cards.get(cid)
            if not other:
                continue
            
            signals = [{'signal': 'buyer_cluster', 'weight': 5, 'detail': card.buyer_cluster}]
            score = 5.0
            
            # Bonus signals
            if other.pixel == card.pixel and card.pixel not in GENERIC_PIX:
                signals.append({'signal': 'pixel', 'weight': 7, 'detail': card.pixel})
                score += 7
            if other.sub5 == card.sub5 and card.sub5 not in GENERIC_5SUB:
                signals.append({'signal': '5sub', 'weight': 4, 'detail': card.sub5})
                score += 4
            
            results.append(SimilarResult(
                card_id=cid,
                score=score,
                signals=signals,
                relationship='same_buyer',
                card_data=_card_to_dict(other),
            ))
        
        results.sort(key=lambda r: -r.score)
        return results[:limit]
    
    def get_stats(self) -> Dict:
        """Статистика по индексу."""
        from collections import Counter
        cluster_counts = Counter(c.buyer_cluster for c in self.cards.values() if c.buyer_cluster)
        agency_counts = Counter(c.agency for c in self.cards.values() if c.agency)
        
        return {
            'total_cards': len(self.cards),
            'unique_content_hashes': len(self._idx_content_hash),
            'unique_domains': len(self._idx_domain),
            'unique_clusters': len(self._idx_cluster),
            'unique_pixels': len(self._idx_pixel),
            'top_clusters': cluster_counts.most_common(20),
            'agencies': dict(agency_counts),
        }


# =============================================================================
# HELPERS
# =============================================================================

def _determine_relationship(signal_types: set) -> str:
    """Определяет основной тип связи для UI."""
    if Signal.CONTENT_HASH in signal_types:
        if Signal.TRACKING_DOMAIN in signal_types:
            return "same_creative_same_land"      # Полный дубль
        return "same_creative_diff_land"          # Тот же ролик → другой ленд
    
    if Signal.TRACKING_DOMAIN in signal_types:
        if Signal.BUYER_CLUSTER in signal_types:
            return "same_buyer_same_land"         # Тот же баер, тот же ленд
        return "same_land_diff_buyer"             # Тот же ленд, другой баер
    
    if Signal.BUYER_CLUSTER in signal_types:
        if Signal.PIXEL in signal_types:
            return "same_buyer_same_pixel"        # Тот же баер, тот же пиксель
        return "same_buyer"                       # Тот же баер (по почерку)
    
    if Signal.PIXEL in signal_types:
        return "same_pixel"                       # Тот же пиксель
    
    if Signal.SUB5 in signal_types:
        return "same_adset_group"                 # Та же adset-группа
    
    return "same_agency"                          # Только агентство совпадает


def _card_to_dict(card: Optional[CardData]) -> Optional[Dict]:
    """Converts CardData to dict for API response."""
    if not card:
        return None
    return {
        'id': card.id,
        'tracking_domain': card.tracking_domain,
        'sub4': card.sub4,
        'sub5': card.sub5,
        'pixel': card.pixel,
        'buyer_cluster': card.buyer_cluster,
        'buyer_confidence': card.buyer_confidence,
        'agency': card.agency,
        'geo': card.geo,
    }


# =============================================================================
# RELATIONSHIP LABELS (for UI)
# =============================================================================

RELATIONSHIP_LABELS = {
    "same_creative_same_land":  {
        "en": "Same creative & landing",
        "ru": "Тот же креатив и ленд",
        "icon": "🔴",
        "priority": 1,
    },
    "same_creative_diff_land":  {
        "en": "Same creative, different landing",
        "ru": "Тот же ролик → другой ленд",
        "icon": "🟠",
        "priority": 2,
    },
    "same_buyer_same_land":     {
        "en": "Same buyer, same landing",
        "ru": "Тот же баер, тот же ленд",
        "icon": "🟡",
        "priority": 3,
    },
    "same_buyer_same_pixel":    {
        "en": "Same buyer, same pixel",
        "ru": "Тот же баер, тот же пиксель",
        "icon": "🟢",
        "priority": 4,
    },
    "same_buyer":               {
        "en": "Same buyer (fingerprint match)",
        "ru": "Тот же баер (по почерку 4sub)",
        "icon": "🔵",
        "priority": 5,
    },
    "same_land_diff_buyer":     {
        "en": "Same landing, different buyer",
        "ru": "Тот же ленд, другой баер",
        "icon": "⚪",
        "priority": 6,
    },
    "same_pixel":               {
        "en": "Same pixel",
        "ru": "Тот же пиксель",
        "icon": "🟣",
        "priority": 7,
    },
    "same_adset_group":         {
        "en": "Same adset group",
        "ru": "Та же 5sub группа",
        "icon": "⚫",
        "priority": 8,
    },
    "same_agency":              {
        "en": "Same agency/seller",
        "ru": "Тот же селлер",
        "icon": "⬜",
        "priority": 9,
    },
}


# =============================================================================
# FASTAPI INTEGRATION EXAMPLE
# =============================================================================

FASTAPI_EXAMPLE = '''
# --- routes/similar.py ---

from fastapi import APIRouter, Query
from similar_engine import SimilarEngine, CardData, RELATIONSHIP_LABELS

router = APIRouter()
engine = SimilarEngine()  # singleton, init at startup

@router.on_event("startup")
async def load_index():
    """Загрузка индекса из БД при старте."""
    from database import get_all_items
    items = await get_all_items()
    for item in items:
        engine.add_card(CardData(
            id=str(item['id']),
            content_hash=item.get('content_hash'),
            tracking_domain=item.get('tracking_domain'),
            sub4=item.get('sub4'),
            sub5=item.get('sub5'),
            pixel=item.get('pix'),
            created_at=item.get('created_at'),
        ))

@router.get("/api/similar/{item_id}")
async def get_similar(item_id: str, limit: int = Query(6, le=20)):
    results = engine.find_similar(item_id, limit=limit)
    return {
        "item_id": item_id,
        "count": len(results),
        "results": [
            {
                "id": r.card_id,
                "score": r.score,
                "relationship": r.relationship,
                "relationship_label": RELATIONSHIP_LABELS[r.relationship]["ru"],
                "relationship_icon": RELATIONSHIP_LABELS[r.relationship]["icon"],
                "signals": r.signals,
                "card": r.card_data,
            }
            for r in results
        ],
    }

@router.get("/api/buyer-campaigns/{item_id}")
async def get_buyer_campaigns(item_id: str, limit: int = Query(20, le=100)):
    """Все кампании того же баера."""
    results = engine.find_buyer_campaigns(item_id, limit=limit)
    card = engine.cards.get(item_id)
    return {
        "item_id": item_id,
        "buyer_cluster": card.buyer_cluster if card else None,
        "agency": card.agency if card else None,
        "count": len(results),
        "campaigns": [
            {
                "id": r.card_id,
                "score": r.score,
                "card": r.card_data,
            }
            for r in results
        ],
    }

# --- Webhook: при добавлении нового item ---
@router.post("/api/items/webhook")
async def on_new_item(item: dict):
    """Добавляет новый item в индекс."""
    engine.add_card(CardData(
        id=str(item['id']),
        content_hash=item.get('content_hash'),
        tracking_domain=item.get('tracking_domain'),
        sub4=item.get('sub4'),
        sub5=item.get('sub5'),
        pixel=item.get('pix'),
    ))
    return {"status": "indexed"}
'''


# =============================================================================
# TESTS
# =============================================================================

def run_tests():
    """Встроенные тесты движка."""
    
    engine = SimilarEngine()
    
    # Create test cards
    cards = [
        CardData(id="1", content_hash="abc123", tracking_domain="chopdeuk.com",
                 sub4="DE ADSA 17 1572850050691788 TAG CTLPLM1 PXPLM1",
                 sub5="zibo2", pixel="1482308046593894"),
        
        CardData(id="2", content_hash="abc123", tracking_domain="other-domain.com",
                 sub4="DE ADSA 120 1224722253127263 TAG CTLPLM1 PXPLM1",
                 sub5="zibo2", pixel="1482308046593894"),
        
        CardData(id="3", content_hash="def456", tracking_domain="chopdeuk.com",
                 sub4="1460020542207172 +-+ 35 +-+ Dep +-+ 1 10 1 +-+ 1000$",
                 sub5="CM13", pixel="9999999"),
        
        CardData(id="4", content_hash="ghi789", tracking_domain="another.com",
                 sub4="DK ADSA 54 4112087969057486 TAG CTLPLM1 PXPLM1",
                 sub5="zibo2", pixel="1482308046593894"),
        
        CardData(id="5", content_hash="jkl012", tracking_domain="bildaihub.com",
                 sub4="V_INfu759-inst/fb/123-1",
                 sub5="singh1", pixel="5555555"),
        
        CardData(id="6", content_hash="mno345", tracking_domain="preiwsl.com",
                 sub4="V_INfu420-inst/fb/113/300-3",
                 sub5="other", pixel="6666666"),
    ]
    
    engine.index_cards(cards)
    
    # Test 1: Card 1 should find card 2 (content_hash + cluster + pixel + 5sub)
    results = engine.find_similar("1", limit=10, min_score=0)
    ids = [r.card_id for r in results]
    
    assert "2" in ids, f"Card 2 should be similar to 1 (same hash + cluster + pixel). Got: {ids}"
    r2 = next(r for r in results if r.card_id == "2")
    assert r2.score > 20, f"Card 2 score should be high (multiple signals). Got: {r2.score}"
    
    # Test 2: Card 1 → Card 3 via domain
    assert "3" in ids, f"Card 3 should be similar to 1 (same domain). Got: {ids}"
    
    # Test 3: Card 1 → Card 4 via cluster + pixel + 5sub (not domain or hash)
    assert "4" in ids, f"Card 4 should be similar to 1 (same cluster + pixel). Got: {ids}"
    
    # Test 4: Card 5 → Card 6 via cluster only (V_geo)
    results_5 = engine.find_similar("5", limit=10, min_score=0)
    ids_5 = [r.card_id for r in results_5]
    assert "6" in ids_5, f"Card 6 should be similar to 5 (same V_geo cluster). Got: {ids_5}"
    
    # Test 5: Card 2 highest score for card 1
    assert results[0].card_id == "2", f"Card 2 should be #1 similar to Card 1. Got: {results[0].card_id}"
    
    # Test 6: Relationship types
    r2 = next(r for r in results if r.card_id == "2")
    assert r2.relationship == "same_creative_diff_land", f"Card 2 relationship should be same_creative_diff_land. Got: {r2.relationship}"
    
    r3 = next(r for r in results if r.card_id == "3")
    assert "land" in r3.relationship, f"Card 3 should have land-based relationship. Got: {r3.relationship}"
    
    # Test 7: Buyer campaigns
    buyer_results = engine.find_buyer_campaigns("1", limit=10)
    buyer_ids = [r.card_id for r in buyer_results]
    assert "2" in buyer_ids and "4" in buyer_ids, f"Cards 2,4 should be same buyer as 1. Got: {buyer_ids}"
    assert "3" not in buyer_ids, f"Card 3 (different buyer) should NOT be in buyer campaigns. Got: {buyer_ids}"
    
    # Test 8: Stats
    stats = engine.get_stats()
    assert stats['total_cards'] == 6
    assert stats['unique_clusters'] > 0
    
    print(f"All {8} tests passed ✅")
    return True


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_tests()
    elif len(sys.argv) > 1 and sys.argv[1] == "--example":
        print(FASTAPI_EXAMPLE)
    else:
        print(__doc__)
        print("\nCommands:")
        print("  --test     Run tests")
        print("  --example  Show FastAPI integration example")
