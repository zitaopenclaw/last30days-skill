"""Bilibili video search via the official web search API (free, no auth required).

Uses api.bilibili.com/x/web-interface/wbi/search/type with WBI request
signing. WBI keys come from x/web-interface/nav, which works logged-out and
returns ``data.wbi_img.img_url``/``sub_url``; the img_key/sub_key filenames
are mixed through the standard 64-char permutation table to build the
``w_rid`` signature. No cookies, no API key, no env vars - just HTTP calls
via stdlib urllib (through the shared ``http`` helper).

The API cannot filter by date server-side, so results are requested with
``order=pubdate`` (newest first) and filtered client-side against
[from_date, to_date]. Pubdates are exact Unix timestamps, so date confidence
is high.
"""

import datetime
import hashlib
import html
import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from . import http, log
from .relevance import token_overlap_relevance

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"

# Browser-like headers; Bilibili rejects requests without a Referer.
_HEADERS = {
    "User-Agent": http.BROWSER_USER_AGENT,
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json",
}

# Standard WBI mixin-key permutation table (see Bilibili web-interface docs).
_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# Characters the reference WBI implementation strips from values before
# signing; leaving them in produces a mismatching w_rid.
_SIGN_FILTER_CHARS = "!'()*"

# Observed page size of the search/type endpoint.
PAGE_SIZE = 20
MAX_PAGES = 5

DEPTH_CONFIG = {
    "quick": 10,
    "default": 25,
    "deep": 50,
}

# WBI keys are per-session, not per-request; cache them at module level so
# paginated searches (and concurrent subquery workers) don't refetch /nav.
_wbi_lock = threading.Lock()
_wbi_keys_cache: Optional[Tuple[str, str]] = None

_EM_TAG_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)


def _log(msg: str) -> None:
    log.source_log("Bilibili", msg, tty_only=False)


def _date_to_unix(date_str: str) -> int:
    """Convert YYYY-MM-DD to Unix timestamp (start of day UTC)."""
    parts = date_str.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _unix_to_date(ts: int) -> str:
    """Convert Unix timestamp to YYYY-MM-DD."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _to_int(value: Any) -> int:
    """Convert a Bilibili counter to int.

    Counters arrive as ints, numeric strings, ``-1`` when the counter is
    unavailable, or ``None``. Unavailable/unparseable values become 0.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "")
    if not text or text == "--":
        return 0
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


def _extract_key(url: str) -> str:
    """Pull the WBI key out of an img/sub URL filename (no extension)."""
    return url.rsplit("/", 1)[-1].split(".")[0]


def _get_wbi_keys() -> Tuple[str, str]:
    """Fetch (img_key, sub_key) from /nav, cached at module level."""
    global _wbi_keys_cache
    with _wbi_lock:
        if _wbi_keys_cache is not None:
            return _wbi_keys_cache
        nav = http.request("GET", NAV_URL, headers=_HEADERS, timeout=15, retries=2)
        wbi_img = (nav.get("data") or {}).get("wbi_img") or {}
        img_key = _extract_key(str(wbi_img.get("img_url") or ""))
        sub_key = _extract_key(str(wbi_img.get("sub_url") or ""))
        if not img_key or not sub_key:
            raise http.HTTPError("Bilibili /nav returned no wbi_img keys")
        _wbi_keys_cache = (img_key, sub_key)
        return _wbi_keys_cache


def _mixin_key(img_key: str, sub_key: str) -> str:
    """Build the 32-char mixin key via the standard permutation table."""
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_TABLE)[:32]


def _signed_query(params: Dict[str, Any], mixin: str) -> str:
    """Sign params with WBI: add wts, sort, strip filter chars, append w_rid."""
    signed = {k: str(v) for k, v in params.items()}
    signed["wts"] = str(int(time.time()))
    ordered = dict(sorted(signed.items()))
    query = urlencode(
        {k: "".join(c for c in v if c not in _SIGN_FILTER_CHARS) for k, v in ordered.items()}
    )
    w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


def _search_page(keyword: str, page: int, order: Optional[str]) -> Dict[str, Any]:
    """Fetch one signed search page. Returns the API envelope (code/data)."""
    params: Dict[str, Any] = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
    }
    if order:
        params["order"] = order
    img_key, sub_key = _get_wbi_keys()
    query = _signed_query(params, _mixin_key(img_key, sub_key))
    return http.request("GET", f"{SEARCH_URL}?{query}", headers=_HEADERS, timeout=30, retries=2)


