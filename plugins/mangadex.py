"""
MangaDex adapter — public REST API, no key required.
Used for adult manga / manhwa (erotica + pornographic content ratings).
https://api.mangadex.org/docs/
"""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

from config import Config
from plugins.base import AnimeSource

API = "https://api.mangadex.org"
COVERS = "https://uploads.mangadex.org/covers"
# Adult-focused ratings only (pornhwa / adult manhwa / hentai manga)
CONTENT_RATINGS = ["erotica", "pornographic"]
TIMEOUT = 12


def _title(attrs: dict) -> str:
    t = attrs.get("title") or {}
    if isinstance(t, dict):
        return (
            t.get("en")
            or t.get("ja-ro")
            or t.get("ja")
            or next((v for v in t.values() if v), None)
            or "Untitled"
        )
    return str(t) or "Untitled"


def _alt_title(attrs: dict) -> str | None:
    alts = attrs.get("altTitles") or []
    main = _title(attrs)
    for block in alts:
        if not isinstance(block, dict):
            continue
        for v in block.values():
            if v and v != main:
                return v
    return None


def _description(attrs: dict) -> str:
    d = attrs.get("description") or {}
    if isinstance(d, dict):
        return (d.get("en") or next((v for v in d.values() if v), "") or "").strip()
    return str(d or "").strip()


def _cover_url(manga_id: str, relationships: list) -> str | None:
    for rel in relationships or []:
        if rel.get("type") != "cover_art":
            continue
        file_name = (rel.get("attributes") or {}).get("fileName")
        if file_name:
            # .256.jpg is a small derivative — fast to load on mobile
            return f"{COVERS}/{manga_id}/{file_name}.256.jpg"
    return None


def _tags(attrs: dict, limit: int = 8) -> list[str]:
    out = []
    for tag in attrs.get("tags") or []:
        name = ((tag.get("attributes") or {}).get("name") or {}).get("en")
        if name:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _status_map(status: str | None) -> str | None:
    # Normalize to AniList-like status strings used by the frontend
    m = {
        "ongoing": "RELEASING",
        "completed": "FINISHED",
        "hiatus": "HIATUS",
        "cancelled": "CANCELLED",
    }
    return m.get((status or "").lower())


class MangaDexSource(AnimeSource):
    name = "mangadex"

    def __init__(self):
        self._cache: dict[str, tuple[float, dict]] = {}
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "HIndexBot/1.0 (catalog; +https://github.com/)",
            "Accept": "application/json",
        })

    def _cached(self, key: str, fetch):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < Config.CATALOG_CACHE_TTL:
            return hit[1]
        try:
            from database import database as db
            mongo_hit = db.cache_get(f"md:{key}")
            if mongo_hit is not None:
                self._cache[key] = (now, mongo_hit)
                return mongo_hit
        except Exception:
            pass
        value = fetch()
        self._cache[key] = (now, value)
        try:
            from database import database as db
            db.cache_set(f"md:{key}", value)
        except Exception:
            pass
        return value

    def _get(self, path: str, params: dict | None = None) -> dict:
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._session.get(f"{API}{path}", params=params, timeout=TIMEOUT)
                if resp.status_code == 429:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                last_exc = e
                time.sleep(0.4 * (attempt + 1))
        raise last_exc

    def _list_manga(self, params: dict, page: int = 1, per_page: int = 12) -> dict:
        offset = max(0, (page - 1) * per_page)
        base = {
            "limit": per_page,
            "offset": offset,
            "includes[]": ["cover_art"],
            "contentRating[]": CONTENT_RATINGS,
            "order[followedCount]": "desc",
        }
        base.update(params)
        # requests encodes list params; pass as list of tuples for repeated keys
        pairs = []
        for k, v in base.items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    pairs.append((k, item))
            else:
                pairs.append((k, v))
        data = self._get("/manga", pairs)
        results = []
        for m in data.get("data") or []:
            attrs = m.get("attributes") or {}
            mid = m.get("id")
            results.append({
                "title": _title(attrs),
                "poster_url": _cover_url(mid, m.get("relationships")),
                "rating": None,
                "source": self.name,
                "source_id": mid,
                "anilist_id": None,
                "genres": _tags(attrs, 3),
                "chapters": None,
                "format": (attrs.get("originalLanguage") or "").upper() or None,
                "countryOfOrigin": (attrs.get("originalLanguage") or "").upper() or None,
                "media_type": "MANGA",
                "status": _status_map(attrs.get("status")),
                "synopsis": (_description(attrs) or "")[:140],
            })
        total = (data.get("total") or 0)
        has_next = offset + per_page < total
        return {"results": results, "has_next": has_next}

    def search(self, query: str, page: int = 1) -> dict:
        return self._cached(
            f"search:{query}:{page}",
            lambda: self._list_manga({"title": query, "order[relevance]": "desc"}, page),
        )

    # Alias used by app routes
    def search_manga(self, query: str, page: int = 1) -> dict:
        return self.search(query, page)

    def get_details(self, source_id, use_cache: bool = True) -> dict:
        sid = str(source_id)

        def fetch():
            data = self._get(f"/manga/{sid}", {
                "includes[]": ["cover_art", "author", "artist"],
            })
            m = data.get("data") or {}
            attrs = m.get("attributes") or {}
            mid = m.get("id") or sid
            year = attrs.get("year")
            return {
                "source": self.name,
                "source_id": mid,
                "media_type": "MANGA",
                "title": _title(attrs),
                "alt_title": _alt_title(attrs),
                "year": year,
                "start_month": None,
                "start_day": None,
                "poster_url": _cover_url(mid, m.get("relationships")),
                "banner_url": None,
                "description": _description(attrs),
                "genres": _tags(attrs),
                "rating": None,
                "status": _status_map(attrs.get("status")),
                "episodes": None,
                "chapters": None,
                "format": (attrs.get("originalLanguage") or "").upper() or None,
                "duration": None,
                "countryOfOrigin": (attrs.get("originalLanguage") or "").upper() or None,
                "related_ids": [],
                "relations": [],
            }

        if use_cache:
            return self._cached(f"details:{sid}", fetch)
        return fetch()

    def get_trending_manga(self, page: int = 1) -> dict:
        # Highest followed among recent — proxy for "trending"
        return self._cached(
            f"trend:{page}",
            lambda: self._list_manga({
                "order[followedCount]": "desc",
                "order[updatedAt]": "desc",
            }, page),
        )

    def get_airing_manga(self, page: int = 1) -> dict:
        # Ongoing adult manga / manhwa
        return self._cached(
            f"air:{page}",
            lambda: self._list_manga({
                "status[]": ["ongoing"],
                "order[followedCount]": "desc",
            }, page),
        )

    def get_popular_manga(self, page: int = 1) -> dict:
        return self._cached(
            f"pop:{page}",
            lambda: self._list_manga({
                "order[followedCount]": "desc",
            }, page),
        )
