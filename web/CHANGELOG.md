# Changelog

## Franchise navigation (Prequel / Sequel)

A title's detail sheet now shows **at most two** related-title cards —
Prequel and Sequel — that step through the whole franchise (every posted
season, OVA, movie, and spin-off) in release order, instead of one card
per AniList relation edge (which could produce a dozen "Side Story" cards
for a single franchise).

- `plugins/anilist.py` — now fetches `month`/`day` alongside `year`, so
  same-year releases (an OVA and a movie in the same year) still sort
  correctly.
- `database/database.py` — `get_franchise_neighbors()` walks the full
  franchise relation graph, lines up every *posted* entry by release
  date, and returns just the immediately-previous and immediately-next
  entry.
- `app.py` — `_related_posted()` now calls that instead of listing every
  relation. No frontend changes were needed; the card UI already matched
  this shape.

## Requests (replaces Votes)

The old vote-counter is gone. Requesting an anime that isn't posted yet
now creates a real request an admin can act on.

- `database/database.py` — `requests_col` replaces `votes_col`.
  `create_request`, `list_pending_requests` (grouped by title),
  `respond_to_request` / `resolve_request_by_id`, `accept_requests_for_title`,
  `get_user_notifications`, `mark_notifications_seen`.
- `app.py` — `POST /api/request` replaces `/api/vote`. Setting a join
  link on a title (via the existing admin link-editor) auto-accepts any
  pending request for that exact title.
- `static/app.js` / `style.css` — the button reads **"Request"** (then
  "✓ Requested"), replacing "Vote" everywhere it appeared.

## Notification bell

A bell icon next to the profile icon in the header, empty until one of
the user's own requests is resolved.

- `GET /api/notifications`, `POST /api/notifications/seen`.
- Card design (in `static/app.js` / `style.css`): status header with
  icon + relative time, poster + title + genres + message, and a
  footer with "Requested by …" / a reference id (`#AR-YYYYMMDD-NNN`)
  plus a contextual action — **Thank you!** (accepted, cosmetic) or
  **Need Help?** (rejected — opens the existing report sheet, pre-filled
  with that title).

## Log-channel Accept / Reject

New requests post to `LOG_CHANNEL_ID` with the poster photo, title,
requester, and buttons.

- **✅ Accept** resolves immediately.
- **❌ Reject** swaps to a quick-reason submenu (not in our source /
  licensing, already posted, no good release yet, other) instead of
  resolving with one generic line — Telegram channels don't reliably
  thread free-text replies back to a bot, so reasons are chosen from
  buttons rather than typed.
- Both are admin-only (`Config.ADMIN_IDS`), edit the log message in
  place to show the outcome, and are safe against double-taps.
- `app.py` — `on_callback` routes `reqaccept` / `reqreject` / `reqreason`
  / `reqback`; `notify_new_request`, `handle_request_accept`,
  `show_reject_reasons`, `show_accept_reject`, `handle_request_reject`.

## Bot text search

- Only responds in **private chats** with the bot (group support was
  added, then removed per request — see `on_text_search`).
- Every reply (no-match message, multi-match picker, single result)
  auto-deletes itself **2 minutes** after being sent, via
  `_delete_message_later()`. This uses a plain `threading.Timer` + a raw
  HTTP call to Telegram's `deleteMessage`, not an asyncio task — this
  process drives its event loop synchronously per webhook request (see
  the module docstring), so an `asyncio.sleep`-based timer would only
  progress whenever some unrelated update happened to arrive.

## Docs

`README.md` updated to describe Requests instead of the old Voting
system, note the franchise-timeline nav fix, and document the
auto-delete/private-chat-only behavior of bot search.
