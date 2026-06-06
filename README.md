<p align="center">
  <img src="./assets/Logo.png" alt="PlexBot" width="320" />
  <p align="center">
    <em>Forward a Telegram link → it lands in Plex, renamed and organized.</em>
  </p>
  <p align="center">
    <a href="#quickstart"><strong>Quick Start</strong></a> ·
    <a href="#features">Features</a> ·
    <a href="#configuration">Configuration</a> ·
    <a href="#docker">Docker</a> ·
    <a href="#how-it-works">How It Works</a>
  </p>
  <p align="center">
    <a href="https://github.com/Ukly0/telegram-to-plex/pkgs/container/plexbot">
      <img src="https://img.shields.io/badge/ghcr.io-pull-blue?logo=docker" alt="Docker image" />
    </a>
  </p>
</p>

---

<p align="center">
  <img src="./assets/example.gif" alt="PlexBot demo" width="860" />
</p>

---

**PlexBot** is an async Telegram bot that downloads media from Telegram groups, automatically detects titles and metadata, matches them against TMDb, renames files for Plex compatibility (ASCII-only, SxxExx format), and places them into the correct library folders — all with zero manual renaming.

## Features

- **Smart filename parsing** — extracts title, season, episode, and year from messy scene-release names
- **TMDb auto-detection** — searches TMDb automatically and shows the top results with posters
- **Plex-compatible renaming** — `S01E02 - Title.mkv` for series, `Title (Year).mkv` for movies, ASCII-only
- **Library auto-detection** — if a show already has a folder, skips library selection
- **Batch downloads** — forward multiple files, confirm once, all queue up
- **Recent destinations** — re-download to the same show/season with one tap
- **Archive extraction** — automatic RAR/ZIP/7z extraction after download
- **FIFO download queue** — single-worker with progress bars, per-title cancel
- **Multi-user groups** — state scoped per authorized chat, any allowed-group member can send links
- **No database** — in-memory session cache, no SQLite, no ORM

## Quickstart

### Option A: One-liner (Recommended)

```bash
git clone https://github.com/Ukly0/telegram-to-plex.git plexbot
cd plexbot
./setup.sh
```

The script will ask for your credentials, generate config files, pull the Docker image, and start the bot.

After setup, authenticate `tdl`:

```bash
docker exec -it -u plexbot -e TDL_HOME=/data/tdl $(docker compose ps -q plexbot) tdl login -T qr
```

### Option B: Manual setup

<details>
<summary>Click to expand</summary>

#### 1. Create a Telegram Bot

Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.

#### 2. Get a TMDb API Key

