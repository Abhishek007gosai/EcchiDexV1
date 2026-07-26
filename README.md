# Anime Index

A Telegram bot + Mini App for browsing, requesting, and moderating an anime
catalog. Flask serves both the JSON API and the mini app's HTML/CSS/JS; the
bot runs in the same process via a Telegram webhook.

## What's included

- **`/anidex`** — welcome message with an Open Mini App button (`/start` stays silent).
  The message text is fully editable via `START_MSG` (see `.env.example`).
- **Auto-search** — send the bot any plain text (not a command) and it
  searches your **Available** library only, replying with just the
  title and a button that deep-links straight into the mini app at that
  exact post. If nothing matches, it offers to open the mini app's Search
  page, pre-searched.
- **Mini app** — bottom nav is **Home / Search / Notifications / Profile**.
  Home has an **All / Available** pill switch: All shows a Featured promo
  carousel (from Anime News Network), Trending Now, a numbered Top Airing
  list (with Load More), and a Genres row; Available is your posted
  catalog, browsable A–Z, with the ad slot when one is running. Search is
  a dedicated page with Popular Searches (tracked in MongoDB) and genre
  tiles that browse AniList by genre.
- **Voting** — any Trending/Top Airing/genre item that isn't posted yet
  shows a Vote button instead of Join. Every 20 votes, your log channel
  gets a "people are demanding this" notification with a one-tap "Add
  This Anime" button. If an item's title matches an Available post that
  already has a join link, it shows Join instead of Vote automatically.
- **Report** — Available posts only (not discovery items, ads, or
  notifications). Preset reasons + optional 50-character note, sent to
  your log channel.
- **Profile** — Telegram ID, registration status, role, access, verified
  via Telegram's WebApp `initData` signature.
- **Admin ➕ link editor** — a ➕ button next to Join/Coming Soon (admin/owner
  only) opens a "Set Join Link" sheet accepting a channel ID, @username, or
  URL. Channel IDs are turned into a real Telegram invite link via the Bot
  API automatically (the bot must be an admin in that channel);
  @usernames and t.me links are normalized the same way the "Set Join
  Link" field always claimed to support.
- **Season auto-linking** — setting a join link (via `/addpost`'s
  follow-up, `/editpost`, or the mini app's ➕ editor) automatically
  applies the same link to any other posted season AniList lists as a
  direct prequel/sequel of that title. Adding a new season later also
  auto-inherits the link if a directly related season is already linked
  — only direct relations are followed, not distant ones several seasons
  away in the same franchise.
- Post details open as a small, fixed-size centered card — not a
  full-screen page — with the action buttons always in the same spot
  regardless of title/genre/description length.

## 1. Create the bot

1. Message **[@BotFather](https://t.me/BotFather)** → `/newbot` → follow
   the prompts → copy the token it gives you (`BOT_TOKEN`).
2. Get your own Telegram numeric ID from **[@userinfobot](https://t.me/userinfobot)**
   — this goes in `ADMIN_IDS`.
3. Create a private channel for logs (requests/reports), add the bot as an
   admin, and grab the channel ID (starts with `-100...`) — you can get it
   by forwarding a message from the channel to **[@userinfobot](https://t.me/userinfobot)**.
   This is `LOG_CHANNEL_ID`.

## 2. Set up MongoDB

Data (catalog, users, requests, reports) is stored in MongoDB — no local
file, so it survives redeploys on Render/Koyeb without any extra disk
config. Easiest option: create a free cluster at
[MongoDB Atlas](https://www.mongodb.com/cloud/atlas), then grab its
connection string for `MONGODB_URL` (looks like
`mongodb+srv://user:pass@cluster.mongodb.net`). `MONGODB_NAME` is just the
database name inside that cluster — `anime_index` by default, change it if
you like.

For local development, `docker compose up` starts a MongoDB container for
you automatically (see `docker-compose.yml`) — no Atlas account needed
until you deploy.

## 3. Configure environment variables

Copy `.env.example` to `.env` and fill it in. Locally, `docker-compose`
reads `.env` automatically. On Render/Koyeb, set the same variables in
their dashboards instead of committing a `.env` file.

`WEBAPP_URL` must be the final HTTPS URL of your deployment — the bot uses
it both for the mini app's "Open" button and to register the Telegram
webhook on startup, so redeploy once after you know the URL if you didn't
have it yet.

## 4. Run locally

```bash
pip install -r requirements.txt
python app.py
```

Or with Docker (also starts a local MongoDB container):

```bash
docker compose up --build
```

Without `WEBAPP_URL` set to a real HTTPS address, the bot won't receive
updates (Telegram webhooks require HTTPS) — for local bot testing, use a
tunnel like `ngrok http 8000` and set `WEBAPP_URL` to the tunnel URL.

## 5. Deploy on Render

1. Push this repo to GitHub.
2. Render Dashboard → **New → Blueprint** → connect the repo. It reads
   `render.yaml` and creates the service automatically.
3. Fill in `BOT_TOKEN`, `LOG_CHANNEL_ID`, `ADMIN_IDS`, `MONGODB_URL` in the
   dashboard (marked `sync: false` in the blueprint, so Render prompts for
   them).
4. Once deployed, update `WEBAPP_URL` to the real `.onrender.com` address
   and redeploy so the webhook registers correctly.

## 6. Deploy on Koyeb

Koyeb doesn't auto-read a repo config file the way Render does — use the
included `Dockerfile`:

1. Push this repo to GitHub.
2. Koyeb Control Panel → **Create Web Service → GitHub** → select the repo.
3. Builder: **Dockerfile**. Port: **8000**.
4. Add the same environment variables as above (including `MONGODB_URL`).
5. Deploy, then set `WEBAPP_URL` to the `.koyeb.app` URL and redeploy.

See `koyeb.yaml` for the equivalent CLI command.

## Notes

- `plugins/` holds the metadata source (`anilist.py`) behind a shared
  interface (`base.py`) — add another source by implementing the same
  `search` / `get_details` methods and registering it in
  `plugins/__init__.py`. MyAnimeList (via the Jikan API) was tried and
  removed — too unreliable in practice.
- The bot keeps `/addpost` and `/delpost` conversation state in memory —
  run the web process with a **single worker** (already set in `Dockerfile`
  and `render.yaml`); multiple workers would each have their own copy and
  break the multi-step flows.
- Join links are stored as plain URLs you provide via `/addpost` follow-up
  editing or the in-app editor — this project doesn't source, scrape, or
  curate content itself.
