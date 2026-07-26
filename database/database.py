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
searches_col = _db["searches"]
recent_searches_col = _db["recent_searches"]
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










def get_anime(anime_id: int) -> dict | None:
    return _to_anime(anime_col.find_one({"_id": anime_id}))


def list_available() -> list[dict]:
    """Every posted title in MongoDB. Since a title is only ever saved
    once it has a join link (see upsert_anime/delete_anime_family), this
    is effectively already "linked only" — but it's still the raw,
    anything the public-facing API layer additionally filters."""
    docs = anime_col.find().collation({"locale": "en", "strength": 2}).sort("title", ASCENDING)
    return [_to_anime(d) for d in docs]








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


AD_DOC_ID = "current"


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