Register at [https://www.themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) → request an API key (free).

#### 3. Configure

```bash
git clone https://github.com/Ukly0/telegram-to-plex.git plexbot
cd plexbot
cp config/.env.example config/.env
cp config/libraries.yaml.example config/libraries.yaml
```

Edit `config/.env` with your credentials:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF
TMDB_API_KEY=your_tmdb_bearer_token
ADMIN_USER_IDS=123456789
ALLOWED_CHAT_IDS=-1001234567890
```

Edit `docker-compose.yml` to mount your media paths (or create `docker-compose.override.yml`):

```yaml
services:
  plexbot:
    volumes:
      - /your/host/tv:/media/tv
      - /your/host/movies:/media/movies
      - /your/host/anime:/media/anime
```

#### 4. Launch

```bash
docker compose up -d
```

#### 5. Authenticate tdl

```bash
docker exec -it -u plexbot -e TDL_HOME=/data/tdl $(docker compose ps -q plexbot) tdl login -T qr
```

</details>

### Important: Disable Group Privacy

In [@BotFather](https://t.me/BotFather): `/mybots` → your bot → **Bot Settings** → **Group Privacy** → **Turn off**

Then remove and re-add the bot to your group. Without this, the bot cannot see forwarded messages/files.

### How to find your IDs

| Value | How to get it |
|---|---|
| Admin User ID | Message [@userinfobot](https://t.me/userinfobot) on Telegram |
| Group Chat ID | Add [@RawDataBot](https://t.me/RawDataBot) to your group, it will reply with the chat ID, then remove it |

## Docker

The prebuilt image is published at `ghcr.io/ukly0/plexbot:latest` and is pulled automatically by `docker compose up`.

To build locally instead:

```bash
docker compose build
docker compose up -d
```

### Volumes

| Path | Purpose |
|---|---|
| `./config:/app/config:ro` | Bot configuration (`.env`, `libraries.yaml`) |
| `plexbot-data:/data` | tdl session (authentication persists here) |
| `/your/media/tv:/media/tv` | Your Plex TV library |
| `/your/media/movies:/media/movies` | Your Plex Movies library |
| `/your/media/anime:/media/anime` | Your Plex Anime library |

### Re-authenticating tdl

If downloads fail with `not authorized`, re-login:

```bash
docker exec -it -u plexbot -e TDL_HOME=/data/tdl $(docker compose ps -q plexbot) tdl login -T qr
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `TMDB_API_KEY` | Yes | TMDb API v3 Bearer token |
| `ALLOWED_CHAT_IDS` | Yes | Comma/space separated Telegram chat IDs where the bot may run |
| `ADMIN_USER_IDS` | Yes | Comma/space separated Telegram user IDs allowed to use admin commands |
| `ADMIN_CHAT_ID` | No | Legacy alias for `ADMIN_USER_IDS` |
| `TDL_HOME` | No | Path to tdl session directory (default: `~/.tdl`) |

### Library Types

| Type | Behavior | Naming | Folder Structure |
|---|---|---|---|
| `series` | Asks for season number | `S01E02 - Title.mkv` | `Show (Year)/Season 01/` |
| `movie` | Auto-queues immediately | `Title (Year).mkv` | `Title (Year)/` |

### Download Settings

In `config/libraries.yaml`:

```yaml
download:
  tdl_template: 'tdl dl -u {url} -d "{dir}" -t 16 -l 9 --reconnect-timeout 0 --template "{{ .FileName }}"'
  # tdl_home: /data/tdl  # optional: separate session directory
```

- `{url}` and `{dir}` are replaced at runtime
- `--template "{{ .FileName }}"` preserves original filenames (avoids Go template conflicts)
- `-t 16` = 16 threads, `-l 9` = log level 9 (progress)

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    User sends link/file                  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Filename parsing    │
              │  "Show.S01E02.      │
              │   1080p.WEB-DL.mkv" │
              │       ↓             │
              │  Title: "Show"      │
              │  Season: 1          │
              │  Episode: 2         │
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  TMDb auto-search   │
              │  → Top 3 results    │
              │  → Poster images    │
              └──────────┬──────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
         ┌─────────┐         ┌──────────┐
         │  Series  │         │  Movie   │
         │          │         │          │
         │ Pick     │         │ Auto-    │
         │ season   │         │ queue    │
         └────┬─────┘         └────┬─────┘
              │                    │
              ▼                    ▼
         ┌──────────────────────────────┐
         │      Download via tdl        │
         │  (FIFO queue, progress bar)  │
         └──────────────┬───────────────┘
                        │
                        ▼
         ┌──────────────────────────────┐
         │      Post-processing          │
         │  1. Extract archives (RAR/ZIP)│
         │  2. Rename for Plex            │
         │  3. Set permissions (1000:1000)│
         └──────────────────────────────┘
```

### Filename Parsing

PlexBot detects metadata from messy filenames:

| Input | Title | Season | Year |
|---|---|---|---|
| `Breaking.Bad.S01E02.1080p.WEB-DL.x264.mkv` | Breaking Bad | 1 | — |
| `Euphoria.(2019).S03E05.1080p.WEB-DL.mkv` | Euphoria | 3 | 2019 |
| `Oppenheimer.(2023).1080p.WEB-DL.mkv` | Oppenheimer | — | 2023 |
| `Te van a matar (2026) by kowalski&xusman` | Te van a matar | — | 2026 |
| `Greenland 2 (2026) UHD BluRay REMUX 2160p` | Greenland 2 | — | 2026 |

Extracted season is pre-filled in the season picker. Year is used in folder names. All SxxExx patterns, resolution tags, codec names, language codes, and release group suffixes are stripped before TMDb search.

## Commands

| Command | Description |
|---|---|
| `/start` | Show main menu |
| `/menu` | Return to dashboard |
| `/search` | Manual TMDb search |
| `/queue` | View running/pending downloads |
| `/cancel` | Cancel current flow + running download |
| `/cancel_all` | Cancel everything for this chat |
| `/clean_tmp` | Remove temp download folders (admin only) |

## Groups vs DMs

- Works only in chats listed in `ALLOWED_CHAT_IDS`, plus private chats with users in `ADMIN_USER_IDS`
- In allowed groups, any member can send links — state is scoped per chat
- If the bot receives an update from an unauthorized group, it logs the chat ID and leaves that group
- Groups must be **public** (or have a public invite link) for `tdl` to resolve forwarded message download links

## Project Structure

```
app/
├── bot.py               # Entry point — handler registration
├── config.py            # Libraries YAML + .env loader
├── state.py             # Conversation state constants + reset
├── handlers/
│   ├── ingest.py         # Link/file intake — auto metadata, batch handling
│   ├── search.py         # TMDb search, season/library selection
│   ├── menu.py           # /start, /menu, dashboard, queue view
│   └── download.py       # Download queue, tdl subprocess, post-process
└── services/
    ├── tmdb.py           # TMDb API client
    ├── downloader.py     # tdl subprocess wrapper — progress, retries
    ├── extractor.py      # RAR/ZIP/7z detection and extraction
    └── namer.py          # Plex-safe naming — ASCII, SxxExx, collision handling

config/
└── libraries.yaml        # Library definitions (user-editable)
```

## Limitations

- **No persistence** — in-memory state resets on restart (download queue, recent destinations, conversation state)
- **Single download worker** — downloads are sequential (one `tdl` at a time to avoid TDLib session conflicts)
- **Public groups only** — `tdl` cannot resolve download links from private Telegram groups

## License

MIT