def search_bilibili(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Search Bilibili videos via the official WBI-signed web search API.

    Args:
        topic: Search topic (Chinese or English).
        from_date: Start date (YYYY-MM-DD), applied client-side.
        to_date: End date (YYYY-MM-DD), applied client-side.
        depth: 'quick', 'default', or 'deep'.

    Returns:
        Dict with a ``result`` list of raw video dicts. On failure,
        ``result`` is empty and an ``error`` key carries a one-line
        description. Never raises.
    """
    count = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    if not topic or not topic.strip():
        return {"result": []}
    keyword = topic.strip()
    from_ts = _date_to_unix(from_date)
    to_ts = _date_to_unix(to_date) + 86400  # Include the end date
    _log(f"Searching for '{keyword}' (since {from_date}, count={count})")

    collected: List[Dict[str, Any]] = []
    order: Optional[str] = "pubdate"
    pages = min(MAX_PAGES, max(1, math.ceil(count / PAGE_SIZE)))
    try:
        for page in range(1, pages + 1):
            try:
                response = _search_page(keyword, page, order)
            except http.HTTPError as e:
                _log(f"Search page {page} failed: {e}")
                return {"result": [], "error": str(e)}
            if response.get("code") == -412:
                _log("Rate limited by Bilibili (code -412)")
                return {"result": [], "error": "bilibili rate limited (code -412)"}
            if response.get("code") != 0:
                # Fall back to default relevance order if pubdate is rejected.
                if order == "pubdate":
                    _log(
                        f"order=pubdate rejected (code={response.get('code')} "
                        f"{response.get('message')}); retrying with default order"
                    )
                    order = None
                    response = _search_page(keyword, page, order)
                if response.get("code") != 0:
                    msg = f"bilibili search code={response.get('code')}: {response.get('message')}"
                    _log(f"Search failed: {msg}")
                    return {"result": [], "error": msg}
            data = response.get("data") or {}
            raw = data.get("result") or []
            if not isinstance(raw, list) or not raw:
                break

            page_oldest_ts: Optional[int] = None
            for entry in raw:
                if not isinstance(entry, dict) or entry.get("type") != "video":
                    continue
                pubdate = _to_int(entry.get("pubdate"))
                if pubdate:
                    page_oldest_ts = min(page_oldest_ts, pubdate) if page_oldest_ts else pubdate
                    if pubdate < from_ts or pubdate > to_ts:
                        continue  # Outside the date window; drop client-side.
                collected.append(entry)
            if len(collected) >= count:
                break
            # Results are newest-first under order=pubdate: once a whole page
            # predates the window, deeper pages only get older.
            if order == "pubdate" and page_oldest_ts and page_oldest_ts < from_ts:
                break
    except http.HTTPError as e:
        _log(f"Search failed: {e}")
        return {"result": [], "error": str(e)}
    except Exception as e:
        _log(f"Search failed: {e}")
        return {"result": [], "error": str(e)}

    results = collected[:count]
    _log(f"Found {len(results)} videos in window")
    return {"result": results, "keyword": keyword}


def _strip_em_tags(text: str) -> str:
    """Remove <em class="keyword"> highlight tags and decode entities."""
    return html.unescape(_EM_TAG_RE.sub("", text or "")).strip()


def parse_bilibili_response(
    response: Dict[str, Any],
    query: str = "",
) -> List[Dict[str, Any]]:
    """Parse a Bilibili search envelope into normalized item dicts.

    Args:
        response: Payload from ``search_bilibili`` (``result`` list of raw
            video dicts from data.result).
        query: Original search query for token-overlap relevance scoring.

    Returns:
        List of item dicts ready for normalization.
    """
    raw = response.get("result") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []

    items: List[Dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        bvid = str(entry.get("bvid") or "").strip()
        if not bvid:
            continue

        title = _strip_em_tags(str(entry.get("title") or ""))
        snippet = _strip_em_tags(str(entry.get("description") or ""))[:500]
        author = str(entry.get("author") or "").strip()

        views = _to_int(entry.get("play"))
        likes = _to_int(entry.get("like"))
        comments = _to_int(entry.get("review"))
        danmaku = _to_int(entry.get("danmaku"))
        favorites = _to_int(entry.get("favorites"))
        coins = _to_int(entry.get("coin"))

        pubdate = _to_int(entry.get("pubdate"))
        date_str = _unix_to_date(pubdate) if pubdate else None

        # Relevance: blend search rank with token-overlap content matching.
        rank_score = max(0.3, 1.0 - (i * 0.02))  # 1.0 -> 0.3 over 35 items
        engagement_boost = min(0.2, math.log1p(likes) / 50)
        if query:
            content_score = token_overlap_relevance(query, f"{title} {snippet}".strip())
            relevance = min(1.0, 0.6 * rank_score + 0.4 * content_score + engagement_boost)
        else:
            relevance = min(1.0, rank_score * 0.7 + engagement_boost + 0.1)

        items.append({
            "id": bvid,
            "title": title or f"Bilibili video {bvid}",
            "url": f"https://www.bilibili.com/video/{bvid}",
            "author": author,
            "date": date_str,
            "engagement": {
                "views": views,
                "likes": likes,
                "comments": comments,
                "danmaku": danmaku,
                "favorites": favorites,
                "coins": coins,
            },
            "snippet": snippet,
            "relevance": round(relevance, 2),
            "why_relevant": (
                f"Bilibili video ({views} views, {likes} likes, "
                f"{comments} comments) by {author or 'unknown'}"
            ),
        })

    return items
