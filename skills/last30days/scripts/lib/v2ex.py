"""V2EX source for last30days (Chinese tech discussion forum).

V2EX's official API (v2ex.com/api) has NO search endpoint, so topic
discovery goes through sov2ex, the standard community search API backed
by an Elasticsearch index of V2EX topics:

    GET https://www.sov2ex.com/api/search?q=<kw>&sort=created&size=<n>

Verified response shape (2026-08):

    {
      "took": 21,
      "total": 3986,
      "hits": [
        {
          "_id": "1235428",
          "_index": "topic_v1",
          "_type": "topic",
          "_score": null,
          "sort": [1787065581000],
          "highlight": {"content": ["...<em>match</em>..."]},
          "_source": {
            "id": 1235428,
            "title": "...",
            "content": "...",        // full topic body (Chinese)
            "member": "username",
            "node": 43,              // numeric node id (no name)
            "replies": 0,
            "created": "2026-08-18T15:06:21"  // naive local time (Asia/Shanghai)
          }
        }
      ]
}

Quirks:
- ``created`` carries no timezone; V2EX is a Chinese site so timestamps are
  effectively Asia/Shanghai. The YYYY-MM-DD prefix is used for window
  filtering, which can be off by a day at window edges near midnight UTC+8,
  hence ``date_confidence="med"`` in item metadata.
- sov2ex has no server-side date filter; [from_date, to_date] is enforced
  client-side after overfetching.
- ``sort=created`` returns newest-first; ``sort=sumup`` is relevance-ish but
  surfaces stale hits, so this module always uses ``created``.
- No auth, no rate-limit headers observed; a single GET per search.

Fallback note: if sov2ex ever dies, the V2EX official hot-topics endpoint
(https://www.v2ex.com/api/topics/hot.json) filtered by keyword match on
title/content is the documented alternative; it is intentionally NOT
implemented here because sov2ex is live and hot.json only covers ~recent
hot topics with no real search.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from . import http, log
from .query import extract_core_subject
from .relevance import token_overlap_relevance

SOV2EX_SEARCH_URL = "https://www.sov2ex.com/api/search"
V2EX_TOPIC_URL = "https://www.v2ex.com/t/{topic_id}"

# sov2ex `size` is capped at ~50 by the upstream Elasticsearch index.
SOV2EX_MAX_SIZE = 50

DEPTH_CONFIG = {
    "quick": 10,
    "default": 25,
    "deep": 50,
}

# Client-side date filtering drops out-of-window hits, so overfetch to keep
# the requested count meaningful. Capped by SOV2EX_MAX_SIZE.
V2EX_OVERFETCH_MULTIPLIER = 2

SNIPPET_MAX_CHARS = 300


def _log(msg: str) -> None:
    log.source_log("V2EX", msg, tty_only=False)


def _date_in_window(date_str: Optional[str], from_date: str, to_date: str) -> bool:
    """True when a YYYY-MM-DD date falls inside [from_date, to_date]."""
    if not date_str:
        return False
    return from_date <= date_str <= to_date


def _clean_snippet(content: str) -> str:
    """Collapse whitespace in the topic body and truncate to a snippet."""
    text = re.sub(r"\s+", " ", content or "").strip()
    if len(text) > SNIPPET_MAX_CHARS:
        return text[:SNIPPET_MAX_CHARS] + "..."
    return text


def search_v2ex(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Search V2EX topics via the sov2ex community search API.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD), enforced client-side
        to_date: End date (YYYY-MM-DD), enforced client-side
        depth: 'quick', 'default', or 'deep'

    Returns:
        Dict with the sov2ex response (contains a 'hits' list whose items
        carry the topic payload under '_source'). On failure, 'hits' is
        empty and an 'error' key carries a one-line description.
    """
    count = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    if not topic or not topic.strip():
        return {"hits": []}
    fetch_count = min(count * V2EX_OVERFETCH_MULTIPLIER, SOV2EX_MAX_SIZE)

    core = extract_core_subject(topic)
    query = " ".join(core.split()) or topic.strip()
    _log(f"Searching for '{query}' (raw: '{topic}', since {from_date}, count={count})")

    params = {
        "q": query,
        "sort": "created",
        "size": str(fetch_count),
    }

    try:
        response = http.request("GET", SOV2EX_SEARCH_URL, params=params, timeout=30)
    except http.HTTPError as e:
        _log(f"Search failed: {e}")
        return {"hits": [], "error": str(e)}
    except Exception as e:
        _log(f"Search failed: {e}")
        return {"hits": [], "error": str(e)}

    if not isinstance(response, dict):
        return {"hits": [], "error": "unexpected sov2ex response shape"}

    raw_hits = response.get("hits") or []
    windowed_hits = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        created = source.get("created") or ""
        date_str = created[:10] if isinstance(created, str) else ""
        if _date_in_window(date_str, from_date, to_date):
            windowed_hits.append(hit)

    hits = windowed_hits[:count]
    dropped = len(raw_hits) - len(windowed_hits)
    if dropped:
        _log(f"Filtered {dropped}/{len(raw_hits)} out-of-window topics")
    _log(f"Found {len(hits)} topics")
    return {**response, "hits": hits}


def parse_v2ex_response(response: Dict[str, Any], query: str = "") -> List[Dict[str, Any]]:
    """Parse a sov2ex response into normalized item dicts.

    Args:
        response: Payload from ``search_v2ex``.
        query: Original search query, used for token-overlap relevance.

    Returns:
        List of item dicts ready for normalization.
    """
    hits = response.get("hits") if isinstance(response, dict) else None
    if not isinstance(hits, list):
        return []

    items: List[Dict[str, Any]] = []
    for i, hit in enumerate(hits):
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        topic_id = source.get("id") or hit.get("_id")
        if not topic_id:
            continue

        title = str(source.get("title") or "").strip()
        content = str(source.get("content") or "")
        replies = source.get("replies") or 0
        if not isinstance(replies, (int, float)) or isinstance(replies, bool):
            replies = 0
        created = source.get("created") or ""
        date_str = created[:10] if isinstance(created, str) and created else None

        snippet = _clean_snippet(content)

        # Relevance: blend newest-first rank decay with token-overlap
        # content matching and a small reply-count boost.
        rank_score = max(0.3, 1.0 - (i * 0.02))  # 1.0 -> 0.3 over 35 items
        engagement_boost = min(0.2, math.log1p(replies) / 40)
        if query:
            content_score = token_overlap_relevance(query, f"{title} {snippet}".strip())
            relevance = min(1.0, 0.6 * rank_score + 0.4 * content_score + engagement_boost)
        else:
            relevance = min(1.0, rank_score * 0.7 + engagement_boost + 0.1)

        items.append({
            "id": str(topic_id),
            "title": title or f"V2EX topic {topic_id}",
            "url": V2EX_TOPIC_URL.format(topic_id=topic_id),
            "author": str(source.get("member") or ""),
            "date": date_str,
            "engagement": {
                "replies": int(replies),
            },
            "snippet": snippet,
            "relevance": round(relevance, 2),
            "why_relevant": f"V2EX topic ({int(replies)} replies): {title[:60]}",
            "metadata": {
                "node_id": source.get("node"),
                # `created` is a naive Asia/Shanghai timestamp; the date
                # prefix can be off by a day at window edges near midnight
                # UTC+8, so confidence is med rather than high.
                "date_confidence": "med",
            },
        })

    return items
