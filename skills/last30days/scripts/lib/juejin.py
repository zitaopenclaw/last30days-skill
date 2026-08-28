"""Juejin (掘金) search source for last30days.

Searches juejin.cn — China's largest developer-content community — via its
public web search API (no auth, no API key):

    POST https://api.juejin.cn/search_api/v1/search
    {"key_word": kw, "id_type": 0, "search_type": 1, "cursor": "0", "limit": n}

Verified response envelope: ``{err_no, err_msg, data, cursor, count,
has_more}`` where ``err_no == 0`` means success. Each entry in ``data`` has a
``result_type``; articles are ``result_type == 2`` (booklets are 12) and carry
``result_model.article_id`` / ``result_model.article_info`` /
``result_model.author_user_info``. ``ctime``/``mtime`` are Unix timestamp
strings. The API caps ``limit`` at 20 per page regardless of what is asked,
so larger depths paginate via the opaque ``cursor`` token.

Sort order: ``search_type=0`` is the relevance ("综合") sort, which buries
recent posts under old popular ones — in a 30-day window query it surfaces
almost nothing in-window. ``search_type=1`` is the recency-biased sort and
returns newest articles first (verified: multiple consecutive pages all
inside the last few days), so this module always uses it. Ordering is not
strictly monotonic, so the date window is still applied client-side.

Quirk: the endpoint soft-throttles rapid repeat calls by returning
``err_no=0`` with an empty ``data`` list (no 429, no error message). A full
browser User-Agent plus ~2s pacing between page requests avoids it; the
first page additionally gets one retry after a short sleep so a soft block
is not mistaken for a genuine zero-result query.

The API has no server-side date filter, so the [from_date, to_date] window
is applied client-side after fetching. Dates come from exact Unix
timestamps, so date confidence is high.
"""

import datetime
import math
import time
from typing import Any, Dict, List, Optional

from . import http, log
from .relevance import token_overlap_relevance

JUEJIN_SEARCH_URL = "https://api.juejin.cn/search_api/v1/search"
JUEJIN_POST_URL = "https://juejin.cn/post/"

# search_type=1 is the recency-biased sort (0 = relevance, which surfaces
# mostly years-old popular articles and is useless for a 30-day window).
SEARCH_TYPE_NEWEST = 1

# Article results in the search envelope. Other observed types: 12 = booklet
# (paid course), which we skip.
RESULT_TYPE_ARTICLE = 2

# The API silently caps `limit` at 20 per page.
PAGE_SIZE = 20

# Pause between page requests. The endpoint soft-throttles rapid repeats by
# returning err_no=0 with empty data; ~2s pacing avoids it.
PAGE_DELAY_SECONDS = 2.0

# Per-depth knobs. `pages` bounds cursor pagination (20 raw results per
# page); `count` is how many in-window articles survive the final slice.
DEPTH_CONFIG = {
    "quick": {"pages": 1, "count": 10},
    "default": {"pages": 2, "count": 25},
    "deep": {"pages": 4, "count": 50},
}

_REQUEST_HEADERS = {
    "User-Agent": http.BROWSER_USER_AGENT,
    "Referer": "https://juejin.cn/",
    "Origin": "https://juejin.cn",
}


def _log(msg: str):
    log.source_log("Juejin", msg, tty_only=False)


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


def _ctime_to_unix(value: Any) -> Optional[int]:
    """Parse Juejin's ctime/mtime (Unix timestamp as string) to int."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fetch_page(key_word: str, cursor: str) -> Dict[str, Any]:
    """POST one search page. Returns the raw envelope. Raises on failure."""
    payload = {
        "key_word": key_word,
        "id_type": 0,
        "search_type": SEARCH_TYPE_NEWEST,
        "cursor": cursor,
        "limit": PAGE_SIZE,
    }
    return http.post(
        JUEJIN_SEARCH_URL,
        json_data=payload,
        headers=dict(_REQUEST_HEADERS),
        timeout=30,
    )


def _article_entries(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract article entries (result_type == 2) from one page envelope."""
    entries = []
    for entry in (response or {}).get("data") or []:
        if isinstance(entry, dict) and entry.get("result_type") == RESULT_TYPE_ARTICLE:
            entries.append(entry)
    return entries


