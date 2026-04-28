"""
Unique Creatives Filter
=======================

Фильтр "Unique only" для spy-инструмента.
Определяет уникальность креатива по первым 20 словам транскрипции.

Три уровня уникальности:
1. EXACT  — content_hash (первые 20 слов) встречается ровно 1 раз
2. FIRST  — первое появление дублированного content_hash (самое раннее по дате)
3. DUPLICATE — повторное появление уже известного content_hash

Режимы фильтра "Unique only":
- strict:  только EXACT (креативы, которые не копировал НИКТО)
- normal:  EXACT + FIRST (каждый текст показывается один раз)
- off:     все карточки

Использование:
    from unique_filter import UniqueIndex, compute_content_hash

    index = UniqueIndex()
    index.build(items)  # [{id, content_hash, created_at, ...}]

    # Фильтр
    filtered_ids = index.get_unique_ids(mode="normal")

    # Проверка одной карточки
    info = index.check(item_id)
    # → {is_unique: True, mode: "exact", copies: 0, first_seen_id: "123"}

    # SQL-совместимый подход
    hash_value = compute_content_hash("Текст транскрипции здесь...")
"""

import hashlib
import re
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from collections import defaultdict
from difflib import SequenceMatcher

__version__ = "1.0.0"


# =============================================================================
# CONTENT HASH
# =============================================================================

def normalize_text(text: str) -> str:
    """
    Нормализует текст перед хэшированием.
    Убирает пунктуацию, лишние пробелы, приводит к lowercase.
    """
    if not text:
        return ""
    # Убрать HTML теги если остались
    t = re.sub(r'<[^>]+>', ' ', text)
    # Убрать HTML entities
    t = re.sub(r'&\w+;', ' ', t)
    # Убрать пунктуацию (оставить буквы, цифры, пробелы)
    t = re.sub(r'[^\w\s]', ' ', t)
    # Множественные пробелы в один
    t = re.sub(r'\s+', ' ', t).strip()
    # Lowercase
    t = t.lower()
    return t


def extract_first_n_words(text: str, n: int = 20) -> str:
    """Извлекает первые N слов из нормализованного текста."""
    normalized = normalize_text(text)
    words = normalized.split()[:n]
    return ' '.join(words)


def compute_content_hash(text: str, n_words: int = 20) -> str:
    """
    Вычисляет content_hash — MD5 от первых N слов нормализованного текста.
    
    Это основной ключ дедупликации.
    Совместим с существующим полем content_hash в БД.
    
    Args:
        text: Полный текст транскрипции
        n_words: Количество первых слов (default: 20)
    
    Returns:
        32-символьный hex MD5 hash
    """
    first_n = extract_first_n_words(text, n_words)
    if not first_n:
        return ""
    return hashlib.md5(first_n.encode('utf-8')).hexdigest()


# =============================================================================
# FUZZY MATCHING (для обнаружения near-duplicates)
# =============================================================================

def similarity_ratio(text1: str, text2: str) -> float:
    """
    Вычисляет степень схожести двух текстов (0.0 - 1.0).
    Используется для обнаружения near-duplicates.
    """
    t1 = extract_first_n_words(text1, 20)
    t2 = extract_first_n_words(text2, 20)
    if not t1 or not t2:
        return 0.0
    return SequenceMatcher(None, t1, t2).ratio()


def find_near_duplicates(
    texts: Dict[str, str],
    threshold: float = 0.85
) -> List[Tuple[str, str, float]]:
    """
    Находит near-duplicate пары среди текстов.
    
    Args:
        texts: {item_id: transcription_text}
        threshold: минимальная схожесть (0.85 = 85%)
    
    Returns:
        [(id1, id2, similarity), ...] отсортировано по similarity DESC
    
    ⚠️ O(n²) — использовать только для анализа, не для runtime фильтрации.
    """
    items = list(texts.items())
    # Pre-compute normalized first-20
    normalized = {k: extract_first_n_words(v, 20) for k, v in items}
    
    # Group by first 3 words for blocking (reduce O(n²))
    blocks = defaultdict(list)
    for item_id, norm in normalized.items():
        words = norm.split()[:3]
        block_key = ' '.join(words) if words else ''
        blocks[block_key].append(item_id)
    
    pairs = []
    seen = set()
    for block_ids in blocks.values():
        for i, id1 in enumerate(block_ids):
            for id2 in block_ids[i+1:]:
                pair_key = tuple(sorted([id1, id2]))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                ratio = SequenceMatcher(
                    None, normalized[id1], normalized[id2]
                ).ratio()
                if ratio >= threshold:
                    pairs.append((id1, id2, round(ratio, 3)))
    
    pairs.sort(key=lambda x: -x[2])
    return pairs


