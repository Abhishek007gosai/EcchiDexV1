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
    # --- Branding ---
    BRAND_NAME = os.environ.get("BRAND_NAME", "H Index")
    BRAND_HANDLE = os.environ.get("BRAND_HANDLE", "HINDEX")
    # Optional banner image shown above the /anidex welcome message
    BANNER_IMAGE_URL = os.environ.get("BANNER_IMAGE_URL", "")

    # /anidex welcome message. Supports {first_name} and {brand_name}
    # placeholders, filled in when the command runs. Uses Telegram
    # Markdown (e.g. _italics_, *bold*). Since env vars are single-line,
    # write literal "\n" for line breaks — they're converted to real
    # newlines below.
    START_MSG = os.environ.get(
        "START_MSG",
        "HELLO {first_name}\\n\\n"
        "I am {brand_name} bot. Use /anidex to browse, search and request anime.\\n\\n"
        "\U0001f4fa Browse trending anime, search for your favorites, and "
        "request anime that isn't available yet.\\n\\n"
        "_Your all-in-one anime station._",
    ).replace("\\n", "\n")

    # --- Telegram bot (Bot API) ---
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
    # Public HTTPS base URL of this deployment, e.g. https://anime-index.onrender.com
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
    # Channel/group the bot posts request + report notifications to (e.g. -1001234567890)
    LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
    # Telegram user IDs allowed to run /addpost, /delpost, and edit links in-app
    ADMIN_IDS = _split_ids(os.environ.get("ADMIN_IDS", ""))

    # --- Telegram API (MTProto — api_id/api_hash from my.telegram.org) ---
    # Not used by the current Bot-API-only code path. Reserved for a future
    # MTProto client (e.g. Pyrogram/Telethon) if deeper features are added
    # later, such as verifying a join link actually resolves to a real,
    # joinable channel before saving it.
    API_ID = os.environ.get("API_ID", "")
    API_HASH = os.environ.get("API_HASH", "")

    # --- App / server ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    PORT = int(os.environ.get("PORT", 8000))
    DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

    # --- Database (MongoDB) ---
    # Full connection string, e.g. mongodb+srv://user:pass@cluster.mongodb.net
    MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
    MONGODB_NAME = os.environ.get("MONGODB_NAME", "anime_index")

    # --- External metadata sources ---
    ANILIST_ENDPOINT = "https://graphql.anilist.co"

    # Catalog cache lifetime (seconds). One TTL for all AniList feeds.
    CATALOG_CACHE_TTL = int(os.environ.get("CATALOG_CACHE_TTL", "1800"))  # 30 min
    # Bump to wipe catalog_cache on next boot after a breaking change.
    CACHE_GENERATION = os.environ.get("CACHE_GENERATION", "3")
