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

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = Config.SECRET_KEY
# 1 hour static cache — trims repeat-visit load time on Render/Koyeb without
# risking a stale asset for too long after a redeploy.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

db.init_db()

# ---------------------------------------------------------------------------
# Telegram bot (python-telegram-bot v20+, async) glued into sync Flask via a
# single long-lived event loop.
# ---------------------------------------------------------------------------

bot_app: Application | None = None
_loop = asyncio.new_event_loop()


def run_async(coro):
    return _loop.run_until_complete(coro)


# In-memory session store for short multi-step conversations (search result
# pickers, ad preview confirmation, etc). Telegram's callback_data has a
# 64-byte limit, so we keep the real state here and only pass a short
# session id through callback_data.
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 15 * 60  # 15 minutes

# Per-admin "I'm expecting your next plain-text message to be a join link"
# state — powers both /addpost's follow-up prompt and /editpost.
PENDING_LINK: dict[int, dict] = {}
PENDING_LINK_TTL = 15 * 60

# Per-admin ad-creation wizard state (/ad -> image url -> caption -> link).
AD_SESSIONS: dict[int, dict] = {}

# Per-admin notification-broadcast wizard state (/wbroadcast).
BROADCAST_SESSIONS: dict[int, dict] = {}

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


async def cmd_addpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /addpost <anime name>\nExample: /addpost one piece")
        return

    sid = new_session(kind="addpost", query=query, source="anilist")
    src = SOURCES["anilist"]
    try:
        data = await asyncio.to_thread(src.search, query, 1)
    except Exception:
        await update.message.reply_text("Couldn't reach AniList right now. Try again shortly.")
        return
    sess = SESSIONS[sid]
    sess.update(page=1, results=data["results"], has_next=data["has_next"])
    if not data["results"]:
        await update.message.reply_text(f"No results found on AniList for '{query}'.")
        SESSIONS.pop(sid, None)
        return
    await send_results(update.message, sid)


async def cmd_delpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /delpost <anime name>\nExample: /delpost one piece")
        return

    matches = await asyncio.to_thread(db.search_local, query)
    if not matches:
        await update.message.reply_text(f"No post found matching '{query}'.")
        return
    if len(matches) == 1:
        db.delete_anime(matches[0]["id"])
        await update.message.reply_text(f"\U0001f5d1 Deleted: {matches[0]['title']}")
        return

    sid = new_session(kind="delpost", matches=matches)
    rows = [[InlineKeyboardButton(m["title"], callback_data=f"delpick:{sid}:{i}")]
            for i, m in enumerate(matches[:10])]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"cancel:{sid}")])
    await update.message.reply_text(
        f"Multiple matches for '{query}'. Pick one to delete:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _prompt_for_new_link(message, admin_id: int, anime: dict):
    PENDING_LINK[admin_id] = {
        "anime_id": anime["id"], "title": anime["title"],
        "created": time.time(), "mode": "confirm",
    }
    await message.reply_text(f"Send the new join link for {anime['title']}:")


async def cmd_editpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /editpost <anime name>\nExample: /editpost one piece")
        return

    matches = await asyncio.to_thread(db.search_local, query)
    if not matches:
        await update.message.reply_text(f"No post found matching '{query}'.")
        return
    if len(matches) == 1:
        await _prompt_for_new_link(update.message, update.effective_user.id, matches[0])
        return

    sid = new_session(kind="editpost_pick", matches=matches[:10])
    rows = [[InlineKeyboardButton(m["title"], callback_data=f"editpick:{sid}:{i}")]
            for i, m in enumerate(matches[:10])]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"cancel:{sid}")])
    await update.message.reply_text(
        f"Multiple matches for '{query}'. Pick one to edit:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# --- /ad, /rmad, /adstats -------------------------------------------------

_DURATION_RE = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE)
_UNIT_SECONDS = {
    "day": 86400, "days": 86400, "d": 86400,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
}