# =============================================================================
# UNIQUE INDEX
# =============================================================================

@dataclass
class ItemInfo:
    """Информация о карточке в индексе."""
    id: str
    content_hash: str
    created_at: Optional[str] = None
    first20: str = ""
    source: str = 'spy'


class UniqueIndex:
    """
    Индекс уникальности креативов.
    
    Строится один раз при старте, обновляется при добавлении новых items.
    Поддерживает три режима фильтрации.
    
    Usage:
        index = UniqueIndex()
        index.build(items)
        
        # Фильтрация
        unique_ids = index.get_unique_ids(mode="normal")
        
        # Проверка одной карточки
        info = index.check("card_123")
        
        # Добавление нового item
        index.add_item(new_item)
    """
    
    def __init__(self):
        self.items: Dict[str, ItemInfo] = {}
        # content_hash → [item_ids] (sorted by created_at)
        self._hash_index: Dict[str, List[str]] = defaultdict(list)
        self._built = False
    
    def build(self, items: List[Dict]) -> 'UniqueIndex':
        """
        Строит индекс из списка items.
        
        Args:
            items: список dict с полями:
                id: str (обязательно)
                content_hash: str (обязательно) — уже вычисленный хэш
                created_at: str (опционально) — дата для сортировки
                transcription: str (опционально) — текст для first20
        
        Returns:
            self (для chaining)
        """
        self.items.clear()
        self._hash_index.clear()
        
        for item in items:
            self._add_item_internal(item)
        
        # Sort each hash group by date
        for h in self._hash_index:
            self._hash_index[h].sort(
                key=lambda iid: self.items[iid].created_at or ''
            )
        
        self._built = True
        return self
    
    def add_item(self, item: Dict) -> Dict:
        """
        Добавляет один item в индекс.
        Возвращает информацию об уникальности.
        
        Returns:
            {is_unique: bool, copies: int, first_seen_id: str|None}
        """
        self._add_item_internal(item)
        
        h = item.get('content_hash', '')
        if not h:
            return {'is_unique': True, 'copies': 0, 'first_seen_id': None}
        
        group = self._hash_index.get(h, [])
        return {
            'is_unique': len(group) == 1,
            'copies': len(group) - 1,
            'first_seen_id': group[0] if group else None,
        }
    
    def _add_item_internal(self, item: Dict):
        """Internal: добавляет item без возврата."""
        item_id = str(item['id'])
        content_hash = item.get('content_hash', '')
        
        if not content_hash and item.get('transcription'):
            content_hash = compute_content_hash(item['transcription'])
        
        info = ItemInfo(
            id=item_id,
            content_hash=content_hash,
            created_at=item.get('created_at'),
            first20=extract_first_n_words(item.get('transcription', ''), 20),
            source=item.get('source', 'spy'),
        )
        
        self.items[item_id] = info
        
        if content_hash:
            if item_id not in self._hash_index[content_hash]:
                self._hash_index[content_hash].append(item_id)
    
    def check(self, item_id: str) -> Dict:
        """
        Проверяет уникальность конкретной карточки.
        
        Returns:
            {
                is_unique: bool,      — уникален ли текст
                status: str,          — "exact" | "first" | "duplicate"  
                copies: int,          — количество копий этого текста
                copy_ids: list,       — ID всех копий
                first_seen_id: str,   — ID первого появления
                is_first: bool,       — это первое появление?
            }
        """
        info = self.items.get(item_id)
        if not info:
            return {
                'is_unique': False,
                'status': 'unknown',
                'copies': 0,
                'copy_ids': [],
                'first_seen_id': None,
                'is_first': False,
            }
        
        if not info.content_hash:
            return {
                'is_unique': True,
                'status': 'no_hash',
                'copies': 0,
                'copy_ids': [],
                'first_seen_id': item_id,
                'is_first': True,
            }
        
        group = self._hash_index.get(info.content_hash, [])
        copies = len(group) - 1
        first_id = group[0] if group else item_id
        is_first = (first_id == item_id)
        
        if copies == 0:
            status = "exact"
        elif is_first:
            status = "first"
        else:
            status = "duplicate"
        
        return {
            'is_unique': copies == 0,
            'status': status,
            'copies': copies,
            'copy_ids': [iid for iid in group if iid != item_id],
            'first_seen_id': first_id,
            'is_first': is_first,
        }
    
    def get_unique_ids(self, mode: str = "normal", allowed_sources: Optional[List[str]] = None) -> Set[str]:
        """
        Возвращает ID карточек для фильтра "Unique only".
        
        Modes:
            "strict" — только EXACT уникальные (текст не повторяется нигде)
            "normal" — EXACT + FIRST (один представитель от каждого текста)
            "off"    — все карточки
        
        Returns:
            Set[str] — множество ID карточек, прошедших фильтр
        """
        # Pre-filter items by allowed_sources
        if allowed_sources and 'all' not in allowed_sources:
            allowed = {iid for iid, info in self.items.items() if info.source in allowed_sources}
        else:
            allowed = set(self.items.keys())

        if mode == "off":
            return allowed

        result = set()

        for h, group in self._hash_index.items():
            scoped = [iid for iid in group if iid in allowed]
            if not scoped:
                continue
            if mode == "strict":
                if len(scoped) == 1:
                    result.add(scoped[0])
            elif mode == "normal":
                result.add(scoped[0])

        # Items без хэша (нет транскрипции) — все из разрешённых
        for iid, info in self.items.items():
            if not info.content_hash and iid in allowed:
                result.add(iid)

        return result
    def get_stats(self) -> Dict:
        """Статистика по индексу."""
        total = len(self.items)
        unique_hashes = len(self._hash_index)
        
        exact = sum(1 for g in self._hash_index.values() if len(g) == 1)
        duplicated = sum(1 for g in self._hash_index.values() if len(g) > 1)
        total_copies = sum(len(g) - 1 for g in self._hash_index.values() if len(g) > 1)
        
        # Distribution
        from collections import Counter
        size_dist = Counter(len(g) for g in self._hash_index.values())
        
        # Top duplicated
        top_dupes = sorted(
            [(h, len(g)) for h, g in self._hash_index.items() if len(g) > 1],
            key=lambda x: -x[1]
        )[:10]
        
        top_dupes_detail = []
        for h, count in top_dupes:
            group = self._hash_index[h]
            sample = self.items.get(group[0])
            top_dupes_detail.append({
                'hash': h,
                'count': count,
                'first20': sample.first20[:80] if sample else '',
            })
        
        return {
            'total_items': total,
            'unique_hashes': unique_hashes,
            'exact_unique': exact,
            'duplicated_groups': duplicated,
            'total_copies': total_copies,
            'duplicate_rate': round(total_copies / total * 100, 1) if total else 0,
            'after_strict_filter': exact,
            'after_normal_filter': unique_hashes,
            'size_distribution': dict(sorted(size_dist.items())),
            'top_duplicated': top_dupes_detail,
        }


