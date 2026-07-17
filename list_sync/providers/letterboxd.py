"""
Letterboxd provider for ListSync.
"""

import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

import requests
from seleniumbase import SB

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


@register_provider("letterboxd")
def fetch_letterboxd_list(list_id: str) -> List[Dict[str, Any]]:
    """
    Fetch Letterboxd list using Selenium with pagination, supporting both regular lists and watchlists
    
    Args:
        list_id (str): Letterboxd list ID (username/list-slug) or full URL
        
    Returns:
        List[Dict[str, Any]]: List of media items
    """
    media_items = []
    logging.info(f"Fetching Letterboxd list: {list_id}")
    
    try:
        with SB(uc=True, headless=True) as sb:
            # Handle full URLs vs list IDs
            if list_id.startswith(('http://', 'https://')):
                base_url = list_id.rstrip('/')
            else:
                base_url = f"https://letterboxd.com/{list_id}"
            
            # Determine if this is a watchlist or regular list
            is_watchlist = '/watchlist' in base_url or list_id.endswith('/watchlist')
            
            # Use detail view for better data extraction (only for custom lists, not watchlists)
            if not is_watchlist and not base_url.endswith('/detail/'):
                base_url = f"{base_url}/detail/"
            
            logging.info(f"Processing Letterboxd {'watchlist' if is_watchlist else 'list'}: {base_url}")
            
            page = 1
            next_page_url = None  # Track the next page URL
            while True:
                # Check for cancellation at the start of each page
                check_and_raise_if_cancelled()
                
                # Construct page URL - handle both first page and subsequent pages
                if page == 1:
                    current_url = base_url
                elif next_page_url:
                    # Use the specific next page URL we found
                    current_url = next_page_url
                else:
                    # Fallback: construct URL manually
                    if is_watchlist:
                        current_url = f"{base_url}/page/{page}/"
                    else:
                        current_url = f"{base_url}page/{page}/"
                
                logging.info(f"Loading URL: {current_url}")
                sb.open(current_url)
                
                # Wait for the list container to load - different selectors for watchlists vs custom lists
                if is_watchlist:
                    try:
                        sb.wait_for_element_present("ul.poster-list", timeout=20)
                    except:
                        logging.warning("Could not find ul.poster-list, attempting to continue")
                else:
                    sb.wait_for_element_present("div.list-detailed-entries-list", timeout=20)
                
                logging.info(f"Processing page {page}")
                
                # Scroll to load all lazy-loaded content
                logging.info("Scrolling to load all content...")
                scroll_attempts = 3 if is_watchlist else 10  # Watchlists load faster
                for i in range(scroll_attempts):
                    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    sb.sleep(1 if is_watchlist else 2)
                    
                    # Also try scrolling to specific elements (for custom lists)
                    if not is_watchlist:
                        items = sb.find_elements("div.listitem.js-listitem")
                        if items:
                            sb.execute_script("arguments[0].scrollIntoView(true);", items[-1])
                            sb.sleep(1)
                
                logging.info("Scrolling complete")
                
                # Extract data - different structure for watchlists vs custom lists
                logging.info("Extracting data from attributes...")
                
                if is_watchlist:
                    # Watchlists use li.griditem structure
                    figures = sb.find_elements("li.griditem")
                    logging.info(f"Found {len(figures)} grid items (watchlist)")
                else:
                    # Custom lists use detail view with figures
                    figures = sb.find_elements("div.react-component.figure[data-item-name]")
                    logging.info(f"Found {len(figures)} figures (custom list)")
                
                # If we find 0 items, we've gone too far - break
                if len(figures) == 0:
                    logging.info(f"No items found on page {page}, ending pagination")
                    break
                
                for idx, figure in enumerate(figures):
                    # Check for cancellation every 10 items
                    if idx > 0 and idx % 10 == 0:
                        check_and_raise_if_cancelled()
                    
                    try:
                        # Extract data - different attributes for watchlist vs custom list
                        if is_watchlist:
                            # For watchlists, find the react-component div within the grid item
                            try:
                                react_div = figure.find_element("css selector", "div.react-component")
                            except:
                                react_div = figure
                            
                            item_name = react_div.get_attribute("data-item-name")
                            film_id = react_div.get_attribute("data-film-id")
                            slug = react_div.get_attribute("data-item-slug")
                        else:
                            # For custom lists, use the figure element directly
                            item_name = figure.get_attribute("data-item-name")
                            film_id = figure.get_attribute("data-film-id")
                            slug = figure.get_attribute("data-item-slug")
                        
                        if not item_name:
                            continue
                        
                        # Parse title and year (format: "Title (Year)")
                        year_match = re.search(r'(.+?)\s*\((\d{4})\)', item_name)
                        if year_match:
                            title = year_match.group(1).strip()
                            year = int(year_match.group(2))
                        else:
                            title = item_name
                            year = None
                        
                        # Skip items with empty titles
                        if not title:
                            logging.warning("Skipping item with empty title")
                            continue
                        
                        # Determine media type (all appear to be films)
                        media_type = "movie"
                        
                        media_items.append({
                            "title": title,
                            "media_type": media_type,
                            "year": year,
                            "film_id": film_id,
                            "slug": slug
                        })
                        logging.info(f"Added {media_type}: {title} ({year if year else 'year unknown'})")
                        
                    except Exception as e:
                        logging.warning(f"Failed to parse item: {str(e)}")
                        continue
                
                # Check for next page - improved logic for Letterboxd pagination
                try:
                    has_next_page = False
                    next_page_url = None
                    
                    # Look for pagination container first
                    pagination_container = sb.find_elements("div.pagination")
                    if pagination_container:
                        # Look for next button within pagination
                        next_buttons = sb.find_elements("div.pagination a.next")
                        if next_buttons and len(next_buttons) > 0:
                            next_href = next_buttons[0].get_attribute("href")
                            if next_href:
                                has_next_page = True
                                # Convert relative URL to absolute URL
                                if next_href.startswith('/'):
                                    next_page_url = f"https://letterboxd.com{next_href}"
                                else:
                                    next_page_url = next_href
                                logging.info(f"Found next page URL: {next_page_url}")
                    
                    # Fallback: also check for numbered pagination
                    if not has_next_page:
                        page_links = sb.find_elements("div.pagination .paginate-pages a")
                        if page_links:
                            # Find the highest page number
                            max_page = page
                            for link in page_links:
                                href = link.get_attribute("href")
                                if href and f"page/{page + 1}/" in href:
                                    has_next_page = True
                                    next_page_url = href if href.startswith('http') else f"https://letterboxd.com{href}"
                                    break
                    
                    if has_next_page and next_page_url:
                        page += 1
                        logging.info(f"Next page available, will load page {page} from URL: {next_page_url}")
                        # Store the next page URL for the next iteration
                        # The next_page_url variable will be used in the next loop iteration
                        # Continue to next iteration of while loop which will load the next page
                    else:
                        logging.info("No next page found, must be the last page")
                        break
                except Exception as e:
                    logging.warning(f"Error checking pagination: {str(e)}")
                    # If there's an error checking pagination, assume we're done
                    break
            
            logging.info(f"Letterboxd list fetched successfully. Found {len(media_items)} items across {page} pages.")

    except SyncCancelledException:
        logging.warning(f"⚠️ Letterboxd list fetch cancelled by user - returning {len(media_items)} items fetched so far")
        raise
        
    except Exception as e:
        logging.error(f"Error fetching Letterboxd list: {str(e)}")
        raise

    # Browser is now closed. Resolve each film's exact TMDB id from its Letterboxd
    # page so downstream matching uses a direct ID lookup instead of fuzzy title search.
    _resolve_tmdb_ids(media_items)
    return media_items