def _parse_duration(text: str) -> int | None:
    match = _DURATION_RE.search(text)
    if not match:
        return None
    n = int(match.group(1))
    unit = match.group(2).lower()
    seconds = n * _UNIT_SECONDS.get(unit, 0)
    return seconds or None


def _format_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"{n} day" + ("s" if n != 1 else "")
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} hour" + ("s" if n != 1 else "")
    n = max(1, seconds // 60)
    return f"{n} minute" + ("s" if n != 1 else "")


async def cmd_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    text = " ".join(context.args).strip()
    seconds = _parse_duration(text)
    if not seconds:
        await update.message.reply_text(
            "Usage: /ad <duration>\nExamples: /ad 1 day, /ad 3 hours, /ad 10 m"
        )
        return
    AD_SESSIONS[update.effective_user.id] = {
        "duration_seconds": seconds, "step": "await_image", "created": time.time(),
    }
    await update.message.reply_text(
        "Send the ad's image URL (paste a link), or send 'skip' for no thumbnail."
    )


async def cmd_rmad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    doc = db.clear_ad()
    if not doc:
        await update.message.reply_text("No active ad to remove.")
        return
    await update.message.reply_text(
        f"\U0001f5d1 Ad removed.\nFinal stats — Taps: {doc.get('taps', 0)}, Clicks: {doc.get('clicks', 0)}"
    )


async def cmd_adstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    doc = db.get_ad_stats()
    if not doc:
        await update.message.reply_text("No active ad running.")
        return
    remaining = doc["expires_at"] - time.time()
    time_left = _format_duration(remaining) if remaining > 0 else "expired"
    await update.message.reply_text(
        f"\U0001f4ca Ad stats\nTaps: {doc.get('taps', 0)}\nClicks: {doc.get('clicks', 0)}\n"
        f"Time left: {time_left}"
    )


async def cmd_refreshposts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Best-effort manual re-sync: re-fetches AniList metadata (including
    airing status/episode count) for every posted anime that came from
    AniList. There's no background scheduler in this process, so this is
    admin-triggered rather than fully automatic — see README for notes on
    adding a real scheduler if you want this to run unattended."""
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    posts = await asyncio.to_thread(db.list_available)
    updated = 0
    finished = []
    for post in posts:
        if post.get("source") != "anilist":
            continue
        try:
            details = await asyncio.to_thread(SOURCES["anilist"].get_details, post["source_id"])
        except Exception:
            continue
        db.upsert_anime(details)
        updated += 1
        if details.get("status") == "FINISHED" and post.get("status") != "FINISHED":
            finished.append(details["title"])

    text = f"\U0001f504 Refreshed {updated} post(s)."
    if finished:
        text += "\n\nJust finished airing (all episodes out):\n" + "\n".join(f"\u2022 {t}" for t in finished)
    await update.message.reply_text(text)


async def cmd_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin_user = update.effective_user.id in Config.ADMIN_IDS
    lines = [
        "*Everyone*",
        "/anidex \u2014 open the start menu",
        "Send any text \u2014 search the Available library",
        "",
    ]
    if is_admin_user:
        lines += [
            "*Admin*",
            "/addpost <name> \u2014 add a new post",
            "/editpost <name> \u2014 change a post's join link",
            "/delpost <name> \u2014 remove a post",
            "/refreshposts \u2014 re-sync metadata from AniList",
            "/ad <duration> \u2014 start a promotional ad (e.g. /ad 1 day)",
            "/rmad \u2014 end the active ad early",
            "/adstats \u2014 live ad taps/clicks",
            "/wbroadcast <duration> \u2014 push a timed notification to the mini app",
            "/cmds \u2014 this list",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_wbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("\u26d4 You're not authorized to use this command.")
        return
    text = " ".join(context.args).strip()
    seconds = _parse_duration(text)
    if not seconds:
        await update.message.reply_text(
            "Usage: /wbroadcast <duration>\nExamples: /wbroadcast 1 day, /wbroadcast 1 hours, /wbroadcast 10 m"
        )
        return
    BROADCAST_SESSIONS[update.effective_user.id] = {
        "duration_seconds": seconds, "step": "await_image", "created": time.time(),
    }
    await update.message.reply_text(
        "Send the notification's image URL (paste a link), or send 'skip' for no thumbnail."
    )


async def handle_broadcast_session_text(update: Update, session: dict):
    text = (update.message.text or "").strip()
    step = session["step"]
    admin_id = update.effective_user.id

    if step == "await_image":
        session["image_url"] = None if text.lower() == "skip" else text
        session["step"] = "await_caption"
        await update.message.reply_text("Now send the notification caption.")
        return

    if step == "await_caption":
        if not text:
            await update.message.reply_text("Caption can't be empty — send some text.")
            return
        session["caption"] = text
        session["step"] = "await_link"
        await update.message.reply_text("Send a link for the notification, or send 'skip' for no link.")
        return

    if step == "await_link":
        link = None
        if text.lower() != "skip":
            try:
                link = normalize_join_link(text)
            except ValueError as e:
                await update.message.reply_text(str(e))
                return
        BROADCAST_SESSIONS.pop(admin_id, None)
        sid = new_session(kind="broadcast_confirm", image_url=session.get("image_url"),
                           caption=session["caption"], link=link, duration_seconds=session["duration_seconds"])
        preview = f"*Notification preview*\n\n{session['caption']}"
        if link:
            preview += f"\n\nLink: {link}"
        preview += f"\n\nStays live for: {_format_duration(session['duration_seconds'])}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2705 Send", callback_data=f"bsave:{sid}"),
            InlineKeyboardButton("\u274c Cancel", callback_data=f"cancel:{sid}"),
        ]])
        await update.message.reply_text(preview, reply_markup=keyboard, parse_mode="Markdown")
        return


async def handle_broadcast_save(q, sid):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /wbroadcast again.", show_alert=True)
        return
    SESSIONS.pop(sid, None)
    expires_at = time.time() + session["duration_seconds"]
    db.create_notification(session.get("image_url"), session["caption"], session.get("link"), expires_at)
    await q.answer("Sent")
    await q.edit_message_text(
        f"\u2705 Notification is live in the mini app for {_format_duration(session['duration_seconds'])}."
    )


# --- Callback query routing ------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    # Title text can contain colons (e.g. "Attack on Titan: Final Season"),
    # so these are checked before the generic colon-split below.
    if data.startswith("quickadd:"):
        await handle_quickadd(q, data[len("quickadd:"):])
        return

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

    if action == "page":
        _, sid, page = parts
        await handle_page(q, sid, int(page))
        return

    if action == "pick":
        _, sid, idx = parts
        await handle_pick(q, update, sid, int(idx))
        return

    if action == "delpick":
        _, sid, idx = parts
        await handle_delpick(q, sid, int(idx))
        return

    if action == "searchpick":
        _, sid, idx = parts
        await handle_searchpick(q, sid, int(idx))
        return

    if action == "editpick":
        _, sid, idx = parts
        await handle_editpick(q, sid, int(idx))
        return

    if action == "editdone":
        _, sid = parts
        await handle_editdone(q, sid)
        return

    if action == "adsave":
        _, sid = parts
        await handle_adsave(q, sid)
        return

    if action == "bsave":
        _, sid = parts
        await handle_broadcast_save(q, sid)
        return

    await q.answer()


async def handle_page(q, sid, page):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /addpost again.", show_alert=True)
        return
    await q.answer()
    src = SOURCES[session["source"]]
    data = await asyncio.to_thread(src.search, session["query"], page)
    session.update(page=page, results=data["results"], has_next=data["has_next"])
    await render_results(q, sid)


def _results_text(session):
    return "Search Results (ANILIST)\nSelect the correct title from the list below:"


def _results_keyboard(sid, session):
    rows = [
        [InlineKeyboardButton(
            f"{r['title']}" + (f" ({r['year']})" if r.get("year") else ""),
            callback_data=f"pick:{sid}:{i}",
        )]
        for i, r in enumerate(session["results"])
    ]
    nav = []
    if session["page"] > 1:
        nav.append(InlineKeyboardButton("\u2b05 Prev", callback_data=f"page:{sid}:{session['page'] - 1}"))
    nav.append(InlineKeyboardButton(str(session["page"]), callback_data="noop"))
    if session.get("has_next"):
        nav.append(InlineKeyboardButton("Next \u27a1", callback_data=f"page:{sid}:{session['page'] + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"cancel:{sid}")])
    return InlineKeyboardMarkup(rows)


async def send_results(message, sid):
    session = SESSIONS[sid]
    await message.reply_text(_results_text(session), reply_markup=_results_keyboard(sid, session))


async def render_results(q, sid):
    session = SESSIONS[sid]
    if not session["results"]:
        await q.edit_message_text(f"No results found on AniList for '{session['query']}'.")
        SESSIONS.pop(sid, None)
        return
    await q.edit_message_text(_results_text(session), reply_markup=_results_keyboard(sid, session))


async def handle_pick(q, update, sid, idx):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /addpost again.", show_alert=True)
        return
    await q.answer("Fetching details...")
    r = session["results"][idx]
    src = SOURCES[session["source"]]
    try:
        details = await asyncio.to_thread(src.get_details, r["source_id"])
    except Exception:
        await q.edit_message_text("Couldn't fetch full details for that title. Try again.")
        return

    anime_id = db.upsert_anime(details, added_by=update.effective_user.id)
    SESSIONS.pop(sid, None)

    await q.edit_message_text(
        f"\u2705 Post created: {details['title']}\n\n"
        f"It's live under Available on {Config.BRAND_NAME} now.",
        reply_markup=InlineKeyboardMarkup([[_webapp_button()]]),
    )
    PENDING_LINK[update.effective_user.id] = {
        "anime_id": anime_id, "title": details["title"], "created": time.time(), "mode": "auto",
    }
    await q.message.reply_text(
        f"\U0001f4ce Now set a join link for {details['title']} — just send it as your next "
        f"message (a Telegram @username, a t.me/ link, an invite link, or a channel ID)."
    )


async def handle_delpick(q, sid, idx):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /delpost again.", show_alert=True)
        return
    match = session["matches"][idx]
    db.delete_anime(match["id"])
    SESSIONS.pop(sid, None)
    await q.answer()
    await q.edit_message_text(f"\U0001f5d1 Deleted: {match['title']}")


async def handle_editpick(q, sid, idx):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /editpost again.", show_alert=True)
        return
    match = session["matches"][idx]
    SESSIONS.pop(sid, None)
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    await _prompt_for_new_link(q.message, q.from_user.id, match)


async def handle_editdone(q, sid):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /editpost again.", show_alert=True)
        return
    SESSIONS.pop(sid, None)
    db.update_link(session["anime_id"], session["link"])
    propagated = db.propagate_join_link(session["anime_id"], session["link"])
    await q.answer("Saved")
    keyboard = InlineKeyboardMarkup([[_preview_button(session["anime_id"])]])
    text = f"\u2705 Join link updated for {session['title']}."
    if propagated:
        text += f"\nAlso applied to {propagated} related season(s)."
    await q.edit_message_text(text, reply_markup=keyboard)


async def handle_adsave(q, sid):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — run /ad again.", show_alert=True)
        return
    SESSIONS.pop(sid, None)
    expires_at = time.time() + session["duration_seconds"]
    db.set_ad(session.get("image_url"), session["caption"], session.get("link"), expires_at)
    await q.answer("Ad is live")
    await q.edit_message_text(
        f"\u2705 Ad is now live for {_format_duration(session['duration_seconds'])}.\n"
        f"Use /adstats to check taps/clicks, or /rmad to end it early."
    )


# --- Auto-search: plain text messages (no command) search the library ----

def _display_name_from_user(tg_user) -> str:
    if tg_user.username:
        return f"@{tg_user.username}"
    return tg_user.full_name or str(tg_user.id)


async def send_anime_result(message, anime: dict):
    """Bot search results only ever show the name — no genres, no
    description — and the action button deep-links into the mini app at
    that exact post instead of opening the raw channel link directly."""
    await message.reply_text(anime["title"], reply_markup=InlineKeyboardMarkup([[_open_post_button(anime)]]))


async def handle_searchpick(q, sid, idx):
    session = SESSIONS.get(sid)
    if not session:
        await q.answer("Session expired — search again.", show_alert=True)
        return
    match = session["matches"][idx]
    SESSIONS.pop(sid, None)
    await q.answer()
    await q.edit_message_text(match["title"], reply_markup=InlineKeyboardMarkup([[_open_post_button(match)]]))


async def handle_quickadd(q, title: str):
    if q.from_user.id not in Config.ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer("Searching AniList...")
    try:
        data = await asyncio.to_thread(SOURCES["anilist"].search, title, 1)
    except Exception:
        await q.message.reply_text("Couldn't reach AniList right now. Try /addpost manually.")
        return
    if not data["results"]:
        await q.message.reply_text(f"No AniList results for '{title}'. Try /addpost manually.")
        return
    sid = new_session(kind="addpost", query=title, source="anilist", page=1,
                       results=data["results"], has_next=data["has_next"])
    await send_results(q.message, sid)


async def handle_pending_link_text(update: Update, pending: dict):
    text = (update.message.text or "").strip()
    try:
        link = normalize_join_link(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return  # keep the pending state so they can just try again

    admin_id = update.effective_user.id

    if pending["mode"] == "auto":
        db.update_link(pending["anime_id"], link)
        propagated = db.propagate_join_link(pending["anime_id"], link)
        PENDING_LINK.pop(admin_id, None)
        keyboard = InlineKeyboardMarkup([[_preview_button(pending["anime_id"])]])
        text = f"\u2705 Join link saved for {pending['title']}."
        if propagated:
            text += f"\nAlso applied to {propagated} related season(s)."
        await update.message.reply_text(text, reply_markup=keyboard)
        return

    # mode == "confirm" (/editpost) — preview + Done/Cancel before saving
    PENDING_LINK.pop(admin_id, None)
    sid = new_session(kind="editconfirm", anime_id=pending["anime_id"], title=pending["title"], link=link)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Done", callback_data=f"editdone:{sid}"),
        InlineKeyboardButton("\u274c Cancel", callback_data=f"cancel:{sid}"),
    ]])
    await update.message.reply_text(
        f"Set join link for *{pending['title']}* to:\n{link}\n\nSave this?",
        reply_markup=keyboard, parse_mode="Markdown",
    )


async def handle_ad_session_text(update: Update, session: dict):
    text = (update.message.text or "").strip()
    step = session["step"]
    admin_id = update.effective_user.id

    if step == "await_image":
        session["image_url"] = None if text.lower() == "skip" else text
        session["step"] = "await_caption"
        await update.message.reply_text("Now send the ad caption.")
        return

    if step == "await_caption":
        if not text:
            await update.message.reply_text("Caption can't be empty — send some text.")
            return
        session["caption"] = text
        session["step"] = "await_link"
        await update.message.reply_text("Send a link for the 'Click Here' button, or send 'skip' for no button.")
        return

    if step == "await_link":
        link = None
        if text.lower() != "skip":
            try:
                link = normalize_join_link(text)
            except ValueError as e:
                await update.message.reply_text(str(e))
                return
        AD_SESSIONS.pop(admin_id, None)
        sid = new_session(
            kind="ad_confirm", image_url=session.get("image_url"), caption=session["caption"],
            link=link, duration_seconds=session["duration_seconds"],
        )
        preview = f"*Ad preview*\n\n{session['caption']}"
        if link:
            preview += f"\n\nButton: Click Here \u2192 {link}"
        preview += f"\n\nDuration: {_format_duration(session['duration_seconds'])}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2705 Done", callback_data=f"adsave:{sid}"),
            InlineKeyboardButton("\u274c Cancel", callback_data=f"cancel:{sid}"),
        ]])
        await update.message.reply_text(preview, reply_markup=keyboard, parse_mode="Markdown")
        return


async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any plain-text message (not a command) is treated as an anime title
    search against the local library — unless the sender is an admin in
    the middle of an /ad wizard or a pending join-link prompt, in which
    case that takes priority."""
    text = (update.message.text or "").strip()
    admin_id = update.effective_user.id

    ad_session = AD_SESSIONS.get(admin_id)
    if ad_session and admin_id in Config.ADMIN_IDS:
        await handle_ad_session_text(update, ad_session)
        return

    broadcast_session = BROADCAST_SESSIONS.get(admin_id)
    if broadcast_session and admin_id in Config.ADMIN_IDS:
        await handle_broadcast_session_text(update, broadcast_session)
        return

    pending = PENDING_LINK.get(admin_id)
    if pending:
        if time.time() - pending["created"] < PENDING_LINK_TTL:
            await handle_pending_link_text(update, pending)
            return
        PENDING_LINK.pop(admin_id, None)

    if len(text) < 2:
        return

    local_matches = await asyncio.to_thread(db.search_local, text)
    if not local_matches:
        keyboard = InlineKeyboardMarkup([[_search_in_app_button(text)]])
        await update.message.reply_text(
            f"'{text}' isn't posted yet. Open {Config.BRAND_NAME} to search and vote for it.",
            reply_markup=keyboard,
        )
        return

    if len(local_matches) == 1:
        await send_anime_result(update.message, local_matches[0])
        return

    sid = new_session(kind="searchpick", matches=local_matches[:8])
    rows = [[InlineKeyboardButton(m["title"], callback_data=f"searchpick:{sid}:{i}")]
            for i, m in enumerate(local_matches[:8])]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"cancel:{sid}")])
    await update.message.reply_text(
        f"Found {len(local_matches)} matches for '{text}':",
        reply_markup=InlineKeyboardMarkup(rows),
    )


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