# =============================================================================
# SQL HELPERS (для интеграции с БД)
# =============================================================================

POSTGRES_UNIQUE_QUERY = """
-- Unique only (normal mode): один представитель от каждого content_hash
-- Используется как подзапрос или CTE

WITH ranked AS (
    SELECT 
        *,
        ROW_NUMBER() OVER (
            PARTITION BY content_hash 
            ORDER BY created_at ASC
        ) AS rn
    FROM items
    WHERE content_hash IS NOT NULL 
      AND content_hash != ''
)
SELECT * FROM ranked WHERE rn = 1

UNION ALL

-- Items без content_hash (нет транскрипции) — всегда показываем
SELECT *, 1 AS rn FROM items 
WHERE content_hash IS NULL OR content_hash = '';
"""

POSTGRES_STRICT_QUERY = """
-- Unique only (strict mode): только тексты без дублей
SELECT * FROM items
WHERE content_hash IN (
    SELECT content_hash 
    FROM items 
    WHERE content_hash IS NOT NULL AND content_hash != ''
    GROUP BY content_hash 
    HAVING COUNT(*) = 1
)
OR content_hash IS NULL 
OR content_hash = '';
"""

POSTGRES_ADD_UNIQUE_FLAG = """
-- Добавить колонку is_unique для быстрой фильтрации
ALTER TABLE items ADD COLUMN IF NOT EXISTS is_unique BOOLEAN DEFAULT TRUE;
ALTER TABLE items ADD COLUMN IF NOT EXISTS is_first_seen BOOLEAN DEFAULT TRUE;
ALTER TABLE items ADD COLUMN IF NOT EXISTS duplicate_count INTEGER DEFAULT 0;

-- Обновить флаги
WITH hash_stats AS (
    SELECT 
        content_hash,
        COUNT(*) as cnt,
        MIN(created_at) as first_date
    FROM items
    WHERE content_hash IS NOT NULL AND content_hash != ''
    GROUP BY content_hash
)
UPDATE items SET
    is_unique = (hs.cnt = 1),
    is_first_seen = (items.created_at = hs.first_date),
    duplicate_count = hs.cnt - 1
FROM hash_stats hs
WHERE items.content_hash = hs.content_hash;

-- Индекс для быстрой фильтрации
CREATE INDEX IF NOT EXISTS idx_items_unique ON items (is_unique);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items (is_first_seen);
CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items (content_hash);
"""


