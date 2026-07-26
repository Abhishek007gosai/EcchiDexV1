"""
Central configuration for Anime Index.

Everything here is driven by environment variables so the exact same
codebase runs unmodified locally, on Render, and on Koyeb. See
.env.example for the full list of variables you need to set.
"""

import os


def _split_ids(raw: str) -> list[int]:
    ids = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    return ids


class Config:
    
    BRAND_NAME = os.environ.get("BRAND_NAME", "Anime Eternals")
    BRAND_HANDLE = os.environ.get("BRAND_HANDLE", "ANIME ETERNALS")
    
    BANNER_IMAGE_URL = os.environ.get("BANNER_IMAGE_URL", "")

    
    
    
    
    
    START_MSG = os.environ.get(
        "START_MSG",
        "HELLO {first_name}\\n\\n"
        "I am {brand_name} bot. Use /anidex to browse, search and request anime.\\n\\n"
        "\U0001f4fa Browse trending anime, search for your favorites, and "
        "request anime that isn't available yet.\\n\\n"
        "_Your all-in-one anime station._",
    ).replace("\\n", "\n")

    
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
    
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
    
    LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
    ADMIN_IDS = _split_ids(os.environ.get("ADMIN_IDS", ""))

    
    
    
    
    
    API_ID = os.environ.get("API_ID", "")
    API_HASH = os.environ.get("API_HASH", "")

    
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    PORT = int(os.environ.get("PORT", 8000))
    DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

    
    
    MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
    MONGODB_NAME = os.environ.get("MONGODB_NAME", "anime_index")

    
    ANILIST_ENDPOINT = "https://graphql.anilist.co"

    
    
    CATALOG_CACHE_TTL = int(os.environ.get("CATALOG_CACHE_TTL", "600"))
