"""
Registry of available metadata sources. MyAnimeList (via Jikan) was removed
— its public API was too unreliable in practice. AniList is the sole
source for now; add another by implementing plugins/base.py's interface
and registering it here.
"""

from plugins.anilist import AniListSource

anilist = AniListSource()

SOURCES = {
    "anilist": anilist,
}

__all__ = ["SOURCES", "anilist"]
