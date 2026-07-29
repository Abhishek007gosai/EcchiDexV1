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
from pymongo.errors import DuplicateKeyError

from config import Config

_client = MongoClient(Config.MONGODB_URL)
_db = _client[Config.MONGODB_NAME]

anime_col = _db["anime"]
users_col = _db["users"]
reports_col = _db["reports"]
requests_col = _db["requests"]
searches_col = _db["searches"]
recent_searches_col = _db["recent_searches"]
counters_col = _db["counters"]


def init_db():
    anime_col.create_index([("source", ASCENDING), ("source_id", ASCENDING)], unique=True)
    anime_col.create_index([("title", ASCENDING)])
    requests_col.create_index([("key", ASCENDING), ("requested_by", ASCENDING)])
    requests_col.create_index([("status", ASCENDING)])
    requests_col.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    requests_col.create_index([("requested_by", ASCENDING), ("seen", ASCENDING)])
    requests_col.create_index([("requested_by", ASCENDING), ("responded_at", ASCENDING)])
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


def get_franchise_neighbors(details: dict) -> list[dict]:
    """Walk the full franchise relation graph (same traversal as
    _family_source_ids — seasons, OVAs, movies, spin-offs, alternates) and,
    considering only titles that are actually posted (have a join_link),
    line the whole family up in release-chronological order (year, then
    month/day to break ties within a year). Returns just the entry
    immediately before `details` and the entry immediately after it in
    that timeline — never more than two — so a detail sheet always shows
    at most one "Prequel" card and one "Sequel" card, regardless of how
    many AniList relation edges (Side Story, Alternative, Spin-off, ...)
    the title actually has. `details` doesn't need to be posted itself —
    its own year/month/day (from AniList) are used to place it in the
    timeline even before it has a join link, so browsing an unposted
    title still shows correct neighbors once other family members are
    posted."""
    source = details["source"]
    source_id = str(details["source_id"])
    related_ids = [str(x) for x in details.get("related_ids") or []]

    family_ids = _family_source_ids(source, related_ids)
    family_ids.discard(source_id)
    docs = list(anime_col.find({
        "source": source,
        "source_id": {"$in": list(family_ids)},
        "join_link": {"$nin": [None, ""]},
    }))
    docs.append({
        "_id": None,
        "source_id": source_id,
        "title": details.get("title"),
        "poster_url": details.get("poster_url"),
        "year": details.get("year"),
        "start_month": details.get("start_month"),
        "start_day": details.get("start_day"),
    })
    if len(docs) < 2:
        return []

    def sort_key(d):
        return (
            d.get("year") if d.get("year") is not None else 9999,
            d.get("start_month") if d.get("start_month") is not None else 13,
            d.get("start_day") if d.get("start_day") is not None else 32,
            str(d.get("_id")) if d.get("_id") is not None else d["source_id"],
        )

    docs.sort(key=sort_key)
    idx = next(i for i, d in enumerate(docs) if d["source_id"] == source_id)

    out = []
    if idx > 0:
        p = docs[idx - 1]
        out.append({"id": p["_id"], "title": p["title"], "poster_url": p.get("poster_url"), "relation_type": "PREQUEL"})
    if idx < len(docs) - 1:
        s = docs[idx + 1]
        out.append({"id": s["_id"], "title": s["title"], "poster_url": s.get("poster_url"), "relation_type": "SEQUEL"})
    return out


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
        "start_month": details.get("start_month"),
        "start_day": details.get("start_day"),
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
        "relations": details.get("relations", []),
        "updated_at": now,
    }

    if existing:
        anime_col.update_one({"_id": existing["_id"]}, {"$set": fields})
        return existing["_id"]

    inherited_link = find_inherited_link(details["source"], related_ids)

    new_id = _next_id("anime")
    try:
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
    except DuplicateKeyError:
        # A concurrent call for this exact (source, source_id) — e.g. two
        # overlapping franchise-propagation runs both discovering the same
        # unposted related title at the same time, or a client/proxy
        # retrying a slow request — already inserted it between our
        # existence check above and this insert. That other insert wins;
        # just update the doc it created instead of crashing.
        existing = anime_col.find_one({"source": details["source"], "source_id": str(details["source_id"])})
        if not existing:
            raise  # shouldn't happen — surface it rather than hide a real bug
        anime_col.update_one({"_id": existing["_id"]}, {"$set": fields})
        return existing["_id"]


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


