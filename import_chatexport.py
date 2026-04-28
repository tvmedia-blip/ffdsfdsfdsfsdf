#!/usr/bin/env python3
"""
Импорт Telegram ChatExport (messages.html) в БД spy_data.db.

Использование:
  python3 import_chatexport.py /path/to/messages.html /path/to/spy_data.db [--dry-run]
"""

import sys, re, sqlite3
from html import unescape
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urlparse


FB_URL_RE = re.compile(r'https?://(?:www\.|m\.)?(?:facebook\.com|fb\.watch|fb\.com)/[^\s,;)\]}>\"\']+')
NON_FB_URL_RE = re.compile(r'https?://(?!(?:www\.|m\.)?(?:facebook\.com|fb\.watch|fb\.com))[^\s,;)\]}>\"\']+')
TRACKING_RE = re.compile(r'LINK\s*-\s*(\S+)\s*,\s*4sub\s*-\s*(.+?)[\n\r]+5sub\s*-\s*(.+?)\s*,\s*pix\s*-\s*(.+?)$', re.MULTILINE)


def parse_date(date_str):
    try:
        clean = re.sub(r'\s*UTC[+-]\d{2}:\d{2}$', '', date_str)
        dt = datetime.strptime(clean, '%d.%m.%Y %H:%M:%S')
        m = re.search(r'UTC([+-])(\d{2}):(\d{2})$', date_str)
        if m:
            sign = 1 if m.group(1) == '+' else -1
            dt = dt - timedelta(hours=sign * int(m.group(2)), minutes=sign * int(m.group(3)))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def extract_text_and_links(div):
    """Extract text content and URLs from a .text div."""
    text_parts = []
    for el in div.children:
        if el.name == 'br':
            text_parts.append('\n')
        elif el.name == 'a':
            href = el.get('href', '')
            if href.startswith('http'):
                text_parts.append(href)
            else:
                text_parts.append(el.get_text())
        elif hasattr(el, 'get_text'):
            text_parts.append(el.get_text())
        else:
            text_parts.append(str(el))
    return ''.join(text_parts).strip()


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 import_chatexport.py <messages.html> <spy_data.db> [--dry-run]")
        sys.exit(1)

    html_path = sys.argv[1]
    db_path = sys.argv[2]
    dry_run = '--dry-run' in sys.argv

    print(f"Parsing {html_path}...")
    with open(html_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    # Parse all messages
    messages = []
    last_from = ''

    for msg_div in soup.find_all('div', class_=re.compile(r'message default')):
        # Get from_name (may be absent in "joined" messages)
        from_div = msg_div.find('div', class_='from_name')
        if from_div:
            last_from = from_div.get_text(strip=True)
        from_name = last_from

        # Get date
        date_div = msg_div.find('div', class_=re.compile(r'date details'))
        date_str = date_div.get('title', '') if date_div else ''

        # Get text
        text_div = msg_div.find('div', class_='text')
        if not text_div:
            continue
        text = extract_text_and_links(text_div)

        messages.append({
            'from': from_name,
            'date': date_str,
            'text': text,
        })

    print(f"Found {len(messages)} messages")

    # Pair CA SPY (input) with СПАЙ ВЫГРУЗКА (response)
    items = []
    pending_spy = None  # last CA SPY message

    for msg in messages:
        name = msg['from']
        text = msg['text']

        # Input message from CA SPY (contains FB + tracking links)
        if 'SPY' in name and 'ВЫГРУЗКА' not in name:
            fb_urls = FB_URL_RE.findall(text)
            non_fb = NON_FB_URL_RE.findall(text)
            pending_spy = {
                'fb_url': fb_urls[0] if fb_urls else None,
                'tracking_url': non_fb[0] if non_fb else None,
                'date': msg['date'],
            }
            continue

        # Bot response (СПАЙ ВЫГРУЗКА)
        if 'ВЫГРУЗКА' in name:
            if '❌' in text:
                continue  # error

            # Parse LINK/4sub/5sub/pix
            tracking_match = TRACKING_RE.search(text)
            domain = tracking_match.group(1).strip() if tracking_match else None
            sub4 = tracking_match.group(2).strip() if tracking_match else None
            sub5 = tracking_match.group(3).strip() if tracking_match else None
            pix = tracking_match.group(4).strip() if tracking_match else None

            # Translation = text before LINK line
            if tracking_match:
                translation = text[:tracking_match.start()].strip()
            else:
                translation = text.strip()

            if not translation or len(translation) < 30:
                continue

            fb_url = pending_spy['fb_url'] if pending_spy else None
            tracking_url = pending_spy['tracking_url'] if pending_spy else None
            date_str = msg['date'] or (pending_spy['date'] if pending_spy else '')

            items.append({
                'created_at': parse_date(date_str),
                'fb_url': fb_url,
                'translation': translation,
                'tracking_domain': domain,
                'tracking_url': tracking_url,
                'sub4': sub4,
                'sub5': sub5,
                'pix': pix,
            })

    print(f"Parsed {len(items)} valid items")

    if dry_run:
        for i, item in enumerate(items[:5]):
            print(f"\n--- Item {i+1} ---")
            print(f"  date: {item['created_at']}")
            print(f"  fb: {(item['fb_url'] or 'N/A')[:70]}")
            print(f"  link: {(item['tracking_url'] or 'N/A')[:70]}")
            print(f"  domain: {item['tracking_domain']}")
            print(f"  4sub: {item['sub4']}, 5sub: {item['sub5']}, pix: {item['pix']}")
            print(f"  text: {item['translation'][:120]}...")
        if len(items) > 5:
            print(f"\n  ... и ещё {len(items) - 5}")
        print(f"\n[DRY RUN] Would insert {len(items)} items into {db_path}")
        return

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    inserted = 0
    skipped = 0
    for item in items:
        if item['fb_url']:
            existing = conn.execute("SELECT id FROM items WHERE fb_url = ?", (item['fb_url'],)).fetchone()
            if existing:
                skipped += 1
                continue

        conn.execute(
            "INSERT INTO items (created_at, fb_url, translation, tracking_domain, tracking_url, sub4, sub5, pix) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item['created_at'], item['fb_url'], item['translation'],
             item['tracking_domain'], item['tracking_url'],
             item['sub4'], item['sub5'], item['pix']),
        )
        inserted += 1

    conn.commit()
    conn.close()
    print(f"Inserted: {inserted}, Skipped (duplicates): {skipped}")


if __name__ == '__main__':
    main()
