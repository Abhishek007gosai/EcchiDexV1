"""
Anime Index — Flask web server (serves the mini app + JSON API) and the
Telegram bot (running in webhook mode, fed by Telegram through the same
Flask process).

Why webhook mode, not polling: Render and Koyeb both run this as a "web
service" that must listen on $PORT — long-polling would fight that model
and waste a dyno doing nothing but polling. Webhook mode means Telegram
pushes updates straight to /webhook/<secret>, which is what the platforms
expect.

Important deployment note: this process keeps in-memory bot state
(SESSIONS, used for the plain-text library search picker) and a single
asyncio event loop. Run it with a single worker (see Dockerfile /
render.yaml) — multiple worker processes would each have their own copy
of this state and event loop, breaking that flow.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import parse_qsl, quote

import requests
from flask import Flask, abort, jsonify, render_template, request
from flask_compress import Compress

from config import Config
from database import database as db
from plugins import SOURCES

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = Config.SECRET_KEY
# 1 hour static cache — trims repeat-visit load time on Render/Koyeb without
# risking a stale asset for too long after a redeploy.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
# gzip every JSON/HTML/JS/CSS response. The catalog endpoints return sizeable
# JSON (poster URLs, synopses, genre lists for 10-15 titles at a time) and
# users are frequently on slow mobile connections, so this is a real win on
# both time-to-first-render and data usage, not just a nicety.
app.config["COMPRESS_MIMETYPES"] = [
    "text/html", "text/css", "text/javascript", "application/javascript",
    "application/json",
]
Compress(app)

db.init_db()

# ---------------------------------------------------------------------------
# Telegram bot (python-telegram-bot v20+, async) glued into sync Flask via a
# single long-lived event loop.
# ---------------------------------------------------------------------------

bot_app: Application | None = None
_loop = asyncio.new_event_loop()


def run_async(coro):
    return _loop.run_until_complete(coro)


# In-memory session store for short multi-step conversations (currently
# just the plain-text library search picker when there are multiple
# matches). Telegram's callback_data has a 64-byte limit, so we keep the
# real state here and only pass a short session id through callback_data.
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 15 * 60  # 15 minutes

# Fixed genre set shown on the Search page's genre tiles.
GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Romance", "Sci-Fi", "Horror"]


def new_session(**kwargs) -> str:
    sid = secrets.token_hex(4)
    kwargs["_created"] = time.time()
    SESSIONS[sid] = kwargs
    _gc_sessions()
    return sid


def _gc_sessions():
    now = time.time()
    expired = [k for k, v in SESSIONS.items() if now - v.get("_created", now) > SESSION_TTL]
    for k in expired:
        SESSIONS.pop(k, None)


def _webapp_button(label: str = None) -> InlineKeyboardButton:
    label = label or f"\U0001f4d6 Open {Config.BRAND_NAME}"
    if Config.WEBAPP_URL.startswith("https://"):
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=Config.WEBAPP_URL))
    # Telegram requires an https URL for web_app buttons — fall back to a
    # plain link so the bot still works before you have a deployed URL.
    return InlineKeyboardButton(label, url=Config.WEBAPP_URL or "https://telegram.org")


def _open_post_button(anime: dict) -> InlineKeyboardButton:
    """Deep-links straight into the mini app at this specific post, instead
    of opening the raw channel link directly."""
    if Config.WEBAPP_URL.startswith("https://"):
        url = f"{Config.WEBAPP_URL}?anime={anime['id']}"
        return InlineKeyboardButton("\u25b6 Open Post", web_app=WebAppInfo(url=url))
    return InlineKeyboardButton("\u25b6 Open Post", url=Config.WEBAPP_URL or "https://telegram.org")


def _search_in_app_button(text: str) -> InlineKeyboardButton:
    query_param = quote(text)
    label = f"\U0001f4d6 Open {Config.BRAND_NAME}"
    if Config.WEBAPP_URL.startswith("https://"):
        url = f"{Config.WEBAPP_URL}?search={query_param}"
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    return InlineKeyboardButton(label, url=Config.WEBAPP_URL or "https://telegram.org")


# --- Commands -------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start no longer shows the welcome card — it just exists so joining
    the bot doesn't feel broken. Use /anidex for the actual start menu."""
    return


