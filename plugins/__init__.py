"""
Registry of available metadata sources.
- anilist: adult anime (hentai)
- mangadex: adult manga / manhwa (pornhwa) — faster, no GraphQL rate walls
"""

from plugins.anilist import AniListSource
from plugins.mangadex import MangaDexSource

anilist = AniListSource()
mangadex = MangaDexSource()

SOURCES = {
    "anilist": anilist,
    "mangadex": mangadex,
}

__all__ = ["SOURCES", "anilist", "mangadex"]