# =============================================================================
# FASTAPI INTEGRATION
# =============================================================================

FASTAPI_EXAMPLE = '''
# --- В существующий endpoint списка карточек ---

from unique_filter import UniqueIndex

# Singleton — инициализировать при старте
unique_index = UniqueIndex()

@app.on_event("startup")
async def build_unique_index():
    items = await db.fetch_all("SELECT id, content_hash, created_at FROM items")
    unique_index.build([dict(r) for r in items])

@app.get("/api/items")
async def list_items(
    unique_only: bool = False,        # ← НОВЫЙ параметр
    unique_mode: str = "normal",      # strict | normal
    # ... остальные фильтры
):
    query = build_base_query(...)
    
    if unique_only:
        unique_ids = unique_index.get_unique_ids(mode=unique_mode)
        # Добавить фильтр в query
        query = query.where(items.c.id.in_(unique_ids))
        # ИЛИ через SQL:
        # query += " AND is_first_seen = TRUE"
    
    results = await db.fetch_all(query)
    return results

# --- Endpoint статистики ---
@app.get("/api/stats/unique")
async def unique_stats():
    return unique_index.get_stats()

# --- Webhook: при добавлении нового item ---
@app.post("/api/items/webhook")  
async def on_new_item(item: dict):
    info = unique_index.add_item(item)
    # info.is_unique → можно отправить уведомление "Новый уникальный креатив!"
    return info
'''


# =============================================================================
# TESTS
# =============================================================================

