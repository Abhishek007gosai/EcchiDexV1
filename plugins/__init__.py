"""
Registry of available metadata sources.
AniList is used for adult anime (hentai) and adult manga/manhwa (pornhwa).
"""

from plugins.anilist import AniListSource

anilist = AniListSource()

SOURCES = {
    "anilist": anilist,
}

__all__ = ["SOURCES", "anilist"]
