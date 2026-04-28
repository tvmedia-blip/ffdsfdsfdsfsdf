"""
Obsidian Sync — one-way SQL → vault pipeline.

Architecture:
- speeches/{geo}/{hash}_{slug}.md  — one per unique content_hash
- entities/{buyers|domains|pixels|geos}/{slug}.md  — one per entity
- sightings/{YYYY}/{MM}/{DD}/{uid}.md  — one per item
- _index/ — MOC navigation

Contracts:
- All writes atomic (tmp → rename)
- User sections preserved between updates
- Safe filenames (whitelist chars, NFC, 100 char cap)
- YAML safe_dump for frontmatter
- Bootstrap lock prevents race with live sync
"""
import os, re, unicodedata, hashlib, sqlite3, logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import yaml

log = logging.getLogger(__name__)

VAULT_ROOT = Path(__file__).parent / "vault"
LOCK_FILE = VAULT_ROOT / ".bootstrap.lock"

SAFE_CHARS_RE = re.compile(r'[^a-zA-Z0-9_.-]')
UNSAFE_WIKILINK = {'|': '\uFF5C', '[': '\uFF3B', ']': '\uFF3D', '\n': ' ', '\r': ''}

USER_START = "<!-- USER -->"
USER_END = "<!-- /USER -->"
AUTO_START = "<!-- AUTO-START -->"
AUTO_END = "<!-- AUTO-END -->"


# =====================================================
# UTILITIES
# =====================================================

