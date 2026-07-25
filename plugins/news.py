"""
Anime news source — pulls recent headlines from Anime News Network's public
RSS feed (https://www.animenewsnetwork.com/all/rss.xml). Powers the News
tab's Anime News carousel. This is a real, publicly documented feed —
not a fabricated data source, so it always reflects an actual, current
news story rather than a re-labelled anime entry.
"""

import time
import xml.etree.ElementTree as ET

import requests

from config import Config

_cache: dict[str, tuple[float, list]] = {}


def _strip_html(text: str) -> str:
    """Minimal tag stripper — good enough for RSS <description> snippets,
    which are typically a sentence or two of plain text wrapped in <p>."""
    out = []
    in_tag = False
    for ch in text or "":
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            out.append(ch)
    return " ".join("".join(out).split())


def _fetch_items() -> list[dict]:
    resp = requests.get(
        Config.ANN_RSS_URL, timeout=10,
        headers={"User-Agent": "AnimeIndexBot/1.0 (+https://github.com/)"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = item.findtext("description") or ""

        image = None
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image = enclosure.get("url")

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "summary": _strip_html(description)[:280],
                "image": image,
            })
    return items


def _cached(key: str, fetch):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < Config.NEWS_CACHE_TTL:
        return hit[1]
    value = fetch()
    _cache[key] = (now, value)
    return value


def get_latest(limit: int = 10) -> list[dict]:
    items = _cached("latest", _fetch_items)
    return items[:limit]
