#!/usr/bin/env bash
# =============================================================================
# RAGBase — Server Setup Script
# Tested on: Ubuntu 22.04 LTS (DigitalOcean Droplet, 4GB RAM / 80GB SSD)
# Run as: sudo bash scripts/setup.sh
#
# What this does:
#   - Installs all system dependencies
#   - Installs and configures Caddy
#   - Hardens the firewall (ports 22 + 443 only)
#   - Creates ragbase system user with locked-down permissions
#   - Sets up Python virtual environment and installs packages
#   - Pre-downloads Whisper medium model
#   - Installs and enables systemd services
#   - Initialises the SQLite database
#   - Installs rclone for Dropbox sync (headless-compatible)
#   - Configures log rotation
#
# Dropbox sync note:
#   The official Dropbox daemon requires interactive browser auth on
#   first run and is not scriptable headlessly. This script installs
#   rclone instead, which supports non-interactive Dropbox OAuth via
#   a config file you generate locally and copy to the server.
#   See the "Dropbox setup" section at the bottom for instructions.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()    { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn()   { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section(){ echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

# ── Require root ──────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash scripts/setup.sh"

# ── Resolve project root (script lives in scripts/, project root is one up) ──
# FIX: All previous path references assumed CWD=project root, which broke
# when running as: sudo bash scripts/setup.sh from the project root.
# We now resolve the project root relative to this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Variables ─────────────────────────────────────────────────────────────────
RAGBASE_USER="ragbase"
RAGBASE_HOME="/opt/ragbase"
LOG_DIR="/var/log/ragbase"
PYTHON_BIN="/usr/bin/python3.11"   # FIX: use absolute path — not on PATH for system user yet

section "System Update"
apt-get update -qq
apt-get upgrade -y -qq
log "System updated"

section "System Dependencies"
apt-get install -y -qq \
    build-essential \
    curl \
    git \
    wget \
    unzip \
    software-properties-common \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtesseract-dev \
    ffmpeg \
    libsqlite3-dev \
    sqlite3 \
    debian-keyring \
    debian-archive-keyring \
    apt-transport-https

log "System dependencies installed"

section "Caddy Installation"
# Official Caddy installation from Caddy's apt repository
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq caddy
log "Caddy installed"

section "Firewall Configuration"
# Only allow SSH and HTTPS — everything else is blocked
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment "SSH"
ufw allow 443/tcp  comment "HTTPS"
# Explicitly deny direct access to application ports
ufw deny 8000/tcp  comment "RAGBase API (internal only)"
ufw deny 8001/tcp  comment "RAGBase MCP (internal only)"
ufw --force enable
log "Firewall configured: ports 22 and 443 only"

section "System User"
if ! id "$RAGBASE_USER" &>/dev/null; then
    useradd -r -m -d "$RAGBASE_HOME" -s /bin/bash "$RAGBASE_USER"
    log "Created system user: $RAGBASE_USER"
else
    log "User $RAGBASE_USER already exists"
fi

section "Directory Structure"
mkdir -p \
    "$RAGBASE_HOME" \
    "$RAGBASE_HOME/data" \
    "$RAGBASE_HOME/uploads" \
    "$RAGBASE_HOME/backups" \
    "$RAGBASE_HOME/failed" \
    "$RAGBASE_HOME/.whisper_cache" \
    "$LOG_DIR" \
    "/var/log/caddy"

# Set permissions
chown -R "$RAGBASE_USER:$RAGBASE_USER" "$RAGBASE_HOME"
chown -R "$RAGBASE_USER:$RAGBASE_USER" "$LOG_DIR"
chmod 700 "$RAGBASE_HOME/data"      # DB directory — owner only
chmod 700 "$RAGBASE_HOME/backups"   # Backups — owner only
log "Directories created with secure permissions"

# ── Application Files ─────────────────────────────────────────────────────────
# FIX: rsync BEFORE creating the venv. Previously the venv was created inside
# $RAGBASE_HOME first, then rsync'd over with the source tree (which has no
# venv), silently deleting the freshly built venv directory.
section "Application Files"
rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.env' \
    "$PROJECT_ROOT/" "$RAGBASE_HOME/"   # FIX: use resolved PROJECT_ROOT, not bare ./
chown -R "$RAGBASE_USER:$RAGBASE_USER" "$RAGBASE_HOME"
log "Application files copied"

section "Python Virtual Environment"
# FIX: use absolute Python path — python3.11 may not be on PATH for ragbase
# user at this point in setup.
sudo -u "$RAGBASE_USER" "$PYTHON_BIN" -m venv "$RAGBASE_HOME/venv"
sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/pip" install --upgrade pip -q
log "Virtual environment created"

section "Python Dependencies"
# Install PyTorch CPU-only (saves ~2GB vs full torch)
sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/pip" install \
    torch --index-url https://download.pytorch.org/whl/cpu -q

sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/pip" install \
    -r "$RAGBASE_HOME/requirements.txt" -q   # requirements.txt already at RAGBASE_HOME via rsync
log "Python dependencies installed"

section "Whisper Model Download"
# Pre-download Whisper medium model so first ingest doesn't time out
sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/python" -c "
import whisper, os
os.makedirs('$RAGBASE_HOME/.whisper_cache', exist_ok=True)
print('Downloading Whisper medium model (~1.5GB)...')
whisper.load_model('medium', download_root='$RAGBASE_HOME/.whisper_cache')
print('Done.')
"
log "Whisper model downloaded"