def find_by_source_id(source: str, source_id: str) -> dict | None:
    return _to_anime(anime_col.find_one({"source": source, "source_id": str(source_id)}))


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
# Requests — replaces the old Votes "demand signal". Unlike a vote count,
# a request is something an admin actually responds to (accepted/rejected),
# so each request is its own row (not just an incrementing counter) with
# its own status the requester can be notified about. Rows are keyed by a
# normalized (lowercased) title so "One Piece" and "one piece" count as the
# same title, and grouped that way so one admin decision reaches every user
# who asked for it.
# ---------------------------------------------------------------------------

def _request_key(title: str) -> str:
    return title.strip().lower()


def _request_ref(doc: dict) -> str:
    """A short human-facing reference like 'AR-20260727-001' — cosmetic
    (for the notification card footer), built from the request's own id
    and creation date rather than stored separately."""
    date_part = time.strftime("%Y%m%d", time.localtime(doc.get("created_at") or time.time()))
    return f"AR-{date_part}-{str(doc['_id']).zfill(3)}"


DEFAULT_ACCEPT_NOTE = "Good news! Your requested anime has been accepted and will be added soon."
DEFAULT_REJECT_NOTE = "Sorry, we're not able to add this title right now."

REQUEST_PENDING_TTL_SECONDS = 24 * 60 * 60  # an unanswered request auto-deletes after 24h


def _expire_stale_pending_requests() -> None:
    """A request an admin never accepts or rejects would otherwise sit as
    'pending' forever. This process has no always-on background worker to
    run a real scheduled job on — it only runs its event loop while
    actually handling a request (see _delete_message_later's docstring in
    app.py for the same constraint) — so instead this sweep just runs
    opportunistically every time pending requests are created or read,
    which in practice happens often enough that nothing sits expired for
    long."""
    cutoff = time.time() - REQUEST_PENDING_TTL_SECONDS
    requests_col.delete_many({"status": "pending", "created_at": {"$lt": cutoff}})


MAX_PENDING_REQUESTS_PER_USER = 5


def create_request(title: str, source: str | None, source_id, poster_url: str | None,
                    genres: list[str] | None, telegram_id: int, telegram_name: str | None) -> dict:
    """Returns {"status": str, "already_requested": bool, "id": int | None}.
    Each Telegram user gets at most one active request per title — asking
    again while it's still pending (or already accepted) just returns the
    current status. A title that was previously rejected can be requested
    again, which reopens it as a fresh pending request — but only up to
    MAX_PENDING_REQUESTS_PER_USER pending requests at once per user; past
    that, status is "limit_reached" and nothing is written, so someone
    can't flood the admin queue with an unbounded backlog."""
    _expire_stale_pending_requests()
    key = _request_key(title)
    existing = requests_col.find_one({"key": key, "requested_by": telegram_id})

    if existing and existing["status"] != "rejected":
        return {"status": existing["status"], "already_requested": True, "id": existing["_id"]}

    pending_count = requests_col.count_documents({"requested_by": telegram_id, "status": "pending"})
    if pending_count >= MAX_PENDING_REQUESTS_PER_USER:
        return {
            "status": "limit_reached", "already_requested": False, "id": None,
            "limit": MAX_PENDING_REQUESTS_PER_USER,
        }

    now = time.time()
    if existing:
        requests_col.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "status": "pending", "created_at": now, "responded_at": None,
                "seen": True, "note": None, "poster_url": poster_url, "genres": genres or [],
            }},
        )
        return {"status": "pending", "already_requested": False, "id": existing["_id"]}

    new_id = _next_id("requests")
    requests_col.insert_one({
        "_id": new_id,
        "key": key,
        "title": title,
        "source": source,
        "source_id": str(source_id) if source_id is not None else None,
        "poster_url": poster_url,
        "genres": genres or [],
        "requested_by": telegram_id,
        "requested_by_name": telegram_name,
        "status": "pending",
        "created_at": now,
        "responded_at": None,
        "note": None,
        # The requester made this themselves, so there's nothing new for
        # the notification bell to surface yet — "seen" only turns false
        # once an admin changes the status out from under them.
        "seen": True,
    })
    return {"status": "pending", "already_requested": False, "id": new_id}


