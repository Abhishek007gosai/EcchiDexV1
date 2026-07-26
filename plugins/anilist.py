"""
AniList adapter — public GraphQL API, no API key required.
https://anilist.gitbook.io/anilist-apiv2-docs/
"""

import logging
import threading
import time
from collections import deque

import requests

from config import Config
from plugins.base import AnimeSource

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Paces outgoing requests so we stay under AniList's own limit.

    AniList's published limit fluctuates — 90 requests/minute normally,
    but as low as 30/minute during one of its periodic "degraded"
    states — and it's enforced per-source-IP, not per-request. A burst
    (e.g. Home firing trending/popular/most-popular at once, or several
    people opening the mini app together) can blow through that ceiling
    in a couple of seconds even though the average rate is fine. This
    just makes calls wait their turn instead of firing all at once and
    bouncing off a 429.
    """

    def __init__(self, max_per_minute: int):
        self._max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._timestamps: deque = deque()

    def wait_for_slot(self):
        while True:
            with self._lock:
                now = time.time()
                while self._timestamps and now - self._timestamps[0] > 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max_per_minute:
                    self._timestamps.append(now)
                    return
                sleep_for = 60 - (now - self._timestamps[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))

SEARCH_QUERY = """
query ($search: String, $page: Int) {
  Page(page: $page, perPage: 15) {
    pageInfo { hasNextPage }
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english }
      startDate { year }
      coverImage { extraLarge large }
      averageScore
      genres
      format
      episodes
    }
  }
}
"""

DETAILS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english }
    startDate { year }
    coverImage { large extraLarge }
    bannerImage
    description(asHtml: false)
    genres
    averageScore
    status
    episodes
    format
    duration
    relations {
      edges {
        relationType
        node { id type }
      }
    }
  }
}
"""

DISCOVER_QUERY = """
query ($sort: [MediaSort], $page: Int) {
  Page(page: $page, perPage: 10) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: $sort) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      averageScore
      genres
      episodes
      description(asHtml: false)
    }
  }
}
"""

# Same shape as DISCOVER_QUERY, but restricted to anime that is actually
# still airing right now — used for the "Top Airing" feed so finished
# shows (e.g. Death Note) don't show up just because they're popular.
DISCOVER_AIRING_QUERY = """
query ($sort: [MediaSort], $page: Int) {
  Page(page: $page, perPage: 10) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: $sort, status: RELEASING) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      averageScore
      genres
      episodes
      description(asHtml: false)
    }
  }
}
"""

GENRE_QUERY = """
query ($genre: String, $page: Int) {
  Page(page: $page, perPage: 10) {
    pageInfo { hasNextPage }
    media(type: ANIME, genre: $genre, sort: POPULARITY_DESC) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      averageScore
    }
  }
}
"""


def _clean_description(html: str | None) -> str:
    if not html:
        return ""
    text = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<i>", "").replace("</i>", "")
    return text.strip()


def _best_title(title_obj: dict) -> str:
    return title_obj.get("english") or title_obj.get("romaji") or "Untitled"