def notify_vote_milestone(title: str, count: int):
    if not Config.LOG_CHANNEL_ID or not bot_app:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2795 Add This Anime", callback_data=f"quickadd:{title[:200]}")
    ]])
    text = f"\U0001f525 {count} people are demanding \"{title}\" — consider adding it!"
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


@app.get("/api/notifications")
def api_notifications():
    return jsonify(db.list_notifications())


@app.get("/api/catalog/available")
def api_available():
    return jsonify(db.list_available())


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
    db.update_link(anime_id, link)
    propagated = db.propagate_join_link(anime_id, link) if link else 0
    return jsonify(status="updated", link=link, propagated=propagated)


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
    propagated = db.propagate_join_link(anime_id, link) if link else 0
    return jsonify(status="updated", anime=db.get_anime(anime_id), propagated=propagated)


@app.get("/api/ads/active")
def api_ads_active():
    ad = db.get_active_ad()
    if not ad:
        return jsonify(None)
    return jsonify({
        "image_url": ad.get("image_url"),
        "caption": ad.get("caption"),
        "link": ad.get("link"),
    })


@app.post("/api/ads/tap")
def api_ads_tap():
    db.record_ad_tap()
    return jsonify(status="ok")


@app.post("/api/ads/click")
def api_ads_click():
    db.record_ad_click()
    return jsonify(status="ok")


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
    application.add_handler(CommandHandler("addpost", cmd_addpost))
    application.add_handler(CommandHandler("delpost", cmd_delpost))
    application.add_handler(CommandHandler("editpost", cmd_editpost))
    application.add_handler(CommandHandler("ad", cmd_ad))
    application.add_handler(CommandHandler("rmad", cmd_rmad))
    application.add_handler(CommandHandler("adstats", cmd_adstats))
    application.add_handler(CommandHandler("refreshposts", cmd_refreshposts))
    application.add_handler(CommandHandler("cmds", cmd_cmds))
    application.add_handler(CommandHandler("wbroadcast", cmd_wbroadcast))
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
