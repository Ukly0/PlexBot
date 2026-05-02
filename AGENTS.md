# PlexBot

Async Telegram bot for managing a Plex media library. The bot lives in a Telegram group where multiple users can contribute links and files. It downloads content via `tdl`, auto-detects metadata from filenames and message context, matches against TMDb, renames for Plex compatibility, and places files into user-defined library folders.

## Tech Stack

- Python 3.10+, `python-telegram-bot` async API
- TMDb API v3 (Bearer token auth)
- `tdl` CLI for Telegram downloads
- Docker / Docker Compose
- No database — in-memory session cache only

## Commands

```bash
python -m compileall app config    # syntax check (no linter/typechecker configured)
python -m app.bot                  # run the bot
```

## Directory Map

```
app/
├── bot.py               # Entry point — handler registration, bot setup
├── config.py            # Libraries YAML + .env loader → Settings dataclass
├── state.py             # Conversation state constants + reset_flow_state()
├── handlers/
│   ├── menu.py          # /start, /menu, main navigation dashboard
│   ├── ingest.py        # Link/file intake — auto metadata extraction, flow orchestration
│   ├── search.py        # TMDb search, result selection, pagination
│   └── download.py      # Download queue, tdl subprocess, post-process trigger
├── services/
│   ├── tmdb.py          # TMDb API client — multi-search, seasons
│   ├── downloader.py    # tdl subprocess wrapper — progress, retries, locking
│   ├── extractor.py     # Multipart RAR/ZIP detection and extraction
│   └── namer.py         # Plex-safe naming, SxxExx parsing, movie naming
config/
├── libraries.yaml       # Library definitions (user-editable)
├── .env.example         # Secrets template
└── settings.py          # (legacy — migrate to app/config.py)
docker-compose.yml
Dockerfile
requirements.txt
```

**Removed / deprecated** — do not restore or add dependencies on:
- `store/` — SQLAlchemy models, repos, session factory
- `fs/` — filesystem scanner
- `cli/` — management scripts
- `app/infra/db.py` — DB session
- `app/telegram/handlers/db.py` — local DB search handlers
- `nest_asyncio` — unused, remove from `requirements.txt`

## Library Configuration

Libraries are defined in `config/libraries.yaml`. There are exactly two behavioral types:

```yaml
libraries:
  - name: "TV Shows"
    root: /media/tv
    type: series          # episodic → SxxExx naming, asks for season number
  - name: "Movies"
    root: /media/movies
    type: movie           # standalone → "Title (Year).ext" naming
  - name: "Anime"
    root: /media/anime
    type: series          # same episodic behavior as "series"
```

- `type: series` — content with seasons and episodes. Downloads go into `{root}/{ShowName}/Season {N}/`. Files are renamed to `S{N:02d}E{N:02d} - {Title}.{ext}`.
- `type: movie` — standalone files. Downloads go into `{root}/{ShowName} ({Year})/`. Files are renamed to `{Title} ({Year}).{ext}`.
- The `name` is a human-readable label shown in library selection buttons.
- Adding a new library is a YAML edit + bot restart. No code changes needed.

## Plex-Compatible Naming (Critical)

All file and folder names must be safe for Plex. The `app/services/namer.py` module is the single source of truth for naming. Every path written to disk MUST go through its helpers.

**Rules:**
- **ASCII only**: strip diacritics, accents, ñ, ç, ü, etc. Normalize to closest ASCII equivalent (`ñ` → `n`, `é` → `e`).
- **No special chars**: remove `< > : " / \ | ? *`. Collapse multiple spaces into one.
- **Series format**: `S01E02 - Episode Title.mkv` inside `ShowName/Season 01/`
- **Movie format**: `Movie Title (2024).mkv` inside `Movie Title (2024)/`
- **Fallback**: if title is empty after sanitization, use `"Content"`.
- **Collisions**: append `-dup1`, `-dup2`, etc. to the stem, never overwrite.

When parsing episode numbers from filenames, support these patterns (all case-insensitive):
- `S01E02`, `s01e02`, `1x02`, `1X02` → season=1, episode=2
- `E05`, `e05` → episode=5 (requires season hint)
- `101` (three digits) → season=1, episode=1 (excludes resolution-like numbers: 720, 108, etc.)

### Filename Parsing (Auto-Detection)

