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
(SESSIONS for multi-step flows, PENDING_LINK, AD_SESSIONS) and a single
asyncio event loop. Run it with a single worker (see Dockerfile /
render.yaml) — multiple worker processes would each have their own copy
of this state and event loop, breaking every multi-step flow.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, quote

import requests
from flask import Flask, abort, jsonify, render_template, request

from config import Config
from database import database as db
from plugins import SOURCES

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)


app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = Config.SECRET_KEY


app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

db.init_db()


bot_app: Application | None = None
_loop = asyncio.new_event_loop()


def run_async(coro):
    return _loop.run_until_complete(coro)


SESSIONS: dict[str, dict] = {}
SESSION_TTL = 15 * 60  


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
    
    
    return InlineKeyboardButton(label, url=Config.WEBAPP_URL or "https://telegram.org")


def _open_post_button(anime: dict) -> InlineKeyboardButton:
    """Deep-links straight into the mini app at this specific post, instead
    of opening the raw channel link directly."""
    if Config.WEBAPP_URL.startswith("https://"):
        url = f"{Config.WEBAPP_URL}?anime={anime['id']}"
        return InlineKeyboardButton("\u25b6 Open Post", web_app=WebAppInfo(url=url))
    return InlineKeyboardButton("\u25b6 Open Post", url=Config.WEBAPP_URL or "https://telegram.org")


def _preview_button(anime_id: int) -> InlineKeyboardButton:
    if Config.WEBAPP_URL.startswith("https://"):
        url = f"{Config.WEBAPP_URL}?anime={anime_id}"
        return InlineKeyboardButton("\U0001f50d Preview Post", web_app=WebAppInfo(url=url))
    return InlineKeyboardButton("\U0001f50d Preview Post", url=Config.WEBAPP_URL or "https://telegram.org")


def _search_in_app_button(text: str) -> InlineKeyboardButton:
    query_param = quote(text)
    label = f"\U0001f4d6 Open {Config.BRAND_NAME}"
    if Config.WEBAPP_URL.startswith("https://"):
        url = f"{Config.WEBAPP_URL}?search={query_param}"
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    return InlineKeyboardButton(label, url=Config.WEBAPP_URL or "https://telegram.org")


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
    text = f"\U0001f525 {count} people are demanding \"{title}\" — consider adding it!"
    run_async(bot_app.bot.send_message(Config.LOG_CHANNEL_ID, text, reply_markup=keyboard))


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
        return ""  

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


@app.get("/")
def index():
    return render_template("index.html", brand_name=Config.BRAND_NAME, brand_handle=Config.BRAND_HANDLE)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/api/catalog/trending")
def api_trending():
    page = request.args.get("page", 1, type=int)
    try:
        return jsonify(SOURCES["anilist"].get_trending(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})


@app.get("/api/catalog/popular")
def api_popular():
    page = request.args.get("page", 1, type=int)
    try:
        return jsonify(SOURCES["anilist"].get_popular(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})


@app.get("/api/catalog/most-popular")
def api_most_popular():
    page = request.args.get("page", 1, type=int)
    try:
        return jsonify(SOURCES["anilist"].get_most_popular(page))
    except requests.RequestException:
        return jsonify({"results": [], "has_next": False})


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
    
    
    
    
    
    
    
    return jsonify([a for a in db.list_available() if a.get("available")])


@app.get("/api/anime/<int:anime_id>")
def api_anime_detail(anime_id):
    anime = db.get_anime(anime_id)
    if not anime:
        abort(404)
    return jsonify(anime)


@app.get("/api/anilist/<int:anilist_id>")
def api_anilist_details(anilist_id):
    """Full details (genres/synopsis/banner) for a Trending/Popular card —
    the lightweight discovery query doesn't include those fields."""
    try:
        return jsonify(SOURCES["anilist"].get_details(anilist_id))
    except requests.RequestException:
        abort(502)


@app.post("/api/vote")
def api_vote():
    payload = request.get_json(force=True, silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify(error="title is required"), 400

    user = current_user()
    if not user:
        return jsonify(error="Open this inside Telegram to vote."), 401

    result = db.record_vote(title, user["id"])
    if not result["already_voted"] and result["count"] % 20 == 0:
        notify_vote_milestone(title, result["count"])
    return jsonify(result)


@app.post("/api/report")
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




def _telegram_user_label(user: dict) -> str:
    if user.get("username"):
        return f"@{user['username']}"
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return name or str(user.get("id"))


@app.post("/webhook/<secret>")
def webhook(secret):
    if secret != Config.WEBHOOK_SECRET or bot_app is None:
        abort(403)
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    run_async(bot_app.process_update(update))
    return "ok"


def build_bot_app() -> Application | None:
    if not Config.BOT_TOKEN:
        return None
    application = Application.builder().token(Config.BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("anidex", cmd_anidex))
    return application


bot_app = build_bot_app()
if bot_app is not None:
    run_async(bot_app.initialize())
    if Config.WEBAPP_URL.startswith("https://"):
        webhook_url = f"{Config.WEBAPP_URL}/webhook/{Config.WEBHOOK_SECRET}"
        run_async(bot_app.bot.set_webhook(url=webhook_url))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=Config.DEBUG)