def safe_filename(name, max_len=100):
    if not name:
        return "_empty_"
    name = unicodedata.normalize('NFC', str(name))
    name = SAFE_CHARS_RE.sub('_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if len(name) > max_len:
        name = name[:max_len].rstrip('_')
    return name or "_empty_"


def normalize_entity_name(name):
    if not name:
        return ""
    return unicodedata.normalize('NFC', str(name)).strip()


def entity_slug(name):
    return safe_filename(normalize_entity_name(name).lower(), max_len=80)


def wikilink_escape(s):
    if not s:
        return ""
    for bad, repl in UNSAFE_WIKILINK.items():
        s = s.replace(bad, repl)
    return s.strip()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def build_frontmatter(data):
    clean = {k: v for k, v in data.items() if v is not None and v != ''}
    dumped = yaml.safe_dump(clean, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return "---\n" + dumped + "---\n"


def preserve_user_section(content):
    if not content:
        return f"\n{USER_START}\n\n{USER_END}\n"
    start = content.find(USER_START)
    end = content.find(USER_END)
    if start == -1 or end == -1:
        return f"\n{USER_START}\n\n{USER_END}\n"
    return content[start:end + len(USER_END)] + "\n"


def bootstrap_lock_active():
    return LOCK_FILE.exists()


def acquire_bootstrap_lock():
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch()


def release_bootstrap_lock():
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


# =====================================================
# FILE BUILDERS
# =====================================================

def speech_file_path(content_hash, geo, first_sub4=None):
    short = (content_hash or 'unknown')[:8]
    geo_folder = safe_filename((geo or 'unknown').upper(), 10)
    slug_part = entity_slug(first_sub4 or 'no_sub4')[:30] if first_sub4 else 'no_sub4'
    filename = f"{short}_{geo_folder}_{slug_part}.md"
    return VAULT_ROOT / "speeches" / geo_folder / filename


def entity_file_path(entity_type, name):
    return VAULT_ROOT / "entities" / entity_type / f"{entity_slug(name)}.md"


def sighting_file_path(uid, created_at):
    # created_at: 'YYYY-MM-DD HH:MM:SS'
    date = (created_at or '')[:10]
    y, m, d = (date.split('-') + ['00', '00', '00'])[:3]
    return VAULT_ROOT / "sightings" / y / m / d / f"{safe_filename(uid, 40)}.md"


def render_speech_md(speech_data, existing_content=""):
    """speech_data: dict with content_hash, geo, translation, etc."""
    fm = {
        'type': 'speech',
        'content_hash': speech_data['content_hash'],
        'geo': speech_data.get('geo') or 'XX',
        'first_seen': speech_data.get('first_seen'),
        'last_seen': speech_data.get('last_seen'),
        'lifespan_days': speech_data.get('lifespan_days', 0),
        'sightings_count': speech_data.get('sightings_count', 1),
        'unique_domains': speech_data.get('unique_domains', []),
        'unique_buyers': speech_data.get('unique_buyers', []),
        'pixel': speech_data.get('pixel'),
        'status': speech_data.get('status', 'unknown'),
        'tags': speech_data.get('tags', []),
    }

    title = (speech_data.get('translation') or '')[:80].split('\n')[0].strip()
    if not title:
        title = f"Speech {speech_data['content_hash'][:8]}"

    user_section = preserve_user_section(existing_content)

    # Build entity links
    buyers = speech_data.get('unique_buyers') or []
    domains = speech_data.get('unique_domains') or []
    pixel = speech_data.get('pixel')
    geo = speech_data.get('geo')

    buyer_links = ', '.join(f"[[entities/buyers/{entity_slug(b)}|{wikilink_escape(b)}]]" for b in buyers) or "_none_"
    domain_links = ', '.join(f"[[entities/domains/{entity_slug(d)}|{wikilink_escape(d)}]]" for d in domains) or "_none_"
    pixel_link = f"[[entities/pixels/{entity_slug(pixel)}|{wikilink_escape(pixel)}]]" if pixel and pixel != 'unknown' else "_none_"
    geo_link = f"[[entities/geos/{entity_slug(geo)}|{wikilink_escape(geo)}]]" if geo else "_none_"

    body = f"""{AUTO_START}
# {title}

## Translation

{speech_data.get('translation') or '_no translation_'}

## Original

{speech_data.get('original_text') or '_no original_'}

## Tracking

- **Buyer(s):** {buyer_links}
- **Domain(s):** {domain_links}
- **Pixel:** {pixel_link}
- **Geo:** {geo_link}

## Sightings

```dataview
LIST
FROM "sightings"
WHERE content_hash = "{speech_data['content_hash']}"
SORT file.name DESC
LIMIT 20
```

{AUTO_END}

{user_section}
"""
    return build_frontmatter(fm) + body


def render_entity_md(entity_type, name, existing_content=""):
    """Simple entity page with dataview queries to speeches/sightings."""
    slug = entity_slug(name)
    fm = {
        'type': f'entity-{entity_type[:-1]}',  # entity-buyer, entity-domain...
        'name': normalize_entity_name(name),
        'slug': slug,
    }

    user_section = preserve_user_section(existing_content)

    body = f"""{AUTO_START}
# {normalize_entity_name(name)}

_{entity_type.rstrip('s').capitalize()} entity page_

## Speeches

```dataview
TABLE WITHOUT ID file.link AS Speech, geo, lifespan_days, status
FROM "speeches"
WHERE contains(unique_{entity_type}, "{normalize_entity_name(name)}")
SORT lifespan_days DESC
LIMIT 50
```

## Sightings Timeline

```dataview
TABLE WITHOUT ID date, tracking_domain
FROM "sightings"
WHERE {entity_type[:-1]} = "{normalize_entity_name(name)}"
SORT date DESC
LIMIT 30
```

{AUTO_END}

{user_section}
"""
    return build_frontmatter(fm) + body


def render_sighting_md(item):
    """item: dict from SQL row."""
    fm = {
        'type': 'sighting',
        'item_id': item['id'],
        'uid': item.get('uid'),
        'content_hash': item.get('content_hash'),
        'date': (item.get('created_at') or '')[:19],
        'geo': item.get('geo'),
        'buyer': item.get('buyer_name') or '',
        'tracking_domain': item.get('tracking_domain') or '',
        'pixel': item.get('pix') or '',
        'sub4': (item.get('sub4') or '')[:200],
        'sub5': (item.get('sub5') or '')[:200],
        'fb_url': item.get('fb_url') or '',
    }

    # Wikilink to speech
    speech_short = (item.get('content_hash') or 'unknown')[:8]
    speech_geo = safe_filename((item.get('geo') or 'unknown').upper(), 10)
    speech_slug = entity_slug(item.get('sub4') or 'no_sub4')[:30]
    speech_link = f"[[speeches/{speech_geo}/{speech_short}_{speech_geo}_{speech_slug}|speech {speech_short}]]"

    buyer = item.get('buyer_name') or ''
    domain = item.get('tracking_domain') or ''
    pixel = item.get('pix') or ''
    geo = item.get('geo') or ''

    links = []
    if buyer:
        links.append(f"- [[entities/buyers/{entity_slug(buyer)}|{wikilink_escape(buyer)}]]")
    if domain:
        links.append(f"- [[entities/domains/{entity_slug(domain)}|{wikilink_escape(domain)}]]")
    if pixel and pixel != 'unknown':
        links.append(f"- [[entities/pixels/{entity_slug(pixel)}|{wikilink_escape(pixel)}]]")
    if geo:
        links.append(f"- [[entities/geos/{entity_slug(geo)}|{wikilink_escape(geo)}]]")

    body = f"""# Sighting {item.get('uid', item['id'])}

{speech_link}

**Date:** {fm['date']}

## Links

{chr(10).join(links) or '_none_'}

## Offer URL

{item.get('tracking_url') or '_none_'}
"""
    return build_frontmatter(fm) + body


def render_index_readme(stats):
    fm = {'type': 'index'}
    body = f"""# SPY Vault

Total speeches: {stats.get('speeches', '?')}
Total sightings: {stats.get('sightings', '?')}
Total buyers: {stats.get('buyers', '?')}
Total domains: {stats.get('domains', '?')}

## Navigation

- [[_index/buyers|All Buyers]]
- [[_index/domains|All Domains]]
- [[_index/geos|Geo Dashboard]]
- [[_index/timeline|Timeline]]

## Top Running Speeches

```dataview
TABLE WITHOUT ID file.link AS Speech, geo, lifespan_days, sightings_count
FROM "speeches"
WHERE status = "running"
SORT lifespan_days DESC
LIMIT 20
```

## Recent Sightings

```dataview
LIST
FROM "sightings"
SORT date DESC
LIMIT 20
```
"""
    return build_frontmatter(fm) + body


# =====================================================
# SYNC OPERATIONS
# =====================================================

def upsert_speech(speech_data):
    path = speech_file_path(
        speech_data['content_hash'], speech_data.get('geo'),
        speech_data.get('first_sub4')
    )
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding='utf-8')
        except Exception:
            pass
    content = render_speech_md(speech_data, existing)
    atomic_write(path, content)
    return path


def upsert_entity(entity_type, name):
    if not name or name == 'unknown':
        return None
    path = entity_file_path(entity_type, name)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding='utf-8')
        except Exception:
            pass
    content = render_entity_md(entity_type, name, existing)
    atomic_write(path, content)
    return path