section "Environment Configuration"
if [ ! -f "$RAGBASE_HOME/.env" ]; then
    cp "$RAGBASE_HOME/.env.example" "$RAGBASE_HOME/.env"
    chmod 600 "$RAGBASE_HOME/.env"     # Owner read/write only
    chown "$RAGBASE_USER:$RAGBASE_USER" "$RAGBASE_HOME/.env"

    # Auto-generate secrets — users only need to fill in external credentials
    MCP_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    ENC_KEY=$(sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/python" -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

    sed -i "s|your_secret_token_here|$MCP_TOKEN|"  "$RAGBASE_HOME/.env"
    sed -i "s|your_fernet_key_here|$ENC_KEY|"      "$RAGBASE_HOME/.env"

    warn "Generated MCP bearer token: $MCP_TOKEN"
    warn "Add this token to your Claude Desktop config (see next steps below)."
    # FIX: removed mention of DROPBOX_ACCESS_TOKEN (deprecated) and BACKUP_PASSPHRASE
    # (unused). Correct Dropbox vars are DROPBOX_APP_KEY / APP_SECRET / REFRESH_TOKEN.
    # ENCRYPTION_KEY is auto-generated above — no user action needed.
    warn "Still required in $RAGBASE_HOME/.env:"
    warn "  ANTHROPIC_API_KEY        — for auto-tagging (get from console.anthropic.com)"
    warn "  DROPBOX_APP_KEY          — from dropbox.com/developers"
    warn "  DROPBOX_APP_SECRET       — from dropbox.com/developers"
    warn "  DROPBOX_REFRESH_TOKEN    — run: python scripts/dropbox_auth.py (locally)"
    warn "  DOMAIN                   — your domain, e.g. ragbase.yourdomain.com"
else
    log ".env already exists — skipping"
fi

section "Caddy Configuration"
# FIX: use PROJECT_ROOT to locate Caddyfile — previously used bare 'Caddyfile'
# which only worked if CWD happened to be the project root.
cp "$PROJECT_ROOT/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
log "Caddyfile installed"

section "Systemd Services"
# FIX: use PROJECT_ROOT to locate service files — same CWD assumption bug as above.
cp "$PROJECT_ROOT/systemd/ragbase-api.service" /etc/systemd/system/
cp "$PROJECT_ROOT/systemd/ragbase-mcp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ragbase-api
systemctl enable ragbase-mcp
log "Systemd services installed and enabled"

section "Database Initialisation"
sudo -u "$RAGBASE_USER" "$RAGBASE_HOME/venv/bin/python" -c "
import sys; sys.path.insert(0, '$RAGBASE_HOME')
from dotenv import load_dotenv; load_dotenv('$RAGBASE_HOME/.env')
from src.database import setup_db; setup_db()
print('Database initialised')
"
log "Database initialised"

section "Log Rotation"
cat > /etc/logrotate.d/ragbase << 'EOF'
/var/log/ragbase/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 ragbase ragbase
}
EOF
log "Log rotation configured"

section "Setup Complete"
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  RAGBase setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo ""
echo -e "  1. Edit ${BLUE}$RAGBASE_HOME/.env${NC} and fill in:"
echo -e "       ANTHROPIC_API_KEY       — for auto-tagging (console.anthropic.com)"
echo -e "       DROPBOX_APP_KEY         — from dropbox.com/developers"
echo -e "       DROPBOX_APP_SECRET      — from dropbox.com/developers"
echo -e "       DROPBOX_REFRESH_TOKEN   — run: python scripts/dropbox_auth.py (on your laptop)"
echo -e "       DOMAIN                  — e.g. ragbase.yourdomain.com"
echo -e ""
echo -e "       (ENCRYPTION_KEY and MCP_BEARER_TOKEN were auto-generated — no action needed)"
echo ""
echo -e "  2. Run the Dropbox OAuth helper ON YOUR LAPTOP to get the refresh token:"
echo -e "       ${BLUE}pip install dropbox${NC}"
echo -e "       ${BLUE}python scripts/dropbox_auth.py${NC}"
echo -e "       Then copy the three DROPBOX_* values into $RAGBASE_HOME/.env"
echo ""
echo -e "  3. Install the Dropbox daemon on the droplet (syncs your files):"
echo -e "       ${BLUE}cd ~ && wget -O dropbox.py 'https://www.dropbox.com/download?dl=packages/dropbox.py'${NC}"
echo -e "       ${BLUE}python3 dropbox.py start${NC}"
echo ""
echo -e "  4. Point your domain DNS → this droplet IP, then start services:"
echo -e "       ${BLUE}systemctl start ragbase-api ragbase-mcp caddy${NC}"
echo ""
echo -e "  5. Add to Claude Desktop config (~/.claude/claude_desktop_config.json):"
echo -e "       {"
echo -e "         \"mcpServers\": {"
echo -e "           \"ragbase\": {"
echo -e '             "url": "https://YOUR_DOMAIN/mcp/sse",'
echo -e '             "headers": { "Authorization": "Bearer YOUR_MCP_TOKEN" }'
echo -e "           }"
echo -e "         }"
echo -e "       }"
echo -e "       Replace YOUR_MCP_TOKEN with the token printed above."
echo ""
echo -e "  6. Drop a PDF in your Dropbox folder — first ingest runs within 5 minutes."
echo ""
echo -e "  Dashboard: ${BLUE}https://YOUR_DOMAIN/${NC}"
echo -e "  Health:    ${BLUE}https://YOUR_DOMAIN/health${NC}"
echo -e "  Logs:      ${BLUE}journalctl -u ragbase-api -f${NC}"
echo ""