async def cmd_anidex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        text = Config.START_MSG.format(first_name=user.first_name, brand_name=Config.BRAND_NAME)
    except (KeyError, IndexError, ValueError):
        text = Config.START_MSG
    keyboard = InlineKeyboardMarkup([[_webapp_button()]])
    if Config.BANNER_IMAGE_URL:
        await update.message.reply_photo(Config.BANNER_IMAGE_URL, caption=text,
                                          reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# --- Callback query routing ------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "noop":
        await q.answer()
        return

    if action == "cancel":
        sid = parts[1] if len(parts) > 1 else None
        SESSIONS.pop(sid, None)
        await q.answer("Cancelled")
        await q.edit_message_text("Cancelled.")
        return

    if action == "searchpick":
        _, sid, idx = parts
        await handle_searchpick(q, sid, int(idx))
        return

    if action == "reqaccept":
        await handle_request_accept(q, parts[1] if len(parts) > 1 else None)
        return

    if action == "reqreject":
        await show_reject_reasons(q, parts[1] if len(parts) > 1 else None)
        return

    if action == "reqreason":
        # data shape: reqreason:<request_id>:<reason_code>
        await handle_request_reject(q, parts[1] if len(parts) > 1 else None,
                                     parts[2] if len(parts) > 2 else "other")
        return

    if action == "reqback":
        await show_accept_reject(q, parts[1] if len(parts) > 1 else None)
        return

    await q.answer()


# --- Auto-search: plain text messages (no command) search the library ----

def _display_name_from_user(tg_user) -> str:
    if tg_user.username:
        return f"@{tg_user.username}"
    return tg_user.full_name or str(tg_user.id)


def _delete_message_later(chat_id: int, message_id: int, delay: float = 120):
    """Deletes a bot message `delay` seconds after it's sent (used for the
    plain-text search replies). Runs on a plain threading.Timer rather than
    an asyncio task: this process drives its event loop synchronously, once
    per incoming webhook request (see the module docstring) — nothing keeps
    it spinning in between, so an asyncio-scheduled sleep/timer would only
    ever get a chance to progress whenever some unrelated update happened
    to arrive, firing arbitrarily late or not at all. A Timer thread making
    a plain HTTP call sidesteps that, and avoids touching the bot's async
    HTTP client from a different thread/loop."""
    def _do_delete():
        try:
            requests.post(
                f"https://api.telegram.org/bot{Config.BOT_TOKEN}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=10,
            )
        except requests.RequestException:
            pass
    threading.Timer(delay, _do_delete).start()


async def send_anime_result(message, anime: dict):
    """Bot search results only ever show the name — no genres, no
    description — and the action button deep-links into the mini app at
    that exact post instead of opening the raw channel link directly."""
    sent = await message.reply_text(anime["title"], reply_markup=InlineKeyboardMarkup([[_open_post_button(anime)]]))
    _delete_message_later(sent.chat_id, sent.message_id)


async def handle_searchpick(q, sid, idx):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — search again.", show_alert=True)
        return
    match = session["matches"][idx]
    SESSIONS.pop(sid, None)
    await q.answer()
    await q.edit_message_text(match["title"], reply_markup=InlineKeyboardMarkup([[_open_post_button(match)]]))


# Quick reject reasons — chosen from buttons rather than a typed reply,
# since the log post lives in a channel where correlating a free-text
# reply back to "which pending request" is unreliable (channels don't
# guarantee reply-threading back to a bot the way private chats do).
# This still gets the requester a real reason instead of one generic line.
REJECT_REASONS = {
    "dup": "This title is already posted — check the library.",
    "unavailable": "This title isn't available right now.",
    "unreleased": "This title hasn't been released yet.",
    "other": "Sorry, we're not able to add this title right now.",
}


async def _request_admin_guard(q) -> bool:
    tg_user = q.from_user
    if not tg_user or tg_user.id not in Config.ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return False
    return True


async def _finalize_request_message(q, label: str):
    try:
        if q.message.photo:
            await q.edit_message_caption(caption=(q.message.caption or "") + f"\n\n{label}")
        else:
            await q.edit_message_text((q.message.text or "") + f"\n\n{label}")
    except Exception:
        pass


async def handle_request_accept(q, request_id_str: str | None):
    """\u2705 Accept — resolves immediately, no submenu needed."""
    if not await _request_admin_guard(q):
        return
    try:
        request_id = int(request_id_str)
    except (TypeError, ValueError):
        await q.answer()
        return
    updated = await asyncio.to_thread(db.resolve_request_by_id, request_id, "accepted")
    if updated is None:
        await q.answer("Already handled.", show_alert=True)
        return
    await q.answer("Accepted \u2705")
    await _finalize_request_message(q, f"\u2705 Accepted by {_display_name_from_user(q.from_user)}")


async def show_reject_reasons(q, request_id_str: str | None):
    """\u274c Reject — swaps the buttons for a quick-reason submenu instead of
    resolving right away, so the requester gets an actual reason."""
    if not await _request_admin_guard(q):
        return
    if not request_id_str:
        await q.answer()
        return
    await q.answer()
    rows = [
        [InlineKeyboardButton("Already posted", callback_data=f"reqreason:{request_id_str}:dup")],
        [InlineKeyboardButton("Not available", callback_data=f"reqreason:{request_id_str}:unavailable")],
        [InlineKeyboardButton("Not release yet", callback_data=f"reqreason:{request_id_str}:unreleased")],
        [InlineKeyboardButton("Other", callback_data=f"reqreason:{request_id_str}:other")],
        [InlineKeyboardButton("\u2190 Back", callback_data=f"reqback:{request_id_str}")],
    ]
    try:
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
    except Exception:
        pass


async def show_accept_reject(q, request_id_str: str | None):
    """\u2190 Back — restores the original Accept/Reject pair."""
    if not await _request_admin_guard(q):
        return
    await q.answer()
    rows = [[
        InlineKeyboardButton("\u2705 Accept", callback_data=f"reqaccept:{request_id_str}"),
        InlineKeyboardButton("\u274c Reject", callback_data=f"reqreject:{request_id_str}"),
    ]]
    try:
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
    except Exception:
        pass


async def handle_request_reject(q, request_id_str: str | None, reason_code: str):
    """A specific reason was picked from the submenu — resolve as rejected
    with that reason as the requester's notification note."""
    if not await _request_admin_guard(q):
        return
    try:
        request_id = int(request_id_str)
    except (TypeError, ValueError):
        await q.answer()
        return
    note = REJECT_REASONS.get(reason_code, REJECT_REASONS["other"])
    updated = await asyncio.to_thread(db.resolve_request_by_id, request_id, "rejected", note)
    if updated is None:
        await q.answer("Already handled.", show_alert=True)
        return
    await q.answer("Rejected \u274c")
    await _finalize_request_message(q, f"\u274c Rejected by {_display_name_from_user(q.from_user)} \u2014 {note}")


async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any plain-text message (not a command) is treated as an anime title
    search against the local library. Private chats only."""
    message = update.message
    text = (message.text or "").strip()
    chat = update.effective_chat

    if chat.type != "private":
        return

    if len(text) < 2:
        return

    local_matches = await asyncio.to_thread(db.search_local, text)
    if not local_matches:
        keyboard = InlineKeyboardMarkup([[_search_in_app_button(text)]])
        sent = await message.reply_text(
            f"'{text}' isn't posted yet. Open {Config.BRAND_NAME} to search and request it.",
            reply_markup=keyboard,
        )
        _delete_message_later(sent.chat_id, sent.message_id)
        return

    if len(local_matches) == 1:
        await send_anime_result(message, local_matches[0])
        return

    sid = new_session(kind="searchpick", matches=local_matches[:8])
    rows = [[InlineKeyboardButton(m["title"], callback_data=f"searchpick:{sid}:{i}")]
            for i, m in enumerate(local_matches[:8])]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"cancel:{sid}")])
    sent = await message.reply_text(
        f"Found {len(local_matches)} matches for '{text}':",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    _delete_message_later(sent.chat_id, sent.message_id)


# --- Notifications to the log channel -------------------------------------

def notify_new_report(title: str, reason: str, details: str, reporter_name: str):
    if not Config.LOG_CHANNEL_ID or not bot_app:
        return
    text = (
        f"\U0001f6a9 New Report\n"
        f"Anime: {title}\n"
        f"Reason: {reason}\n"
        + (f"Details: {details}\n" if details else "")
        + f"By: {reporter_name}"
    )
    run_async(bot_app.bot.send_message(Config.LOG_CHANNEL_ID, text))


def notify_new_request(request_id: int, title: str, requester_name: str, poster_url: str | None):
    # Text-only log post — no anime poster attached, even when one is
    # available, so the Logs feed stays a plain scrollable text list.
    if not Config.LOG_CHANNEL_ID or not bot_app:
        return
    text = (
        f"\U0001f4dd New Request\n"
        f"Anime: {title}\n"
        f"By: {requester_name}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Accept", callback_data=f"reqaccept:{request_id}"),
        InlineKeyboardButton("\u274c Reject", callback_data=f"reqreject:{request_id}"),
    ]])
    run_async(bot_app.bot.send_message(Config.LOG_CHANNEL_ID, text, reply_markup=keyboard))


# ---------------------------------------------------------------------------
# Telegram WebApp initData verification
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str) -> dict | None:
    if not init_data or not Config.BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_raw = parsed.get("user")
    if not user_raw:
        return None
    return json.loads(user_raw)


def current_user():
    """Returns the verified Telegram user dict for this request, or None if
    the request didn't come from inside Telegram (or failed verification)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(init_data)


def is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("id") in Config.ADMIN_IDS


# ---------------------------------------------------------------------------
# Spam / flood-wait protection
#
# In-memory sliding-window limiter, keyed per Telegram user (falling back to
# IP only for the rare unauthenticated call). This matches the app's
# existing architecture — a single gunicorn worker holding in-memory state
# (see the AniList plugin's cache, and the module docstring above) — so a
# plain dict is enough; there's no second process for it to be inconsistent
# with. Admins are exempt so moderating the queue is never throttled.
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit_key(user: dict | None) -> str:
    if user and user.get("id"):
        return f"tg:{user['id']}"
    return f"ip:{request.remote_addr or 'unknown'}"


def _check_rate_limit(bucket: str, key: str, limit: int, window_seconds: float) -> bool:
    """True if this call is allowed (and is recorded); False if `key` has
    already made `limit` calls to `bucket` within the trailing
    `window_seconds`."""
    now = time.time()
    dq_key = f"{bucket}:{key}"
    with _rate_lock:
        dq = _rate_hits[dq_key]
        while dq and dq[0] <= now - window_seconds:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def rate_limited(bucket: str, limit: int, window_seconds: float):
    """Decorator for a Flask view: rejects with 429 once the calling user
    exceeds `limit` calls to `bucket` per `window_seconds`. Use this for
    endpoints that create/send something (requests, reports) on top of the
    blanket per-request flood guard below, since spam there is cheap for
    an abuser but costly for admins reading the Logs feed."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if is_admin(user):
                return fn(*args, **kwargs)
            key = _rate_limit_key(user)
            if not _check_rate_limit(bucket, key, limit, window_seconds):
                return jsonify(error="Too many requests — please slow down and try again in a bit."), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.before_request
def _flood_guard():
    """Blanket flood-wait protection for every API call, independent of
    the stricter per-action limits above. Generous enough that normal use
    (Home's several parallel loads, fast tab-switching, typing a search)
    never comes close, but it stops a runaway client loop or a scripted
    abuser from hammering the server."""
    if not request.path.startswith("/api/"):
        return None
    user = current_user()
    if is_admin(user):
        return None
    key = _rate_limit_key(user)
    if not _check_rate_limit("global", key, 120, 60):
        return jsonify(error="Too many requests — please slow down and try again in a bit."), 429
    return None


# A Telegram public username: 5-32 chars, must start with a letter, only
# letters/digits/underscores after that (Telegram's own username rules).
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


async def _normalize_join_link_async(raw: str) -> str:
    """Turn whatever an admin pastes — a bare @username, a bare username, a
    t.me/... link missing its scheme, a numeric channel ID, or a full URL —
    into a URL that's actually safe to open. Raises ValueError with a
    user-facing message on anything that can't be turned into one.

    The numeric-channel-ID and @username cases are the actual fix for
    "Set Join Link doesn't work": previously the raw input was stored
    as-is, so pasting a bare channel ID or @username saved a string
    Telegram's openLink() can't open, and nothing ever explained why. Now
    a channel ID gets turned into a real invite link via the Bot API (the
    bot must already be an admin in that channel), and an @username gets
    turned into a proper https://t.me/<username> URL.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""  # clearing the link is always allowed

    if raw.startswith("http://") or raw.startswith("https://"):
        if "t.me/" not in raw and "telegram.me/" not in raw:
            raise ValueError("That doesn't look like a Telegram link.")
        return raw

    if raw.startswith("t.me/") or raw.startswith("telegram.me/"):
        return "https://" + raw

    if re.fullmatch(r"-?\d+", raw):
        if not bot_app:
            raise ValueError("Bot isn't connected — can't generate an invite link for a channel ID right now.")
        try:
            invite = await bot_app.bot.create_chat_invite_link(chat_id=int(raw))
            return invite.invite_link
        except Exception:
            raise ValueError(
                "Couldn't create an invite link for that channel ID — make sure the bot "
                "has been added to that channel as an admin with 'Invite Users' permission."
            )

    username = raw[1:] if raw.startswith("@") else raw
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Enter a Telegram @username, a t.me/ link, an invite link (https://t.me/+...), "
            "or a channel ID."
        )
    return f"https://t.me/{username}"


def normalize_join_link(raw: str) -> str:
    return run_async(_normalize_join_link_async(raw))


# ---------------------------------------------------------------------------
# Web app + JSON API
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", brand_name=Config.BRAND_NAME, brand_handle=Config.BRAND_HANDLE)


@app.get("/healthz")
def healthz():
    # Also opportunistically warms the Trending/Popular/Most-popular cache
    # (see plugins/anilist.py's _cached — same TTL Home reads from). On a
    # free hosting tier that spins down when idle, the very first real
    # visitor after a cold start would otherwise be the one who eats a
    # live AniList round trip on all three sections at once. Pointing an
    # external uptime monitor (UptimeRobot, cron-job.org, etc.) at this
    # endpoint every ~10 minutes keeps both the process warm *and* this
    # cache fresh, so real users essentially never see a cold load.
    # Best-effort: a slow/unreachable AniList must never fail the health
    # check itself, so failures here are swallowed.
    try:
        SOURCES["anilist"].get_trending()
        SOURCES["anilist"].get_popular()
        SOURCES["anilist"].get_most_popular()
    except Exception:
        pass
    return jsonify(status="ok")


@app.get("/api/catalog/trending")
def api_trending():
    page = request.args.get("page", 1, type=int)
    try:
        resp = jsonify(SOURCES["anilist"].get_trending(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})
    resp.headers["Cache-Control"] = f"public, max-age={Config.CATALOG_CACHE_TTL}"
    return resp


@app.get("/api/catalog/popular")
def api_popular():
    page = request.args.get("page", 1, type=int)
    try:
        resp = jsonify(SOURCES["anilist"].get_popular(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})
    resp.headers["Cache-Control"] = f"public, max-age={Config.CATALOG_CACHE_TTL}"
    return resp


@app.get("/api/catalog/most-popular")
def api_most_popular():
    page = request.args.get("page", 1, type=int)
    try:
        resp = jsonify(SOURCES["anilist"].get_most_popular(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})
    resp.headers["Cache-Control"] = f"public, max-age={Config.CATALOG_CACHE_TTL}"
    return resp


@app.post("/api/search/track")
def api_search_track():
    payload = request.get_json(force=True, silent=True) or {}
    query = (payload.get("query") or "").strip()
    if query:
        db.record_search(query)
        user = current_user()
        if user:
            db.record_recent_search(user["id"], query)
    return jsonify(status="ok")


@app.get("/api/search/popular")
def api_search_popular():
    limit = request.args.get("limit", 6, type=int)
    return jsonify(db.get_popular_searches(limit))


@app.post("/api/search/clear")
def api_search_clear():
    user = current_user()
    if not is_admin(user):
        abort(403)
    db.clear_popular_searches()
    return jsonify(status="cleared")


@app.get("/api/search/recent")
def api_search_recent():
    user = current_user()
    if not user:
        return jsonify([])
    limit = request.args.get("limit", 10, type=int)
    return jsonify(db.get_recent_searches(user["id"], limit))


@app.post("/api/search/recent/clear")
def api_search_recent_clear():
    user = current_user()
    if not user:
        abort(403)
    db.clear_recent_searches(user["id"])
    return jsonify(status="cleared")


@app.get("/api/search/anime")
def api_search_anime():
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)
    if not q:
        return jsonify({"results": [], "has_next": False})
    try:
        return jsonify(SOURCES["anilist"].search(q, page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})


@app.get("/api/genres/<genre>")
def api_genre_browse(genre):
    page = request.args.get("page", 1, type=int)
    try:
        return jsonify(SOURCES["anilist"].browse_genre(genre, page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})


@app.get("/api/catalog/available")
def api_available():
    # A title is only ever saved without being deleted again while it has
    # a join link (see upsert_anime/delete_anime_family in database.py),
    # so in practice db.list_available() is already links-only. This
    # filter is a defensive safety net for that invariant — e.g. any
    # pre-existing data from before this behavior — so the public
    # Available tab never shows an unjoinable title even if one somehow
    # exists without a link.
    return jsonify([a for a in db.list_available() if a.get("available")])


def _related_posted(details: dict) -> list[dict]:
    """The whole franchise (seasons, OVAs, movies, spin-offs, alternates —
    every entry reachable by walking AniList's relation graph) collapsed
    into a single release-chronological timeline, filtered to entries that
    are actually posted. Returns just the immediately-previous and
    immediately-next entry relative to `details` — never one card per
    AniList relation edge — so the detail sheet always shows at most a
    Prequel card and a Sequel card, no matter how large the franchise is."""
    return db.get_franchise_neighbors(details)


@app.get("/api/anime/<int:anime_id>")
def api_anime_detail(anime_id):
    anime = db.get_anime(anime_id)
    if not anime:
        abort(404)
    anime["related_posted"] = _related_posted(anime)
    return jsonify(anime)


@app.get("/api/anilist/<int:anilist_id>")
def api_anilist_details(anilist_id):
    """Full details (genres/synopsis/banner) for a Trending/Popular card —
    the lightweight discovery query doesn't include those fields."""
    try:
        details = SOURCES["anilist"].get_details(anilist_id)
    except requests.RequestException:
        abort(502)
    details["related_posted"] = _related_posted(details)
    return jsonify(details)


@app.post("/api/request")
@rate_limited("request", limit=8, window_seconds=600)
def api_request_anime():
    payload = request.get_json(force=True, silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify(error="title is required"), 400
    source = payload.get("source")
    source_id = payload.get("source_id")
    poster_url = payload.get("poster_url")
    genres = payload.get("genres")

    user = current_user()
    if not user:
        return jsonify(error="Open this inside Telegram to request an anime."), 401

    result = db.create_request(title, source, source_id, poster_url, genres, user["id"], _telegram_user_label(user))
    if result["status"] == "limit_reached":
        return jsonify(
            error=f"You've got {result['limit']} pending requests already — wait for one to be "
                  f"reviewed before requesting more."
        ), 429
    if not result["already_requested"]:
        notify_new_request(result["id"], title, _telegram_user_label(user), poster_url)
    return jsonify(result)


@app.get("/api/notifications")
def api_notifications():
    user = current_user()
    if not user:
        return jsonify(unseen_count=0, notifications=[])
    return jsonify(db.get_user_notifications(user["id"]))


@app.post("/api/notifications/seen")
def api_notifications_seen():
    user = current_user()
    if not user:
        return jsonify(error="Open this inside Telegram."), 401
    db.mark_notifications_seen(user["id"])
    return jsonify(status="ok")


@app.get("/api/admin/requests")
def api_admin_requests():
    user = current_user()
    if not is_admin(user):
        abort(403)
    return jsonify(db.list_pending_requests())


@app.patch("/api/admin/requests/<path:key>")
def api_admin_respond_request(key):
    user = current_user()
    if not is_admin(user):
        abort(403)
    payload = request.get_json(force=True, silent=True) or {}
    status = payload.get("status")
    if status not in ("accepted", "rejected"):
        return jsonify(error="status must be 'accepted' or 'rejected'"), 400
    updated = db.respond_to_request(key, status)
    return jsonify(status="ok", updated=updated)


@app.post("/api/report")
@rate_limited("report", limit=5, window_seconds=600)
def api_report():
    payload = request.get_json(force=True, silent=True) or {}
    reason = (payload.get("reason") or "").strip()
    if not reason:
        return jsonify(error="reason is required"), 400
    details = (payload.get("details") or "").strip()[:50]
    anime_id = payload.get("anime_id")
    anime_title = (payload.get("anime_title") or "").strip()

    user = current_user()
    reporter_id = user.get("id") if user else None
    reporter_name = _telegram_user_label(user) if user else "Guest"

    db.create_report(anime_id, anime_title, reason, details, reporter_id, reporter_name)
    notify_new_report(anime_title, reason, details, reporter_name)
    return jsonify(status="received"), 201


@app.get("/api/profile")
def api_profile():
    user = current_user()
    if not user:
        return jsonify(error="Open this from inside Telegram to view your profile."), 401
    profile = db.get_or_create_user(
        telegram_id=user["id"],
        username=user.get("username"),
        first_name=user.get("first_name"),
        is_admin=is_admin(user),
    )
    return jsonify(profile)


def propagate_link_full_franchise(anime_id: int, link: str) -> int:
    """Like db.propagate_join_link, but not limited to titles that are
    already posted. db.propagate_join_link can only *update* MongoDB docs
    that already exist — and since an unlinked title is never saved (see
    upsert_anime/delete_anime_family in database.py), the very first time
    you link Season 1, none of Season 2/3/4/OVAs/movies exist in MongoDB
    yet, so there'd be nothing for it to actually reach. This instead
    walks the full AniList relation graph live: for each related title
    that isn't posted yet, it fetches that title's own details from
    AniList and creates+links it on the spot, then continues the walk
    using *that* title's relations too — so linking any one entry point
    pulls in and links the whole franchise (seasons, OVAs, movies,
    spin-offs, alternates), not just whatever happened to be posted
    already. Returns how many *other* titles ended up linked."""
    doc = db.get_anime(anime_id)
    if not doc:
        return 0
    source = doc["source"]
    src = SOURCES[source]

    seen = {str(doc["source_id"])}
    frontier = [str(x) for x in (doc.get("related_ids") or [])]
    updated = 0
    MAX_FETCHES = 40  # safety cap so one link-set can't spiral into dozens of AniList calls

    while frontier and updated < MAX_FETCHES:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)

        existing = db.find_by_source_id(source, sid)
        if existing:
            db.update_link(existing["id"], link)
            updated += 1
            frontier.extend(str(x) for x in (existing.get("related_ids") or []))
            continue

        try:
            details = src.get_details(sid)
        except requests.RequestException:
            continue  # skip this branch, but keep walking the rest of the graph
        new_id = db.upsert_anime(details)
        db.update_link(new_id, link)
        updated += 1
        frontier.extend(str(x) for x in (details.get("related_ids") or []))

    return updated


@app.patch("/api/anime/<int:anime_id>/link")
def api_edit_link(anime_id):
    user = current_user()
    if not is_admin(user):
        abort(403)
    payload = request.get_json(force=True, silent=True) or {}
    raw_link = (payload.get("link") or "").strip()
    if not db.get_anime(anime_id):
        abort(404)
    try:
        link = normalize_join_link(raw_link)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if link:
        # Setting a link is also the natural moment to refresh this post's
        # cached AniList metadata (poster, genres, episode count, and
        # critically its relations list) — not just the join_link field.
        # Without this, a post created before the relations field existed
        # (or one whose franchise has grown since it was posted) would be
        # permanently stuck with stale/missing data and never show
        # Prequel/Sequel cards, since nothing else ever re-fetches it.
        anime = db.get_anime(anime_id)
        try:
            details = SOURCES[anime["source"]].get_details(anime["source_id"], use_cache=False)
            db.upsert_anime(details)
        except requests.RequestException:
            pass  # AniList unreachable — keep whatever's cached, still set the link below
        db.update_link(anime_id, link)
        propagated = propagate_link_full_franchise(anime_id, link)
        db.accept_requests_for_title(anime["title"])
        return jsonify(status="updated", link=link, propagated=propagated)
    # No link = not a real post anymore — delete it (and the rest of its
    # franchise, which just lost the link via propagation) from MongoDB
    # entirely, rather than leaving an unlinked, unjoinable entry behind.
    propagated = db.delete_anime_family(anime_id)
    return jsonify(status="deleted", link="", propagated=propagated)


@app.post("/api/anime/link-anilist/<int:anilist_id>")
def api_set_link_from_anilist(anilist_id):
    """Set a join link for a title that's only been browsed from AniList
    (Discover/Genre) and doesn't have a local library entry yet. Creates
    that entry on the fly — from this point on it's a normal posted anime
    and shows up in the Available tab, same as one added via /addpost."""
    user = current_user()
    if not is_admin(user):
        abort(403)
    payload = request.get_json(force=True, silent=True) or {}
    raw_link = (payload.get("link") or "").strip()
    if not raw_link:
        return jsonify(error="A join link is required."), 400
    try:
        link = normalize_join_link(raw_link)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        details = SOURCES["anilist"].get_details(anilist_id)
    except requests.RequestException:
        return jsonify(error="Couldn't fetch details from AniList right now."), 502
    anime_id = db.upsert_anime(details, added_by=user["id"])
    db.update_link(anime_id, link)
    propagated = propagate_link_full_franchise(anime_id, link)
    db.accept_requests_for_title(details["title"])
    return jsonify(status="updated", anime=db.get_anime(anime_id), propagated=propagated)


def _telegram_user_label(user: dict) -> str:
    if user.get("username"):
        return f"@{user['username']}"
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return name or str(user.get("id"))


# ---------------------------------------------------------------------------
# Telegram webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/<secret>")
def webhook(secret):
    if secret != Config.WEBHOOK_SECRET or bot_app is None:
        abort(403)
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    run_async(bot_app.process_update(update))
    return "ok"


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

def build_bot_app() -> Application | None:
    if not Config.BOT_TOKEN:
        return None
    application = Application.builder().token(Config.BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("anidex", cmd_anidex))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))
    return application


bot_app = build_bot_app()
if bot_app is not None:
    run_async(bot_app.initialize())
    if Config.WEBAPP_URL.startswith("https://"):
        webhook_url = f"{Config.WEBAPP_URL}/webhook/{Config.WEBHOOK_SECRET}"
        run_async(bot_app.bot.set_webhook(url=webhook_url))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=Config.DEBUG)