class AniListSource(AnimeSource):
    name = "anilist"

    def __init__(self):
        self._cache: dict[str, tuple[float, dict]] = {}
        self._limiter = _RateLimiter(Config.ANILIST_RATE_LIMIT_PER_MINUTE)

    def _post(self, query: str, variables: dict) -> dict:
        # AniList's public API rate-limits aggressively, and more so
        # during one of its "degraded" periods. When several requests
        # land in the same burst (e.g. Home's trending/popular/most-
        # popular calls firing together), a few commonly come back 429.
        # Retry those with a short backoff instead of surfacing a
        # failure for what's really just "try again in a moment".
        last_exc = None
        for attempt in range(5):
            self._limiter.wait_for_slot()
            try:
                resp = requests.post(
                    Config.ANILIST_ENDPOINT,
                    json={"query": query, "variables": variables},
                    timeout=10,
                )
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                last_exc = requests.HTTPError(f"429 rate limited (attempt {attempt + 1})", response=resp)
                retry_after = resp.headers.get("Retry-After")
                # AniList's own timeout for exceeding the limit can be up
                # to 60s. Waiting that long inside a single request makes
                # the app *feel* broken (a spinner or stuck button for a
                # minute) even though it would eventually succeed. Cap the
                # wait so we fail fast instead — the cache fallback below
                # and the frontend's own retry cover the rest.
                delay = min(float(retry_after), 5.0) if retry_after else 0.8 * (attempt + 1)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()["data"]
        logger.warning("AniList request failed after retries: %s", last_exc)
        raise last_exc

    def search(self, query: str, page: int = 1) -> dict:
        data = self._post(SEARCH_QUERY, {"search": query, "page": page})
        media = data["Page"]["media"]
        results = []
        for m in media:
            score = m.get("averageScore")
            results.append({
                "source_id": m["id"],
                "anilist_id": m["id"],
                "title": _best_title(m["title"]),
                "year": (m.get("startDate") or {}).get("year"),
                "poster_url": (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
                "rating": round(score / 10, 1) if score else None,
                "genres": (m.get("genres") or [])[:3],
                "format": m.get("format"),
                "episodes": m.get("episodes"),
            })
        return {"results": results, "has_next": data["Page"]["pageInfo"]["hasNextPage"]}

    def get_details(self, source_id) -> dict:
        data = self._post(DETAILS_QUERY, {"id": int(source_id)})
        m = data["Media"]
        score = m.get("averageScore")
        titles = m.get("title") or {}
        main_title = _best_title(titles)
        alt_title = titles.get("romaji") if titles.get("english") else None
        if alt_title == main_title:
            alt_title = None

        # Every relation type that's still genuinely "this anime" (another
        # season, an OVA/movie tied to the story, a spin-off, an alternate
        # cut/compilation) — not just direct prequel/sequel — so a join
        # link set anywhere propagates across the whole franchise. Left out
        # on purpose: ADAPTATION (source manga/novel), CHARACTER (unrelated
        # series that merely shares a guest character), and OTHER (too
        # loose — often crossovers with no real franchise connection).
        SAME_FRANCHISE_RELATIONS = {
            "PREQUEL", "SEQUEL", "SIDE_STORY", "PARENT",
            "ALTERNATIVE", "SPIN_OFF", "SUMMARY", "COMPILATION", "CONTAINS",
        }
        related_ids = []
        for edge in (m.get("relations") or {}).get("edges", []):
            node = edge.get("node") or {}
            if edge.get("relationType") in SAME_FRANCHISE_RELATIONS and node.get("type") == "ANIME":
                related_ids.append(node["id"])

        return {
            "source": self.name,
            "source_id": m["id"],
            "title": main_title,
            "alt_title": alt_title,
            "year": (m.get("startDate") or {}).get("year"),
            "poster_url": (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
            "banner_url": m.get("bannerImage"),
            "description": _clean_description(m.get("description")),
            "genres": m.get("genres") or [],
            "rating": round(score / 10, 1) if score else None,
            "status": m.get("status"),
            "episodes": m.get("episodes"),
            "format": m.get("format"),
            "duration": m.get("duration"),
            "related_ids": related_ids,
        }

    # -- Extra: powers Home's Trending/Top Airing feeds (not part of the shared interface) --

    def _cached(self, key: str, fetch):
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < Config.CATALOG_CACHE_TTL:
            return cached[1]
        try:
            value = fetch()
        except Exception:
            # A live fetch failing (AniList rate-limited/degraded) is a
            # bad reason to blank out a section that has stale-but-still-
            # good data sitting right here. Serve that instead — it'll
            # refresh itself next time a fetch succeeds. Only propagate
            # the failure if we have nothing at all to fall back on.
            if cached:
                logger.warning("AniList refresh failed for %r, serving stale cache", key)
                return cached[1]
            raise
        self._cache[key] = (now, value)
        return value

    def _discover(self, sort: str, page: int = 1, query: str = DISCOVER_QUERY, cache_prefix: str = "") -> dict:
        def fetch():
            data = self._post(query, {"sort": [sort], "page": page})
            out = []
            for m in data["Page"]["media"]:
                score = m.get("averageScore")
                out.append({
                    "title": _best_title(m["title"]),
                    "poster_url": (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
                    "rating": round(score / 10, 1) if score else None,
                    "anilist_id": m["id"],
                    "genres": (m.get("genres") or [])[:3],
                    "episodes": m.get("episodes"),
                    "synopsis": _clean_description(m.get("description"))[:140],
                })
            return {"results": out, "has_next": data["Page"]["pageInfo"]["hasNextPage"]}

        return self._cached(f"{cache_prefix}{sort}:{page}", fetch)

    def get_trending(self, page: int = 1) -> dict:
        return self._discover("TRENDING_DESC", page)

    def get_popular(self, page: int = 1) -> dict:
        # Backs the "Top Airing" section — must only include anime that is
        # currently releasing, not just anime that is popular overall.
        return self._discover("POPULARITY_DESC", page, query=DISCOVER_AIRING_QUERY, cache_prefix="airing:")

    def get_most_popular(self, page: int = 1) -> dict:
        # Backs the "Popular" section — most popular anime overall,
        # regardless of airing status (unlike get_popular/"Top Airing").
        return self._discover("POPULARITY_DESC", page, cache_prefix="popular-all:")

    def browse_genre(self, genre: str, page: int = 1) -> dict:
        def fetch():
            data = self._post(GENRE_QUERY, {"genre": genre, "page": page})
            out = []
            for m in data["Page"]["media"]:
                score = m.get("averageScore")
                out.append({
                    "title": _best_title(m["title"]),
                    "poster_url": (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
                    "rating": round(score / 10, 1) if score else None,
                    "anilist_id": m["id"],
                })
            return {"results": out, "has_next": data["Page"]["pageInfo"]["hasNextPage"]}

        return self._cached(f"genre:{genre}:{page}", fetch)