def run_tests():
    """Встроенные тесты."""
    
    # Test 1: compute_content_hash
    h1 = compute_content_hash("Привет мир это тестовый текст для проверки хэша из двадцати слов минимум нужно набрать столько слов чтобы проверить работу функции")
    h2 = compute_content_hash("Привет мир это тестовый текст для проверки хэша из двадцати слов минимум нужно набрать столько слов чтобы проверить работу функции но конец другой")
    h3 = compute_content_hash("Совсем другой текст который не похож на предыдущий ни одним словом")
    
    assert h1 == h2, "Same first 20 words should produce same hash"
    assert h1 != h3, "Different text should produce different hash"
    assert h1 != "", "Hash should not be empty"
    
    # Test 2: normalize
    assert normalize_text("  Привет,  мир!  ") == "привет мир"
    assert normalize_text("<b>Hello</b> &amp; world") == "hello world"
    
    # Test 3: UniqueIndex
    index = UniqueIndex()
    items = [
        {'id': '1', 'content_hash': 'aaa', 'created_at': '2026-01-01'},
        {'id': '2', 'content_hash': 'aaa', 'created_at': '2026-01-02'},  # duplicate
        {'id': '3', 'content_hash': 'aaa', 'created_at': '2026-01-03'},  # duplicate
        {'id': '4', 'content_hash': 'bbb', 'created_at': '2026-01-01'},  # unique
        {'id': '5', 'content_hash': 'ccc', 'created_at': '2026-01-01'},
        {'id': '6', 'content_hash': 'ccc', 'created_at': '2026-01-02'},  # duplicate
        {'id': '7', 'content_hash': '',    'created_at': '2026-01-01'},  # no hash
    ]
    index.build(items)
    
    # Strict: only exact unique
    strict = index.get_unique_ids("strict")
    assert strict == {'4', '7'}, f"Strict should return only truly unique. Got: {strict}"
    
    # Normal: first of each group
    normal = index.get_unique_ids("normal")
    assert '1' in normal, "First of 'aaa' group should be included"
    assert '2' not in normal, "Second of 'aaa' group should NOT be included"
    assert '4' in normal, "Unique 'bbb' should be included"
    assert '5' in normal, "First of 'ccc' should be included"
    assert '6' not in normal, "Second of 'ccc' should NOT be included"
    assert '7' in normal, "No-hash item should be included"
    assert len(normal) == 4, f"Normal should return 4 items. Got: {len(normal)}"
    
    # Check individual
    c1 = index.check('1')
    assert c1['status'] == 'first', f"Item 1 should be 'first'. Got: {c1['status']}"
    assert c1['copies'] == 2
    assert c1['is_first'] == True
    
    c2 = index.check('2')
    assert c2['status'] == 'duplicate', f"Item 2 should be 'duplicate'. Got: {c2['status']}"
    assert c2['is_first'] == False
    
    c4 = index.check('4')
    assert c4['status'] == 'exact'
    assert c4['is_unique'] == True
    assert c4['copies'] == 0
    
    # Stats
    stats = index.get_stats()
    assert stats['total_items'] == 7
    assert stats['exact_unique'] == 1  # only 'bbb'
    assert stats['duplicated_groups'] == 2  # 'aaa' and 'ccc'
    assert stats['total_copies'] == 3  # 2 extra aaa + 1 extra ccc
    
    # Test 4: add_item
    result = index.add_item({'id': '8', 'content_hash': 'ddd'})
    assert result['is_unique'] == True
    
    result = index.add_item({'id': '9', 'content_hash': 'aaa'})
    assert result['is_unique'] == False
    assert result['copies'] == 3  # now 4 total - 1
    
    # Test 5: similarity
    r = similarity_ratio(
        "Привет дорогие друзья сегодня я расскажу вам о том как заработать деньги используя новую программу которая была создана специально для вас",
        "Привет дорогие друзья сегодня я расскажу вам о том как заработать деньги используя новую систему которая была разработана специально для вас",
    )
    assert r > 0.8, f"Similar texts should have ratio > 0.8. Got: {r}"
    
    r2 = similarity_ratio(
        "Совершенно другой текст про погоду и природу",
        "Привет дорогие друзья сегодня расскажу",
    )
    assert r2 < 0.5, f"Different texts should have low ratio. Got: {r2}"
    
    print("All 12 tests passed ✅")
    return True


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_tests()
    elif len(sys.argv) > 1 and sys.argv[1] == "--sql":
        print("=== NORMAL MODE ===")
        print(POSTGRES_UNIQUE_QUERY)
        print("\n=== STRICT MODE ===")
        print(POSTGRES_STRICT_QUERY)
        print("\n=== ADD FLAGS ===")
        print(POSTGRES_ADD_UNIQUE_FLAG)
    elif len(sys.argv) > 1 and sys.argv[1] == "--example":
        print(FASTAPI_EXAMPLE)
    else:
        print(__doc__)
