"""
MongoDB data layer for Anime Index.

IDs are kept as small sequential integers (via a `counters` collection)
rather than raw Mongo ObjectIds — Flask's route converters (e.g.
<int:anime_id>) and the bot's callback_data parsing both expect plain
integers, and this keeps that working unchanged.

Every function here mirrors the shape app.py already expects: dicts with
plain keys (anime "id", not "_id"), lists for genres, etc.
"""

import time

from pymongo import ASCENDING, MongoClient

from config import Config

_client = MongoClient(Config.MONGODB_URL)
_db = _client[Config.MONGODB_NAME]

anime_col = _db["anime"]
users_col = _db["users"]
reports_col = _db["reports"]
votes_col = _db["votes"]
ads_col = _db["ads"]
searches_col = _db["searches"]
notifications_col = _db["notifications"]
counters_col = _db["counters"]


def init_db():
    anime_col.create_index([("source", ASCENDING), ("source_id", ASCENDING)], unique=True)
    anime_col.create_index([("title", ASCENDING)])
    votes_col.create_index([("title", ASCENDING)])
    searches_col.create_index([("count", ASCENDING)])


def _next_id(counter_name: str) -> int:
    doc = counters_col.find_one_and_update(
        {"_id": counter_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return doc["seq"]


# ---------------------------------------------------------------------------
# Anime catalog
# ---------------------------------------------------------------------------

def _to_anime(doc) -> dict | None:
    if not doc:
        return None
    d = dict(doc)
    d["id"] = d.pop("_id")
    d["genres"] = d.get("genres") or []
    d["available"] = bool(d.get("join_link"))
    return d


def upsert_anime(details: dict, added_by: int | None = None) -> int:
    """Insert a new catalog entry from a normalized source dict, or update
    the existing one if this (source, source_id) was already posted.

    If this is a brand-new post and any of its AniList-linked prequel/
    sequel seasons are already posted with a join link set, the new post
    automatically inherits that same link — so adding "Season 2" of
    something you've already linked doesn't need a separate /editpost.
    """
    now = time.time()
    existing = anime_col.find_one({"source": details["source"], "source_id": str(details["source_id"])})
    related_ids = [str(x) for x in details.get("related_ids", [])]

    fields = {
        "title": details["title"],
        "alt_title": details.get("alt_title"),
        "year": details.get("year"),
        "poster_url": details.get("poster_url"),
        "banner_url": details.get("banner_url"),
        "description": details.get("description"),
        "genres": details.get("genres", []),
        "rating": details.get("rating"),
        "status": details.get("status"),
        "episodes": details.get("episodes"),
        "format": details.get("format"),
        "duration": details.get("duration"),
        "related_ids": related_ids,
        "updated_at": now,
    }

    if existing:
        anime_col.update_one({"_id": existing["_id"]}, {"$set": fields})
        return existing["_id"]

    inherited_link = None
    if related_ids:
        related_doc = anime_col.find_one({
            "source": details["source"],
            "source_id": {"$in": related_ids},
            "join_link": {"$nin": [None, ""]},
        })
        if related_doc:
            inherited_link = related_doc["join_link"]

    new_id = _next_id("anime")
    anime_col.insert_one({
        "_id": new_id,
        "source": details["source"],
        "source_id": str(details["source_id"]),
        "join_link": inherited_link,
        "added_by": added_by,
        "created_at": now,
        **fields,
    })
    return new_id


def delete_anime(anime_id: int):
    anime_col.delete_one({"_id": anime_id})


def get_anime(anime_id: int) -> dict | None:
    return _to_anime(anime_col.find_one({"_id": anime_id}))


def list_available() -> list[dict]:
    """Every post in the local library — a post appears here as soon as
    /addpost creates it, whether or not a join link has been set yet."""
    docs = anime_col.find().collation({"locale": "en", "strength": 2}).sort("title", ASCENDING)
    return [_to_anime(d) for d in docs]


def search_local(query: str) -> list[dict]:
    docs = (
        anime_col.find({"title": {"$regex": query, "$options": "i"}})
        .collation({"locale": "en", "strength": 2})
        .sort("title", ASCENDING)
    )
    return [_to_anime(d) for d in docs]


def update_link(anime_id: int, link: str):
    anime_col.update_one(
        {"_id": anime_id},
        {"$set": {"join_link": link or None, "updated_at": time.time()}},
    )


def propagate_join_link(anime_id: int, link: str) -> int:
    """After setting anime_id's join link, apply the same link to any
    other already-posted seasons AniList lists as its direct prequel/
    sequel. Returns how many other posts were updated. Only propagates
    direct relations from this one call — a distant season three hops
    away in the franchise won't be picked up unless it's also directly
    related to something already linked."""
    if not link:
        return 0
    doc = anime_col.find_one({"_id": anime_id})
    if not doc:
        return 0
    related_ids = doc.get("related_ids") or []
    if not related_ids:
        return 0
    result = anime_col.update_many(
        {"source": doc["source"], "source_id": {"$in": related_ids}},
        {"$set": {"join_link": link, "updated_at": time.time()}},
    )
    return result.modified_count


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_or_create_user(telegram_id: int, username: str | None, first_name: str | None,
                        is_admin: bool) -> dict:
    role = "admin" if is_admin else "member"
    existing = users_col.find_one({"_id": telegram_id})

    if existing:
        users_col.update_one(
            {"_id": telegram_id},
            {"$set": {"username": username, "first_name": first_name, "role": role}},
        )
        existing.update(username=username, first_name=first_name, role=role)
        existing["telegram_id"] = existing.pop("_id")
        return existing

    now = time.time()
    doc = {
        "_id": telegram_id, "username": username, "first_name": first_name,
        "role": role, "access": "active", "registered_at": now,
    }
    users_col.insert_one(dict(doc))
    doc["telegram_id"] = doc.pop("_id")
    return doc


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def create_report(anime_id: int | None, anime_title: str, reason: str, details: str,
                   reported_by: int | None, reported_by_name: str | None) -> int:
    new_id = _next_id("reports")
    reports_col.insert_one({
        "_id": new_id,
        "anime_id": anime_id,
        "anime_title": anime_title,
        "reason": reason,
        "details": details,
        "reported_by": reported_by,
        "reported_by_name": reported_by_name,
        "created_at": time.time(),
    })
    return new_id


# ---------------------------------------------------------------------------
# Votes — "demand signal" for anime that isn't posted yet, replacing the
# old Request Anime feature. Keyed by a normalized (lowercased) title so
# "One Piece" and "one piece" count as the same title.
# ---------------------------------------------------------------------------

def _vote_key(title: str) -> str:
    return title.strip().lower()


def record_vote(title: str, telegram_id: int) -> dict:
    """Returns {"count": int, "already_voted": bool}. Each Telegram user can
    only count once per title — re-voting just returns the current count."""
    key = _vote_key(title)
    existing = votes_col.find_one({"_id": key})
    if existing and telegram_id in existing.get("voters", []):
        return {"count": existing["count"], "already_voted": True}

    updated = votes_col.find_one_and_update(
        {"_id": key},
        {
            "$setOnInsert": {"title": title, "created_at": time.time()},
            "$addToSet": {"voters": telegram_id},
            "$inc": {"count": 1},
        },
        upsert=True,
        return_document=True,
    )
    return {"count": updated["count"], "already_voted": False}


def get_vote_count(title: str) -> int:
    doc = votes_col.find_one({"_id": _vote_key(title)})
    return doc["count"] if doc else 0


# ---------------------------------------------------------------------------
# Ads — a single active promotional post shown at the top of the Available
# tab. Only one ad runs at a time; creating a new one replaces the old.
# ---------------------------------------------------------------------------

AD_DOC_ID = "current"


def set_ad(image_url: str | None, caption: str, link: str | None, expires_at: float) -> None:
    ads_col.replace_one(
        {"_id": AD_DOC_ID},
        {
            "_id": AD_DOC_ID,
            "image_url": image_url,
            "caption": caption,
            "link": link,
            "created_at": time.time(),
            "expires_at": expires_at,
            "taps": 0,
            "clicks": 0,
        },
        upsert=True,
    )


def get_active_ad() -> dict | None:
    """Returns the live ad, or None if there isn't one / it just expired.
    Expiry is checked lazily here (no background scheduler in this
    process) — the first read after expires_at clears the stats from
    Mongo automatically, per the "don't keep ad data around after it
    ends" requirement."""
    doc = ads_col.find_one({"_id": AD_DOC_ID})
    if not doc:
        return None
    if time.time() >= doc["expires_at"]:
        ads_col.delete_one({"_id": AD_DOC_ID})
        return None
    return doc


def record_ad_tap():
    ads_col.update_one({"_id": AD_DOC_ID}, {"$inc": {"taps": 1}})


def record_ad_click():
    ads_col.update_one({"_id": AD_DOC_ID}, {"$inc": {"clicks": 1}})


def get_ad_stats() -> dict | None:
    """Like get_active_ad, but also returns already-expired stats one last
    time (without deleting) so /adstats can report a final tally right at
    the moment it ends, before the next read clears it."""
    doc = ads_col.find_one({"_id": AD_DOC_ID})
    return doc


def clear_ad() -> dict | None:
    doc = ads_col.find_one({"_id": AD_DOC_ID})
    ads_col.delete_one({"_id": AD_DOC_ID})
    return doc


# ---------------------------------------------------------------------------
# Search tracking — powers the Search page's "Popular Searches" list.
# ---------------------------------------------------------------------------

def record_search(query: str) -> None:
    query = query.strip()
    if len(query) < 2:
        return
    key = query.lower()
    searches_col.update_one(
        {"_id": key},
        {"$setOnInsert": {"display": query}, "$inc": {"count": 1}, "$set": {"last_searched": time.time()}},
        upsert=True,
    )


def get_popular_searches(limit: int = 6) -> list[dict]:
    docs = searches_col.find().sort("count", -1).limit(limit)
    return [{"query": d["display"], "count": d["count"]} for d in docs]


def clear_popular_searches() -> None:
    searches_col.delete_many({})


# ---------------------------------------------------------------------------
# Notifications — admin broadcasts pushed via /wbroadcast, shown in the
# mini app's Notifications tab exactly as sent (thumbnail + caption + link).
# ---------------------------------------------------------------------------

def create_notification(image_url: str | None, caption: str, link: str | None, expires_at: float) -> int:
    new_id = _next_id("notifications")
    notifications_col.insert_one({
        "_id": new_id,
        "image_url": image_url,
        "caption": caption,
        "link": link,
        "created_at": time.time(),
        "expires_at": expires_at,
    })
    return new_id


def list_notifications(limit: int = 30) -> list[dict]:
    now = time.time()
    notifications_col.delete_many({"expires_at": {"$lte": now}})
    docs = notifications_col.find({"expires_at": {"$gt": now}}).sort("created_at", -1).limit(limit)
    out = []
    for d in docs:
        item = dict(d)
        item["id"] = item.pop("_id")
        out.append(item)
    return out

