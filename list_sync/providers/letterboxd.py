"""
Letterboxd provider for ListSync.
"""

import html as _html
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import register_provider, check_and_raise_if_cancelled, SyncCancelledException
from ..utils.logger import DATA_DIR

# Persistent cache of Letterboxd film slug -> (tmdb_id, media_type). The mapping is
# stable for a given film, so once resolved it never needs to be fetched again.
_CACHE_DB = os.path.join(DATA_DIR, "list_sync.db")

# Regexes for the TMDB identity Letterboxd exposes on every film page's <body>:
#   <body ... data-tmdb-id="496243" data-tmdb-type="movie" ...>
_TMDB_ID_RE = re.compile(r'data-tmdb-id="(\d+)"')
_TMDB_TYPE_RE = re.compile(r'data-tmdb-type="(movie|tv)"')

_LETTERBOXD_FILM_URL = "https://letterboxd.com/film/{slug}/"
_RESOLVE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _ensure_cache_table() -> None:
    """Create the slug->TMDB cache table if it does not exist (best-effort)."""
    try:
        with sqlite3.connect(_CACHE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS letterboxd_tmdb_cache (
                    slug        TEXT PRIMARY KEY,
                    tmdb_id     INTEGER,
                    media_type  TEXT,
                    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
    except sqlite3.Error as e:
        # If the cache can't be created (locked/unwritable DB), resolution still
        # works live — cache reads/writes are individually guarded and degrade to no-ops.
        logging.warning(f"Letterboxd TMDB cache unavailable ({e}); resolving without cache")


def _cache_get_many(slugs: List[str]) -> Dict[str, Tuple[Optional[int], Optional[str]]]:
    """Return cached (tmdb_id, media_type) for any of the given slugs already resolved."""
    if not slugs:
        return {}
    out: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    try:
        with sqlite3.connect(_CACHE_DB) as conn:
            placeholders = ",".join("?" for _ in slugs)
            rows = conn.execute(
                f"SELECT slug, tmdb_id, media_type FROM letterboxd_tmdb_cache WHERE slug IN ({placeholders})",
                slugs,
            ).fetchall()
        for slug, tmdb_id, media_type in rows:
            out[slug] = (tmdb_id, media_type)
    except sqlite3.Error as e:
        logging.warning(f"Letterboxd TMDB cache read failed: {e}")
    return out


def _cache_put(slug: str, tmdb_id: Optional[int], media_type: Optional[str]) -> None:
    """Persist a resolved slug so future syncs skip the network fetch."""
    try:
        with sqlite3.connect(_CACHE_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO letterboxd_tmdb_cache (slug, tmdb_id, media_type) VALUES (?, ?, ?)",
                (slug, tmdb_id, media_type),
            )
    except sqlite3.Error as e:
        logging.warning(f"Letterboxd TMDB cache write failed for '{slug}': {e}")


def _resolve_one(slug: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Fetch a Letterboxd film page and extract its exact TMDB id and type.

    Returns:
        (tmdb_id, media_type) where media_type is 'movie' or 'tv', or (None, None)
        if the page could not be fetched or exposes no TMDB id.
    """
    url = _LETTERBOXD_FILM_URL.format(slug=slug)
    try:
        resp = requests.get(url, headers=_RESOLVE_HEADERS, timeout=15)
        if resp.status_code != 200:
            logging.debug(f"Letterboxd film page {slug} returned HTTP {resp.status_code}")
            return None, None
        html = resp.text
        id_match = _TMDB_ID_RE.search(html)
        if not id_match:
            return None, None
        type_match = _TMDB_TYPE_RE.search(html)
        tmdb_type = type_match.group(1) if type_match else "movie"
        return int(id_match.group(1)), tmdb_type
    except requests.exceptions.RequestException as e:
        logging.debug(f"Letterboxd TMDB resolution failed for '{slug}': {e}")
        return None, None


def _resolve_tmdb_ids(media_items: List[Dict[str, Any]]) -> None:
    """
    Populate exact TMDB ids on scraped Letterboxd items in place.

    For every item with a slug, resolves the film's canonical TMDB id (and corrects
    media_type to 'tv' for shows) so downstream matching uses a direct TMDB lookup
    instead of fuzzy title/year search. Results are cached persistently by slug.

    Controlled by LETTERBOXD_RESOLVE_TMDB (default enabled) and
    LETTERBOXD_RESOLVE_WORKERS (default 8).
    """
    if os.getenv("LETTERBOXD_RESOLVE_TMDB", "true").lower() != "true":
        logging.info("Letterboxd TMDB resolution disabled via LETTERBOXD_RESOLVE_TMDB")
        return

    slugged = [it for it in media_items if it.get("slug")]
    if not slugged:
        return

    _ensure_cache_table()
    cached = _cache_get_many(sorted({it["slug"] for it in slugged}))

    resolved = 0
    from_cache = 0
    to_fetch: List[Dict[str, Any]] = []
    for it in slugged:
        hit = cached.get(it["slug"])
        if hit is not None:
            tmdb_id, media_type = hit
            if tmdb_id:
                it["tmdb_id"] = tmdb_id
                if media_type:
                    it["media_type"] = media_type
                resolved += 1
            from_cache += 1
        else:
            to_fetch.append(it)

    logging.info(
        f"🔗 Resolving Letterboxd TMDB ids: {from_cache} cached, {len(to_fetch)} to fetch"
    )

    if to_fetch:
        workers = int(os.getenv("LETTERBOXD_RESOLVE_WORKERS", "8") or "8")
        # De-duplicate slugs so the same film is only fetched once per run.
        unique_slugs = sorted({it["slug"] for it in to_fetch})
        results: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_resolve_one, slug): slug for slug in unique_slugs}
            for i, future in enumerate(as_completed(futures)):
                if i % 25 == 0:
                    check_and_raise_if_cancelled()
                slug = futures[future]
                tmdb_id, media_type = future.result()
                results[slug] = (tmdb_id, media_type)
                # Only persist successful resolutions. Caching a failure (transient
                # network error or 5xx) would permanently poison the slug; leaving it
                # uncached means it is simply retried on the next sync.
                if tmdb_id:
                    _cache_put(slug, tmdb_id, media_type)

        for it in to_fetch:
            tmdb_id, media_type = results.get(it["slug"], (None, None))
            if tmdb_id:
                it["tmdb_id"] = tmdb_id
                if media_type:
                    it["media_type"] = media_type
                resolved += 1

    unresolved = len(slugged) - resolved
    logging.info(
        f"🔗 Letterboxd TMDB resolution complete: {resolved}/{len(slugged)} resolved"
        + (f", {unresolved} unresolved (will fall back to title matching)" if unresolved else "")
    )


def _determine_media_type(title: str) -> str:
    """
    Determine if a title is a movie or TV show based on common patterns
    
    Args:
        title (str): The title to analyze
        
    Returns:
        str: "movie" or "tv_show"
    """
    title_lower = title.lower()
    
    # TV show indicators
    tv_indicators = [
        ':',  # Colon often indicates episode (e.g., "Euphoria: Trouble Don't Last Always")
        'season', 'episode', 'series',
        'special', 'pilot', 'finale',
        'part i', 'part ii', 'part iii', 'part iv', 'part v',
        'chapter', 'episode'
    ]
    
    # Check for TV show patterns
    for indicator in tv_indicators:
        if indicator in title_lower:
            return "tv_show"
    
    # Check for specific TV show patterns
    if any(pattern in title_lower for pattern in [
        'black mirror:', 'sherlock:', 'euphoria:', 'sense8:',
        'bojack horseman', 'wet hot american summer',
        'conversations with a killer', 'the walking dead:',
        'big little lies', 'sharp objects', 'godless',
        'who is america', 'the young pope', 'war and peace',
        'the vietnam war', 'the civil war', 'cosmos:',
        'roots', 'show me a hero', 'patrick melrose'
    ]):
        return "tv_show"
    
    # Default to movie
    return "movie"


# One <div class="react-component" ... data-item-name="Title (Year)" data-item-slug="slug" ...>
# per film on both watchlist and custom-list pages. [^>]* keeps the match inside one tag.
_ITEM_TAG_RE = re.compile(r'<div[^>]*\bdata-item-slug="[^"]*"[^>]*>')
_ATTR_NAME_RE = re.compile(r'\bdata-item-name="([^"]*)"')
_ATTR_SLUG_RE = re.compile(r'\bdata-item-slug="([^"]*)"')
_NEXT_PAGE_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bnext\b[^"]*")[^>]*\bhref="([^"]+)"'
)
_TITLE_YEAR_RE = re.compile(r'(.+?)\s*\((\d{4})\)\s*$')


def _http_session() -> requests.Session:
    """Session that retries Letterboxd's transient 5xx/429 responses."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _parse_list_page(page_html: str) -> List[Dict[str, Any]]:
    """Extract the films on one Letterboxd list page."""
    items: List[Dict[str, Any]] = []
    for tag in _ITEM_TAG_RE.findall(page_html):
        slug_match = _ATTR_SLUG_RE.search(tag)
        name_match = _ATTR_NAME_RE.search(tag)
        if not slug_match or not name_match:
            continue

        item_name = _html.unescape(name_match.group(1)).strip()
        if not item_name:
            continue

        year_match = _TITLE_YEAR_RE.match(item_name)
        if year_match:
            title = year_match.group(1).strip()
            year = int(year_match.group(2))
        else:
            title = item_name
            year = None

        if not title:
            logging.warning("Skipping item with empty title")
            continue

        items.append({
            "title": title,
            "media_type": "movie",
            "year": year,
            "slug": _html.unescape(slug_match.group(1)).strip(),
        })
    return items


def _next_page_url(page_html: str) -> Optional[str]:
    """Absolute URL of the pagination 'next' link, if there is one."""
    match = _NEXT_PAGE_RE.search(page_html)
    if not match:
        return None
    href = _html.unescape(match.group(1))
    if href.startswith("http"):
        return href
    return "https://letterboxd.com" + ("" if href.startswith("/") else "/") + href


@register_provider("letterboxd")
def fetch_letterboxd_list(list_id: str) -> List[Dict[str, Any]]:
    """
    Fetch a Letterboxd watchlist or custom list over plain HTTP, with pagination.

    Args:
        list_id (str): Letterboxd list ID (username/list-slug) or full URL

    Returns:
        List[Dict[str, Any]]: List of media items
    """
    media_items: List[Dict[str, Any]] = []
    logging.info(f"Fetching Letterboxd list: {list_id}")

    if list_id.startswith(("http://", "https://")):
        base_url = list_id.rstrip("/") + "/"
    else:
        base_url = f"https://letterboxd.com/{list_id.strip('/')}/"

    # The /detail/ view is 403 for non-browser clients and carries nothing extra:
    # slug + "Title (Year)" are on the plain view, and TMDB ids come from the film page.
    if base_url.endswith("/detail/"):
        base_url = base_url[: -len("detail/")]

    session = _http_session()
    seen_urls = set()
    current_url = base_url
    page = 0

    try:
        while current_url and current_url not in seen_urls:
            check_and_raise_if_cancelled()
            seen_urls.add(current_url)
            page += 1

            logging.info(f"Loading URL: {current_url}")
            resp = session.get(current_url, headers=_RESOLVE_HEADERS, timeout=30)
            if resp.status_code == 404 and page > 1:
                logging.info("Page 404 - past the last page")
                break
            resp.raise_for_status()

            page_items = _parse_list_page(resp.text)
            if not page_items:
                logging.info(f"No items on page {page}, stopping")
                break

            media_items.extend(page_items)
            logging.info(f"Page {page}: {len(page_items)} items (total {len(media_items)})")

            current_url = _next_page_url(resp.text)

        logging.info(
            f"Letterboxd list fetched successfully. Found {len(media_items)} items across {page} pages."
        )

    except SyncCancelledException:
        logging.warning(
            f"⚠️ Letterboxd list fetch cancelled by user - returning {len(media_items)} items fetched so far"
        )
        raise

    except Exception as e:
        logging.error(f"Error fetching Letterboxd list: {str(e)}")
        raise

    # Resolve each film's exact TMDB id from its Letterboxd page so downstream
    # matching uses a direct ID lookup instead of fuzzy title search.
    _resolve_tmdb_ids(media_items)
    return media_items