`app/handlers/ingest.py::_parse_filename()` tokenizes the original filename and classifies each token:
- **SxxExx tokens** (`S01E02`, `1x03`, `S02`) → extracted as `season`/`episode` metadata, removed from title
- **Year tokens** (`(2024)`, `2019`) → extracted as `year` metadata, removed from title
- **Resolution tokens** (`1080p`, `720p`, `2160p`, `4k`) → removed from title
- **Noise tokens** (codec, source, group: `x264`, `WEB-DL`, `AMZN`, etc.) → removed from title
- Remaining tokens → joined as the clean title sent to TMDb

Extracted `season` and `year` are stored as `pending_season`/`pending_year` and `season_hint` in context, so the bot can pre-fill the season picker when the TMDb result is selected.

## User Flow

1. **User sends or forwards a link/file** to the bot (in a group or DM). The bot is added to public groups so `tdl` can resolve download links from forwarded messages.
2. **Auto-detection**: the bot extracts a candidate title from the filename, forwarded message text, or caption. It searches TMDb automatically and shows the top 3 matches.
3. **Confirm or search manually**: user picks a result with an inline button, or triggers a manual search if auto-detection was wrong.
4. **Series only — season**: if the content type is episodic, the bot asks for the season number. It attempts to detect the season from the filename; if it cannot, it shows season buttons.
5. **Library destination**: user picks which library folder to download into.
6. **Enqueue → download → post-process**: the bot queues the download. On completion it extracts archives, renames files for Plex, sets permissions (`chown 1000:1000`), and records the destination in the in-memory session cache.

### Session Memory (No Persistence)

While the bot is running, it keeps an in-memory dict in `bot_data` mapping `chat_id` → list of recent destinations (title, library, season). When new content arrives, if the title matches a recent entry, the bot offers a "Continue [Series X]?" shortcut button. No SQLite, no ORM, no files on disk. Everything resets on restart.

## Multi-User / Group Behavior

- The bot expects to be added to one or more Telegram **groups**.
- Groups must be **public** (or have a public invite link) for `tdl` to resolve forwarded message download links.
- Any group member can send links/files — the bot processes them regardless of sender.
- State is scoped per-chat via `context.chat_data`. Conversation state (search results, pending selections) is per-user via `context.user_data`.
- Admin-only features (`/scan`, `/clean_tmp`) respect `ADMIN_CHAT_ID` from `.env`.

## State Keys

### `context.user_data` (per-user conversation state)

| Key | Type | Purpose |
|-----|------|---------|
| `state` | `str` | Current step: `"awaiting_search"`, `"awaiting_season"`, `"awaiting_library"` |
| `pending_title` | `str` | Extracted or manually entered title |
| `pending_year` | `int` or `None` | Detected year |
| `pending_season` | `int` or `None` | Detected season number |
| `tmdb_results` | `list[dict]` | Cached TMDb search results |
| `tmdb_page` | `int` | Current pagination page |
| `selected_tmdb` | `dict` | Confirmed TMDb item `{id, title, year, kind}` |

### `context.chat_data` (per-chat scoped data)

| Key | Type | Purpose |
|-----|------|---------|
| `download_dir` | `str` | Chosen destination path |
| `season_hint` | `int` or `None` | Season number for current download |
| `active_library` | `dict` | `{name, root, type}` — selected library |
| `pending_links` | `list[dict]` | Telegram links/files awaiting destination or batch confirmation |
| `batch_prompted` | `bool` | Whether the current series batch prompt has already been shown |
| `_batch_notices` | `dict` | Debounce state for repeated batch status messages |

### `context.bot_data` (global singletons)

| Key | Type | Purpose |
|-----|------|---------|
| `dl_manager` | `DownloadManager` | Single download queue worker |
| `recent_destinations` | `dict[chat_id, list]` | In-memory cache of recent downloads per chat |
| `settings` | `Settings` | Loaded library configuration |
| `download_batches` | `dict` | Runtime status messages for compact multi-item batch progress |

Call `reset_flow_state(context)` to clear all user_data and chat_data keys on cancel or completion.

## Callback Data Patterns

All callback data uses pipe-delimited prefixes: `prefix|value1|value2`. Every pattern must be registered as a `CallbackQueryHandler` in `app/bot.py`.