def write_sighting(item):
    path = sighting_file_path(item.get('uid') or str(item['id']), item.get('created_at'))
    content = render_sighting_md(item)
    atomic_write(path, content)
    return path


def sync_item(item, speech_aggregates=None):
    """Main entry point: sync one item (sighting + speech + entities).

    Skip translator-bot items — vault только для SPY-карточек.
    If bootstrap lock active, skip (caller will retry or batch later).
    speech_aggregates: optional precomputed aggregate from bootstrap loop.
    """
    # Translator-only items не синкаются в vault
    if isinstance(item, dict) and item.get('bot_source') == 'translator':
        return False

    if bootstrap_lock_active():
        return False

    try:
        # 1. Write sighting
        write_sighting(item)

        # 2. Upsert speech (aggregated across all sightings with same hash)
        if item.get('content_hash'):
            if speech_aggregates:
                agg = speech_aggregates.get(item['content_hash'], {})
            else:
                # Compute aggregate from DB
                agg = compute_speech_aggregate(item['content_hash'])
            agg['content_hash'] = item['content_hash']
            agg['translation'] = item.get('translation')
            agg['original_text'] = item.get('original_text')
            agg['first_sub4'] = agg.get('first_sub4') or item.get('sub4')
            upsert_speech(agg)

        # 3. Upsert entities
        for etype, value in [
            ('buyers', item.get('buyer_name')),
            ('domains', item.get('tracking_domain')),
            ('pixels', item.get('pix')),
            ('geos', item.get('geo')),
        ]:
            if value and value != 'unknown' and value != 'XX':
                upsert_entity(etype, value)

        return True
    except Exception as e:
        log.error("[OBSIDIAN] sync_item failed for id=%s: %s", item.get('id'), e)
        return False


def compute_speech_aggregate(content_hash):
    """Compute speech aggregate from DB for given content_hash."""
    from app import DB_PATH  # type: ignore
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT items.*, buyers.name as buyer_name FROM items "
            "LEFT JOIN buyers ON items.buyer_id = buyers.id "
            "WHERE content_hash=?",
            (content_hash,)
        ).fetchall()
        if not rows:
            return {}

        first_date = min(r['created_at'][:10] for r in rows if r['created_at'])
        last_date = max(r['created_at'][:10] for r in rows if r['created_at'])
        lifespan_days = (datetime.strptime(last_date, '%Y-%m-%d') - datetime.strptime(first_date, '%Y-%m-%d')).days if first_date and last_date else 0

        domains = sorted({r['tracking_domain'] for r in rows if r['tracking_domain']})
        buyers = sorted({r['buyer_name'] for r in rows if r['buyer_name']})
        geos = sorted({r['geo'] for r in rows if r['geo'] and r['geo'] != 'XX'})
        pixels = sorted({r['pix'] for r in rows if r['pix'] and r['pix'] != 'unknown'})

        status = 'running' if (datetime.now() - datetime.strptime(last_date, '%Y-%m-%d')).days <= 3 else 'stopped' if (datetime.now() - datetime.strptime(last_date, '%Y-%m-%d')).days > 7 else 'paused'

        return {
            'geo': geos[0] if geos else 'XX',
            'first_seen': first_date,
            'last_seen': last_date,
            'lifespan_days': lifespan_days,
            'sightings_count': len(rows),
            'unique_domains': domains,
            'unique_buyers': buyers,
            'pixel': pixels[0] if pixels else None,
            'status': status,
            'first_sub4': rows[0]['sub4'],
            'tags': [],
        }
    finally:
        conn.close()


