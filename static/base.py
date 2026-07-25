"""
Common interface every metadata source (AniList, MyAnimeList, ...) implements.

A "source" only ever does two things: search by title, and fetch full
details for one result. Everything it returns is normalized to the same
shape so app.py and the database layer never need to know which source
an entry came from.

Normalized detail dict shape:
    {
        "source": "anilist" | "myanimelist",
        "source_id": str | int,
        "title": str,
        "year": int | None,
        "poster_url": str | None,
        "banner_url": str | None,
        "description": str,
        "genres": list[str],
        "rating": float | None,   # out of 10
    }
"""

from abc import ABC, abstractmethod


class AnimeSource(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, query: str, page: int = 1) -> dict:
        """Return {"results": [{"source_id", "title", "year", "poster_url"}, ...],
        "has_next": bool}. Cheap/partial data only — full details are fetched
        separately once the user picks the correct result."""
        raise NotImplementedError

    @abstractmethod
    def get_details(self, source_id) -> dict:
        """Return the full normalized detail dict for one title."""
        raise NotImplementedError
