#!/usr/bin/env bash
set -euo pipefail

# PlexBot Quick Start Setup
# Generates config/.env and config/libraries.yaml, pulls the image, and starts the bot.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[X]${NC} $*"; exit 1; }
header(){ echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Preflight ──────────────────────────────────────────────

check_command() {
    if ! command -v "$1" &>/dev/null; then
        error "Required command '$1' not found. Please install it first."
    fi
}

header "Preflight checks"
check_command docker
check_command docker

if ! docker info &>/dev/null 2>&1; then
    error "Docker is not running. Please start Docker first."
fi
info "Docker is running"

# Check docker compose (v2) or docker-compose (v1)
if docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    error "Docker Compose not found. Install it: https://docs.docker.com/compose/install/"
fi
info "Using: $COMPOSE"

# ── Configuration ──────────────────────────────────────────

header "Configuration"

if [ -f config/.env ] && [ -s config/.env ]; then
    warn "config/.env already exists. Skipping interactive setup."
    warn "Edit it manually if needed."
else
    echo ""
    echo "Enter your credentials (values are saved to config/.env):"
    echo ""

    read -rp "Telegram Bot Token (from @BotFather): " BOT_TOKEN
    [ -z "$BOT_TOKEN" ] && error "Bot token is required"

    read -rp "TMDb API Key (Bearer token from themoviedb.org): " TMDB_KEY
    [ -z "$TMDB_KEY" ] && error "TMDb API key is required"

    read -rp "Admin User ID (your Telegram user ID): " ADMIN_ID
    [ -z "$ADMIN_ID" ] && error "Admin user ID is required"

    read -rp "Allowed Group Chat ID (Telegram group ID, e.g. -1001234567890): " GROUP_ID
    [ -z "$GROUP_ID" ] && error "Group chat ID is required"

    mkdir -p config
    cat > config/.env <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TMDB_API_KEY=$TMDB_KEY
ADMIN_USER_IDS=$ADMIN_ID
ALLOWED_CHAT_IDS=$GROUP_ID
EOF
    info "config/.env created"
fi

if [ -f config/libraries.yaml ] && [ -s config/libraries.yaml ]; then
    warn "config/libraries.yaml already exists. Skipping."
else
    mkdir -p config
    cp config/libraries.yaml.example config/libraries.yaml 2>/dev/null || cat > config/libraries.yaml <<'EOF'
libraries:
  - name: "TV Shows"
    type: series
    root: /media/tv

  - name: "Movies"
    type: movie
    root: /media/movies

  - name: "Anime"
    type: series
    root: /media/anime

download:
  tdl_template: 'tdl dl -u {url} -d "{dir}" -t 16 -l 9 --reconnect-timeout 0
    --template "{{ .FileName }}"'
  tdl_home: /data/tdl
EOF
    info "config/libraries.yaml created"
fi

if [ -f docker-compose.override.yml ]; then
    warn "docker-compose.override.yml already exists. Skipping."
else
    header "Media paths"
    echo ""
    echo "Map your host media paths to container paths."
    echo "These must match the 'root' values in config/libraries.yaml."
    echo "Press Enter to use defaults (you can edit docker-compose.override.yml later)."
    echo ""

    read -rp "Host TV path [/path/to/tv]: " TV_PATH
    TV_PATH="${TV_PATH:-/path/to/tv}"

    read -rp "Host Movies path [/path/to/movies]: " MOVIES_PATH
    MOVIES_PATH="${MOVIES_PATH:-/path/to/movies}"

    read -rp "Host Anime path [/path/to/anime]: " ANIME_PATH
    ANIME_PATH="${ANIME_PATH:-/path/to/anime}"

    cat > docker-compose.override.yml <<EOF
services:
  plexbot:
    volumes:
      - ${TV_PATH}:/media/tv
      - ${MOVIES_PATH}:/media/movies
      - ${ANIME_PATH}:/media/anime
EOF
    info "docker-compose.override.yml created"
fi

# ── Launch ─────────────────────────────────────────────────

header "Launch"
echo ""
read -rp "Pull and start PlexBot now? [Y/n] " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    warn "Skipping. Run later:"
    echo "  $COMPOSE pull && $COMPOSE up -d"
    echo ""
    warn "Then authenticate tdl:"
    echo "  docker exec -it -u plexbot -e TDL_HOME=/data/tdl \$(docker compose ps -q plexbot) tdl login -T qr"
    exit 0
fi

$COMPOSE pull
$COMPOSE up -d
sleep 3

if $COMPOSE ps | grep -q "running\|Up"; then
    info "PlexBot is running!"
else
    error "PlexBot failed to start. Check logs: $COMPOSE logs"
fi

# ── Post-setup ─────────────────────────────────────────────

header "Next steps"
echo ""
echo "1. Authenticate tdl (one-time):"
echo "   docker exec -it -u plexbot -e TDL_HOME=/data/tdl \$(docker compose ps -q plexbot) tdl login -T qr"
echo ""
echo "2. In @BotFather, disable Group Privacy:"
echo "   /mybots → your bot → Bot Settings → Group Privacy → Turn off"
echo "   Then remove and re-add the bot to your group."
echo ""
echo "3. Forward a Telegram link to the bot's group and it will download!"
echo ""
echo "Useful commands:"
echo "  $COMPOSE logs -f          # Follow logs"
echo "  $COMPOSE restart          # Restart"
echo "  $COMPOSE down             # Stop"
echo ""