# =====================================================
# BOOTSTRAP
# =====================================================

def bootstrap_all(db_path):
    """Export all existing items to vault. Lock prevents live sync during this."""
    log.info("[OBSIDIAN] Bootstrap starting...")
    acquire_bootstrap_lock()
    try:
        VAULT_ROOT.mkdir(parents=True, exist_ok=True)
        # Create .gitignore
        (VAULT_ROOT / '.gitignore').write_text(
            "sightings/\n.obsidian/workspace*\n.bootstrap.lock\n*.tmp\n"
        )

        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT items.*, buyers.name as buyer_name FROM items "
            "LEFT JOIN buyers ON items.buyer_id = buyers.id "
            "WHERE translation IS NOT NULL ORDER BY created_at"
        ).fetchall()

        total = len(rows)
        log.info("[OBSIDIAN] Bootstrap: %d items to export", total)

        # Group by content_hash for speech aggregates
        by_hash = defaultdict(list)
        for r in rows:
            if r['content_hash']:
                by_hash[r['content_hash']].append(dict(r))

        # Pre-compute aggregates
        aggregates = {}
        for h, items in by_hash.items():
            first_date = min(i['created_at'][:10] for i in items if i['created_at'])
            last_date = max(i['created_at'][:10] for i in items if i['created_at'])
            try:
                lifespan = (datetime.strptime(last_date, '%Y-%m-%d') - datetime.strptime(first_date, '%Y-%m-%d')).days
            except Exception:
                lifespan = 0

            domains = sorted({i['tracking_domain'] for i in items if i['tracking_domain']})
            buyers = sorted({i['buyer_name'] for i in items if i['buyer_name']})
            geos = sorted({i['geo'] for i in items if i['geo'] and i['geo'] != 'XX'})
            pixels = sorted({i['pix'] for i in items if i['pix'] and i['pix'] != 'unknown'})

            try:
                days_since = (datetime.now() - datetime.strptime(last_date, '%Y-%m-%d')).days
                status = 'running' if days_since <= 3 else ('paused' if days_since <= 7 else 'stopped')
            except Exception:
                status = 'unknown'

            aggregates[h] = {
                'geo': geos[0] if geos else 'XX',
                'first_seen': first_date,
                'last_seen': last_date,
                'lifespan_days': lifespan,
                'sightings_count': len(items),
                'unique_domains': domains,
                'unique_buyers': buyers,
                'pixel': pixels[0] if pixels else None,
                'status': status,
                'first_sub4': items[0]['sub4'],
                'translation': items[-1]['translation'],
                'original_text': items[-1].get('original_text'),
                'content_hash': h,
                'tags': [],
            }

        # Write speech files (one per hash)
        for h, agg in aggregates.items():
            try:
                upsert_speech(agg)
            except Exception as e:
                log.warning("[OBSIDIAN] speech write failed for %s: %s", h, e)

        # Write sighting files
        sighting_count = 0
        for r in rows:
            try:
                write_sighting(dict(r))
                sighting_count += 1
            except Exception as e:
                log.warning("[OBSIDIAN] sighting write failed id=%s: %s", r['id'], e)

        # Collect entities
        entities = {'buyers': set(), 'domains': set(), 'pixels': set(), 'geos': set()}
        for r in rows:
            if r['buyer_name']:
                entities['buyers'].add(r['buyer_name'])
            if r['tracking_domain']:
                entities['domains'].add(r['tracking_domain'])
            if r['pix'] and r['pix'] != 'unknown':
                entities['pixels'].add(r['pix'])
            if r['geo'] and r['geo'] != 'XX':
                entities['geos'].add(r['geo'])

        # Write entities
        for etype, names in entities.items():
            for name in names:
                try:
                    upsert_entity(etype, name)
                except Exception as e:
                    log.warning("[OBSIDIAN] entity write failed %s/%s: %s", etype, name, e)

        # Write index
        stats = {
            'speeches': len(aggregates),
            'sightings': sighting_count,
            'buyers': len(entities['buyers']),
            'domains': len(entities['domains']),
        }
        atomic_write(VAULT_ROOT / "_index" / "README.md", render_index_readme(stats))

        conn.close()
        log.info("[OBSIDIAN] Bootstrap complete: %d speeches, %d sightings, %d buyers, %d domains",
                 len(aggregates), sighting_count, len(entities['buyers']), len(entities['domains']))
        return stats
    finally:
        release_bootstrap_lock()


if __name__ == "__main__":
    # CLI: python obsidian_sync.py bootstrap
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        db = sys.argv[2] if len(sys.argv) > 2 else "spy_data.db"
        bootstrap_all(db)
    else:
        print("Usage: python obsidian_sync.py bootstrap [db_path]")