| Pattern | Handler | Purpose |
|---------|---------|---------|
| `action\|home` | `handle_menu` | Return to main dashboard |
| `action\|search` | `handle_menu` | Trigger manual TMDb search |
| `action\|queue` | `handle_menu` | Show download queue |
| `tmdb\|{kind}\|{id}` | `handle_tmdb_select` | TMDb result chosen (`kind` = `movie` or `tv`) |
| `page\|{n}` | `handle_tmdb_page` | TMDb results pagination |
| `season\|{n}` | `handle_season` | Season number selected |
| `lib\|{name}` | `handle_library` | Library destination chosen |
| `cancel\|flow` | `handle_cancel` | Inline cancel button |
| `cancel_task\|{id}` | `handle_queue_cancel` | Cancel queued download |
| `continue\|{n}` | `handle_continue` | Quick-add from recent destinations |

Add new patterns to both the handler registration in `app/bot.py` and this table.

## Download Pipeline

1. `app/handlers/download.py::queue_download(url, dest_dir, season_hint)` builds the `tdl` command and enqueues a coroutine factory in `DownloadManager`. `queue_download_batch()` wraps multiple items into one compact Telegram status message.
2. `DownloadManager` processes one task at a time via a single async worker. It is a global singleton stored in `bot_data["dl_manager"]`.
3. `app/services/downloader.py::run_download()` calls `tdl dl` as an async subprocess, parsing progress from stdout. Constants:
   - Retries: 3 on failure
   - Progress updates: rate-limited to max every 5 seconds and min 2% change
   - Global `TDL_LOCK` (asyncio.Lock) serializes all `tdl` invocations to prevent TDLib database conflicts
4. After download: `app/services/extractor.py::extract_archives()` detects multipart RAR/ZIP by scanning for `.rar`/`.part1.rar`/`.zip`, extracts, and removes archives.
5. Series: `app/services/namer.py::bulk_rename()` walks the directory and renames all video files to Plex-compatible SxxExx names.
6. Movies: `app/services/namer.py::rename_movie_files()` renames to `Title (Year).ext`.
7. Permissions: `chown 1000:1000` on all files (hardcoded — make configurable in future).
8. Destination recorded in `bot_data["recent_destinations"]` for quick-add shortcuts.

## Coding Rules

### Async & Blocking
- Telegram handlers are async. Never block the event loop with synchronous I/O.
- Wrap filesystem or CPU-heavy work in `asyncio.to_thread()`.
- Downloads are serialized through `DownloadManager` — do not spawn parallel downloads outside it.

### Telegram UX
- Edit existing messages with `query.message.edit_text()` instead of sending new ones.
- Every multi-step flow must offer a cancel button and a way back to the main menu.
- Button labels must be concise. Use emojis only if they already exist in the codebase.
- Library and category button labels are configurable in `config/libraries.yaml`.

### Downloads & Filesystem
- All library roots come from `config/libraries.yaml`. Never hardcode paths.
- Every filename and folder written to disk must pass through `app/services/namer.py`.
- Treat forwarded messages and links as untrusted input.
- Target ownership: UID 1000, GID 1000 (hardcoded — plan: move to config).

### Error Handling
- Never let an unhandled exception crash the bot process.
- Log exceptions with enough context for debugging.
- Send concise, friendly error messages to the user.
- For subprocess failures, capture exit code and stderr.

### Configuration
- `config/libraries.yaml` is the single source of truth for library definitions.
- `.env` holds secrets only: `TELEGRAM_BOT_TOKEN`, `TMDB_API_KEY`, `ADMIN_CHAT_ID`.
- No hardcoded library types — use `type: series` or `type: movie` from the YAML.

## Known Issues & Gotchas

1. **`pkill -u` is Linux-specific** — `kill_stale_tdl()` in downloader won't work on macOS. Fine in Docker, but document in code.
2. **Hardcoded UID/GID** — `TARGET_UID=1000`, `TARGET_GID=1000`. Should be configurable.
3. ~~**`safe_title` does not normalize Unicode**~~ — Fixed. `_ascii_safe()` now applies NFKD normalization before stripping non-ASCII. Accents and `ñ` → `n` are handled correctly.
4. **Group must be public** — `tdl` cannot resolve download links from private groups. The README and setup docs must make this clear.
5. **AGENTS.md was in `.gitignore`** — removed. Commit this file.
6. **4 placeholder files were removed** — `browse.py`, `create.py`, `season.py`, `selector.py` were empty stubs. Do not recreate them without implementing the feature.

## Verification Checklist

After code changes:
```bash
python -m compileall app config
```
- Verify each new callback pattern is registered in `app/bot.py` and documented in this file.
- Verify new state keys are cleared by `reset_flow_state()` in `app/state.py`.
- Verify all file paths written to disk go through `app/services/namer.py` helpers.
- Test the full flow: forward link → auto-detect → confirm → pick library → download → verify Plex naming.