def list_pending_requests() -> list[dict]:
    """One row per distinct requested title (not per requester), for the
    admin queue — grouped so an admin sees "12 people want Title X" instead
    of 12 separate identical-looking rows."""
    _expire_stale_pending_requests()
    pipeline = [
        {"$match": {"status": "pending"}},
        {"$sort": {"created_at": 1}},
        {"$group": {
            "_id": "$key",
            "title": {"$first": "$title"},
            "source": {"$first": "$source"},
            "source_id": {"$first": "$source_id"},
            "poster_url": {"$first": "$poster_url"},
            "count": {"$sum": 1},
            "first_requested_at": {"$min": "$created_at"},
        }},
        {"$sort": {"count": -1, "first_requested_at": 1}},
    ]
    docs = list(requests_col.aggregate(pipeline))
    for d in docs:
        d["key"] = d.pop("_id")
    return docs


def respond_to_request(key: str, status: str, note: str | None = None) -> int:
    """Apply an admin decision (accepted/rejected) to every pending request
    for this title at once, and flag each as unseen so the requester's
    notification bell picks it up. Returns how many requests were updated."""
    result = requests_col.update_many(
        {"key": key, "status": "pending"},
        {"$set": {
            "status": status,
            "responded_at": time.time(),
            "seen": False,
            "note": note or (DEFAULT_ACCEPT_NOTE if status == "accepted" else DEFAULT_REJECT_NOTE),
        }},
    )
    return result.modified_count


def resolve_request_by_id(request_id: int, status: str, note: str | None = None) -> int | None:
    """Look up a single request row by its own numeric id — this is what
    lets the log-channel Accept/Reject buttons use a short numeric
    callback_data (Telegram caps callback_data at 64 bytes, and a full
    title easily blows past that) — then apply the decision to every
    pending request sharing that title, same as respond_to_request.
    Returns None (instead of a count) if this particular request was
    already resolved, so a double-tap on the log message's buttons is a
    harmless no-op rather than re-firing notifications."""
    doc = requests_col.find_one({"_id": request_id})
    if not doc or doc["status"] != "pending":
        return None
    return respond_to_request(doc["key"], status, note)


def accept_requests_for_title(title: str) -> int:
    """Convenience wrapper around respond_to_request for the common case:
    a title just got posted (a join link was set), so any pending request
    for that exact title is resolved automatically — the admin doesn't
    have to separately visit the requests queue for every title they add
    directly. Returns how many requests were updated."""
    return respond_to_request(_request_key(title), "accepted")


NOTIFICATION_TTL_SECONDS = 24 * 60 * 60  # notifications auto-expire after 24h


def get_user_notifications(telegram_id: int, limit: int = 30) -> dict:
    """The current user's own resolved requests (accepted/rejected) from the
    last 24 hours, most recent first, plus how many of those they haven't
    seen yet — that unseen count is what the notification bell badge shows.
    Anything resolved more than 24h ago has aged out and no longer appears,
    even if it was never opened. A user who has never requested anything
    (or whose requests are all still pending, or all expired) gets an empty
    list and a zero count, i.e. an empty bell."""
    _expire_stale_pending_requests()
    cutoff = time.time() - NOTIFICATION_TTL_SECONDS
    fresh_filter = {
        "requested_by": telegram_id,
        "status": {"$ne": "pending"},
        "responded_at": {"$gte": cutoff},
    }
    docs = requests_col.find(fresh_filter).sort("responded_at", -1).limit(limit)
    notifications = [
        {
            "id": d["_id"],
            "ref": _request_ref(d),
            "title": d["title"],
            "poster_url": d.get("poster_url"),
            "genres": d.get("genres") or [],
            "status": d["status"],
            "note": d.get("note") or (DEFAULT_ACCEPT_NOTE if d["status"] == "accepted" else DEFAULT_REJECT_NOTE),
            "requested_by_name": d.get("requested_by_name"),
            "responded_at": d.get("responded_at"),
            "seen": d.get("seen", True),
        }
        for d in docs
    ]
    unseen_count = requests_col.count_documents({**fresh_filter, "seen": False})
    return {"unseen_count": unseen_count, "notifications": notifications}


def mark_notifications_seen(telegram_id: int) -> None:
    requests_col.update_many(
        {"requested_by": telegram_id, "seen": False},
        {"$set": {"seen": True}},
    )


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