def search_juejin(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Search Juejin articles via the web search API.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD), applied client-side
        to_date: End date (YYYY-MM-DD), applied client-side
        depth: 'quick', 'default', or 'deep'

    Returns:
        Dict with 'data' list (merged across pages, articles only, date
        filtered). On failure, 'data' is empty and an 'error' key carries a
        one-line description. Never raises.
    """
    cfg = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    pages, count = cfg["pages"], cfg["count"]
    if not topic or not topic.strip():
        return {"data": []}

    from_ts = _date_to_unix(from_date)
    to_ts = _date_to_unix(to_date) + 86400  # Include the end date
    _log(f"Searching for '{topic}' (since {from_date}, pages={pages}, count={count})")

    # Paginate via the opaque cursor token; dedupe on article_id because
    # pages can occasionally repeat entries.
    raw_results: List[Dict[str, Any]] = []
    seen: set = set()
    cursor = "0"
    for page in range(pages):
        if page > 0:
            time.sleep(PAGE_DELAY_SECONDS)
        try:
            response = _fetch_page(topic.strip(), cursor)
        except Exception as e:
            _log(f"Search failed (page {page + 1}): {e}")
            if not raw_results:
                return {"data": [], "error": str(e)}
            break

        if not isinstance(response, dict):
            break
        if response.get("err_no") != 0:
            err_msg = response.get("err_msg") or f"err_no={response.get('err_no')}"
            _log(f"API error: {err_msg}")
            if not raw_results:
                return {"data": [], "error": str(err_msg)}
            break

        entries = _article_entries(response)
        if not entries and page == 0 and not raw_results:
            # err_no=0 with empty data on the first page is the endpoint's
            # soft-throttle signature; retry with escalating pauses so a
            # block isn't mistaken for a genuine zero-result query.
            for attempt in range(2):
                _log(f"Empty first page (possible soft throttle); retry {attempt + 1}/2")
                time.sleep(PAGE_DELAY_SECONDS * 2 * (attempt + 1))
                try:
                    response = _fetch_page(topic.strip(), cursor)
                except Exception as e:
                    _log(f"Retry failed: {e}")
                    return {"data": [], "error": str(e)}
                if not isinstance(response, dict) or response.get("err_no") != 0:
                    return {"data": []}
                entries = _article_entries(response)
                if entries:
                    break

        for entry in entries:
            model = entry.get("result_model") or {}
            article_id = model.get("article_id")
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            raw_results.append(entry)

        next_cursor = response.get("cursor")
        if not response.get("has_more") or not next_cursor:
            break
        cursor = str(next_cursor)

    # Client-side date window: the API cannot filter by date server-side.
    # ctime is an exact Unix timestamp, so out-of-window hits are dropped
    # outright; only entries with an unparseable ctime are kept (a coverage
    # gap, not a stale item).
    in_window: List[Dict[str, Any]] = []
    dropped_out_of_window = 0
    for entry in raw_results:
        article_info = (entry.get("result_model") or {}).get("article_info") or {}
        ctime = _ctime_to_unix(article_info.get("ctime"))
        if ctime is not None and not (from_ts <= ctime < to_ts):
            dropped_out_of_window += 1
            continue
        in_window.append(entry)

    data = in_window[:count]
    if dropped_out_of_window:
        _log(f"Filtered {dropped_out_of_window}/{len(raw_results)} out-of-window articles")
    _log(f"Found {len(data)} articles")
    return {"data": data, "cursor": cursor, "has_more": bool(data)}


def parse_juejin_response(response: Dict[str, Any], query: str = "") -> List[Dict[str, Any]]:
    """Parse a Juejin search envelope into normalized item dicts.

    Args:
        response: Envelope from ``search_juejin`` (merged 'data' list)
        query: Original search query for token-overlap relevance scoring

    Returns:
        List of item dicts ready for normalization.
    """
    raw = response.get("data") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []

    items: List[Dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        model = entry.get("result_model") or {}
        article_info = model.get("article_info") or {}
        article_id = str(model.get("article_id") or article_info.get("article_id") or "")
        if not article_id:
            continue

        title = str(article_info.get("title") or "").strip()
        brief = str(article_info.get("brief_content") or "").strip()
        author = str((model.get("author_user_info") or {}).get("user_name") or "").strip()

        date_str = None
        ctime = _ctime_to_unix(article_info.get("ctime"))
        if ctime is not None:
            date_str = _unix_to_date(ctime)

        digg_count = article_info.get("digg_count") or 0
        comment_count = article_info.get("comment_count") or 0
        view_count = article_info.get("view_count") or 0
        collect_count = article_info.get("collect_count") or 0

        # Relevance: blend search rank with token-overlap content matching,
        # plus a small engagement boost from digg (like) count.
        rank_score = max(0.3, 1.0 - (i * 0.02))  # 1.0 -> 0.3 over 35 items
        engagement_boost = min(0.2, math.log1p(digg_count) / 40)
        if query:
            content_score = token_overlap_relevance(query, f"{title} {brief}".strip())
            relevance = min(1.0, 0.6 * rank_score + 0.4 * content_score + engagement_boost)
        else:
            relevance = min(1.0, rank_score * 0.7 + engagement_boost + 0.1)

        items.append({
            "id": article_id,
            "title": title,
            "url": f"{JUEJIN_POST_URL}{article_id}",
            "author": author,
            "date": date_str,
            "engagement": {
                "digg_count": digg_count,
                "comment_count": comment_count,
                "view_count": view_count,
                "collect_count": collect_count,
            },
            "snippet": brief[:300],
            "relevance": round(relevance, 2),
            "why_relevant": (
                f"Juejin article ({digg_count} diggs, {view_count} views): {title[:60]}"
            ),
        })

    return items
