# PlexBot

PlexBot is a Telegram bot that pulls media from Telegram messages using [TDL](https://github.com/iyear/tdl-telegram), matches titles with TMDb, and stores everything in a Plex-friendly library layout.

## What it does
- Accepts Telegram links or forwarded media (documents/videos/photos/audio).
- Searches TMDb to classify movies/series/anime/docuseries/documentaries and builds destination folders automatically once you pick a title.
- If you paste a link or file without a destination, the bot first asks TMDb (or manual) and a category (Movies/Series/etc.), then downloads directly to the chosen library.
- Concurrent downloads per chat (default max 3).
- Library scan helpers and simple DB search/stats commands.

## Commands (English)
- `/start` — show help.
- `/search` (alias `/buscar`) — search TMDb and set the destination library.
- `/menu` — quick actions (search, DB search/stats, scan libraries).
- `/dbsearch <text>` — search stored shows; `/dbstats` — quick DB metrics.
- `/scan` — rescan configured libraries and sync DB.
- `/clean_tmp` — remove leftover auto-download temp folders (not commonly needed now).
- `/cancel` — cancel the current flow and stop running downloads for this chat.
- `/cancel_all` — cancel flow and stop running + queued downloads for this chat.
- `/season <n>` (alias `/temporada`) — switch the active series/docuseries season without reselecting the show.

## Directory structure (cleaned)
- `app/` — application code.
  - `telegram/` — bot entrypoint and handlers.
  - `services/` — TMDb client, naming helpers, ingest/post-processing, download manager.
  - `infra/` — env loader, DB session helper.
- `config/` — settings loader (`libraries.yaml`), env helpers.
- `fs/` — filesystem scanner utilities.
- `store/` — database models/repos.
- `cli/` — maintenance scripts (DB and library seeding/scans).
- Removed legacy/unused: `mediamarauder.py`, `bot/add_flow.py`, `test_regex.py`, legacy README in Chinese.

## Setup
1. Install Python deps: `pip install -r requirements.txt`.
2. Install TDL and log in: `go install github.com/iyear/tdl-telegram/cmd/tdl@latest` (or download a release) and run `tdl login`.
3. Set environment:
   - `TELEGRAM_BOT_TOKEN`
   - `TMDB_API_KEY`
   - Optional: `PLEX_DB_URL` (defaults to sqlite:///plexbot.db)
   - Optional: `TDL_HOME` to isolate the TDL session (defaults to `~/.tdl-plexbot` in the bot)
4. Configure `config/libraries.yaml` with your libraries (movie/series/anime/docuseries/documentary roots).
5. Run: `python -m app.telegram.main`.

## Notes
- Temp auto-download folders are only used if you re-enable that mode; otherwise downloads go straight to the chosen library.
- If TDL cannot export metadata for a link (no access), the bot will still try TMDb using the filename or link text before download.
- Adjust per-chat concurrency in `DownloadManager(max_concurrent=3)` inside `bot/handlers/download.py`.
- You can set a dedicated TDL session dir via `TDL_HOME` or `download.tdl_home` in `config/libraries.yaml` to avoid DB locks with other clients.
- The TDL template is escaped to pass `{{ .FileName }}` so downloaded files keep their original filename (incl. extensión) before post-processing.
