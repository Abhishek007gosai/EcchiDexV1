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
recent_searches_col = _db["recent_searches"]
notifications_col = _db["notifications"]
counters_col = _db["counters"]


def init_db():
    anime_col.create_index([("source", ASCENDING), ("source_id", ASCENDING)], unique=True)
    anime_col.create_index([("title", ASCENDING)])
    votes_col.create_index([("title", ASCENDING)])
    searches_col.create_index([("count", ASCENDING)])
    recent_searches_col.create_index([("user_id", ASCENDING), ("searched_at", ASCENDING)])


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


def _family_source_ids(source: str, start_related_ids: list[str]) -> set[str]:
    """Walk the AniList franchise relation graph (seasons, OVAs, movies,
    spin-offs, alternates/compilations) across already-posted
    entries, starting from `start_related_ids`, and return every source_id
    reachable — not just the immediate one-hop neighbors. This is what lets
    a link set on Season 1 reach Season 3 even when AniList only records a
    direct edge between 1<->2 and 2<->3, as long as Season 2 is posted."""
    seen: set[str] = set()
    frontier = [str(x) for x in start_related_ids]
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        doc = anime_col.find_one({"source": source, "source_id": sid})
        if doc:
            for rel in doc.get("related_ids") or []:
                rel = str(rel)
                if rel not in seen:
                    frontier.append(rel)
    return seen


def find_inherited_link(source: str, related_ids: list[str]) -> str | None:
    """Look for a join link anywhere in the same franchise (walking the
    full relation graph across already-posted entries). Standalone so it
    can be checked *before* a title is saved — e.g. from /addpost, to
    decide whether a brand-new post can be auto-linked immediately instead
    of prompting the admin for a link at all."""
    if not related_ids:
        return None
    family_ids = _family_source_ids(source, related_ids)
    if not family_ids:
        return None
    related_doc = anime_col.find_one({
        "source": source,
        "source_id": {"$in": list(family_ids)},
        "join_link": {"$nin": [None, ""]},
    })
    return related_doc["join_link"] if related_doc else None


def upsert_anime(details: dict, added_by: int | None = None) -> int:
    """Insert a new catalog entry from a normalized source dict, or update
    the existing one if this (source, source_id) was already posted.

    If this is a brand-new post and any other already-posted title in the
    same franchise (found by walking the full relation graph, not just
    this title's direct AniList relations) already has a join link set,
    the new post automatically inherits that same link — so adding
    "Season 3" of something you've already linked doesn't need a separate
    /editpost, even if Season 2 is the only thing directly linking them.
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

    inherited_link = find_inherited_link(details["source"], related_ids)

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


def delete_anime_family(anime_id: int) -> int:
    """Delete anime_id and every other already-posted title in the same
    franchise (seasons, OVAs, movies, spin-offs, etc. — found the same way
    propagate_join_link finds them). Used when a join link is cleared: a
    title with no link isn't a real post anymore, so it (and the rest of
    the family, which loses the same link via propagation) is removed
    from MongoDB entirely rather than left behind as an unlinked,
    unjoinable entry. Returns how many *other* posts (besides anime_id
    itself) were deleted."""
    doc = anime_col.find_one({"_id": anime_id})
    if not doc:
        return 0
    family_ids = _family_source_ids(doc["source"], doc.get("related_ids") or [])
    family_ids.discard(str(doc["source_id"]))
    other_count = 0
    if family_ids:
        result = anime_col.delete_many({"source": doc["source"], "source_id": {"$in": list(family_ids)}})
        other_count = result.deleted_count
    anime_col.delete_one({"_id": anime_id})
    return other_count


def get_anime(anime_id: int) -> dict | None:
    return _to_anime(anime_col.find_one({"_id": anime_id}))


def list_available() -> list[dict]:
    """Every posted title in MongoDB. Since a title is only ever saved
    once it has a join link (see upsert_anime/delete_anime_family), this
    is effectively already "linked only" — but it's still the raw,
    unfiltered query, used directly by admin bot commands (/editpost,
    /delpost, /refreshposts) that need to find a post regardless of
    anything the public-facing API layer additionally filters."""
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
    """After setting (or clearing) anime_id's join link, apply the same
    value to every other already-posted title in the same franchise —
    found by walking the AniList franchise relation graph across posted
    entries, so the whole family (seasons, OVAs, movies, spin-offs, etc.)
    stays in sync either way: a link set anywhere reaches the rest of the
    family, and clearing a link anywhere clears it everywhere too, so a
    removed post also disappears from the "Available" tab across the
    board rather than leaving stale linked entries behind. Returns how
    many other posts were updated."""
    doc = anime_col.find_one({"_id": anime_id})
    if not doc:
        return 0
    family_ids = _family_source_ids(doc["source"], doc.get("related_ids") or [])
    family_ids.discard(str(doc["source_id"]))
    if not family_ids:
        return 0
    result = anime_col.update_many(
        {"source": doc["source"], "source_id": {"$in": list(family_ids)}},
        {"$set": {"join_link": link or None, "updated_at": time.time()}},
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
# Recent searches — per-user history, kept separate from the global
# popular-searches counts above. Only the most recent MAX_RECENT_SEARCHES
# entries are kept per user.
# ---------------------------------------------------------------------------

MAX_RECENT_SEARCHES = 15


def record_recent_search(user_id: int, query: str) -> None:
    query = query.strip()
    if len(query) < 2:
        return
    key = query.lower()
    recent_searches_col.update_one(
        {"user_id": user_id, "key": key},
        {"$set": {"display": query, "searched_at": time.time()}},
        upsert=True,
    )
    # Trim anything past the most recent MAX_RECENT_SEARCHES for this user.
    extra_ids = [
        d["_id"] for d in recent_searches_col
        .find({"user_id": user_id}, {"_id": 1})
        .sort("searched_at", -1)
        .skip(MAX_RECENT_SEARCHES)
    ]
    if extra_ids:
        recent_searches_col.delete_many({"_id": {"$in": extra_ids}})


def get_recent_searches(user_id: int, limit: int = 10) -> list[dict]:
    docs = recent_searches_col.find({"user_id": user_id}).sort("searched_at", -1).limit(limit)
    return [{"query": d["display"]} for d in docs]


def clear_recent_searches(user_id: int) -> None:
    recent_searches_col.delete_many({"user_id": user_id})


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

