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

RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

log()     { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash scripts/setup.sh"

RAGBASE_USER="ragbase"
INSTALL_DIR="/opt/ragbase"
LOG_DIR="/var/log/ragbase"

# ── 1. System update ──────────────────────────────────────────────────────────
section "System Update"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
log "System updated"

# ── 2. System dependencies ────────────────────────────────────────────────────
section "System Dependencies"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    build-essential curl git wget unzip \
    software-properties-common \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    tesseract-ocr tesseract-ocr-eng libtesseract-dev \
    ffmpeg \
    libsqlite3-dev sqlite3 \
    debian-keyring debian-archive-keyring apt-transport-https \
    rclone \
    fail2ban
log "System dependencies installed"

# ── 3. Caddy ──────────────────────────────────────────────────────────────────
section "Caddy"
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq && apt-get install -y -qq caddy
log "Caddy installed"

# ── 4. Firewall ───────────────────────────────────────────────────────────────
section "Firewall (ufw)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment "SSH"
ufw allow 443/tcp  comment "HTTPS (Caddy)"
# Application ports are NOT exposed — Caddy proxies internally
ufw deny 8000/tcp  comment "RAGBase API internal only"
ufw deny 8001/tcp  comment "RAGBase MCP internal only"
ufw --force enable
log "Firewall: ports 22 and 443 only"

# ── 5. fail2ban ───────────────────────────────────────────────────────────────
section "fail2ban (SSH brute-force protection)"
systemctl enable fail2ban
systemctl start fail2ban
log "fail2ban enabled"

# ── 6. System user ────────────────────────────────────────────────────────────
section "System User"
if ! id "$RAGBASE_USER" &>/dev/null; then
    useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$RAGBASE_USER"
    log "Created system user: $RAGBASE_USER (no login shell)"
else
    log "User $RAGBASE_USER already exists"
fi

# ── 7. Directories ────────────────────────────────────────────────────────────
section "Directories"
mkdir -p \
    "$INSTALL_DIR/data" \
    "$INSTALL_DIR/uploads" \
    "$INSTALL_DIR/backups" \
    "$INSTALL_DIR/failed" \
    "$INSTALL_DIR/.whisper_cache" \
    "$INSTALL_DIR/config" \
    "$LOG_DIR" \
    "/var/log/caddy" \
    "/home/$RAGBASE_USER/Dropbox"

chown -R "$RAGBASE_USER:$RAGBASE_USER" "$INSTALL_DIR"
chown -R "$RAGBASE_USER:$RAGBASE_USER" "$LOG_DIR"
chown -R "$RAGBASE_USER:$RAGBASE_USER" "/home/$RAGBASE_USER/Dropbox"

# Sensitive dirs: owner read/write/execute only
chmod 700 "$INSTALL_DIR/data"
chmod 700 "$INSTALL_DIR/backups"
chmod 755 "$INSTALL_DIR"
log "Directories created"

# ── 8. Copy application files ─────────────────────────────────────────────────
section "Application Files"
rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='venv' \
    ./ "$INSTALL_DIR/"
chown -R "$RAGBASE_USER:$RAGBASE_USER" "$INSTALL_DIR"
log "Application files copied to $INSTALL_DIR"

# ── 9. Python virtual environment ────────────────────────────────────────────
section "Python Virtual Environment"
sudo -u "$RAGBASE_USER" python3.11 -m venv "$INSTALL_DIR/venv"
sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel -q
log "Virtual environment created"

# ── 10. Python dependencies ───────────────────────────────────────────────────
section "Python Dependencies"

# PyTorch CPU-only first — avoids pulling 2GB+ CUDA build
log "Installing PyTorch (CPU-only)..."
sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/pip" install \
    torch --index-url https://download.pytorch.org/whl/cpu -q
log "PyTorch installed"

log "Installing remaining dependencies..."
sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/pip" install \
    -r "$INSTALL_DIR/requirements.txt" -q
log "All Python dependencies installed"

# ── 11. Whisper model ────────────────────────────────────────────────────────
section "Whisper Model Pre-download"
log "Downloading Whisper 'medium' model (~1.5 GB) — this takes a few minutes..."
sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/python" - <<'PYEOF'
import os, sys
os.makedirs('/opt/ragbase/.whisper_cache', exist_ok=True)
try:
    import whisper
    whisper.load_model('medium', download_root='/opt/ragbase/.whisper_cache')
    print('Whisper medium model downloaded.')
except Exception as e:
    print(f'WARNING: Whisper download failed: {e}')
    print('The model will be downloaded on first video ingest.')
    sys.exit(0)
PYEOF
log "Whisper model ready"

# ── 12. Environment file ─────────────────────────────────────────────────────
section "Environment Configuration"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    chown "$RAGBASE_USER:$RAGBASE_USER" "$INSTALL_DIR/.env"

    # Auto-generate secrets
    MCP_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    ENC_KEY=$(sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/python" -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

    sed -i "s|replace_with_generated_token|$MCP_TOKEN|" "$INSTALL_DIR/.env"
    sed -i "s|replace_with_generated_key|$ENC_KEY|"     "$INSTALL_DIR/.env"

    warn "Auto-generated secrets written to $INSTALL_DIR/.env"
    warn "MCP Bearer Token: $MCP_TOKEN"
    warn "SAVE THIS TOKEN — you need it for Claude Desktop config."
else
    log ".env already exists — skipping auto-generation"
fi

# ── 13. Database initialisation ──────────────────────────────────────────────
section "Database Initialisation"
sudo -u "$RAGBASE_USER" "$INSTALL_DIR/venv/bin/python" - <<PYEOF
import sys
sys.path.insert(0, '$INSTALL_DIR')
import os
os.chdir('$INSTALL_DIR')

# Load env before importing src modules
from dotenv import load_dotenv
load_dotenv('$INSTALL_DIR/.env')

from src.database import setup_db
setup_db()
print('Database schema created.')
PYEOF
log "Database initialised"

# ── 14. Caddy configuration ───────────────────────────────────────────────────
section "Caddy Configuration"
cp "$INSTALL_DIR/Caddyfile" /etc/caddy/Caddyfile
chown root:caddy /etc/caddy/Caddyfile
chmod 640 /etc/caddy/Caddyfile

# Caddy needs the DOMAIN environment variable at runtime
# Inject it from .env into the caddy systemd override
mkdir -p /etc/systemd/system/caddy.service.d
DOMAIN_VAL=$(grep '^DOMAIN=' "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"')
if [ -n "$DOMAIN_VAL" ]; then
    cat > /etc/systemd/system/caddy.service.d/ragbase.conf << EOF
[Service]
Environment="DOMAIN=${DOMAIN_VAL}"
EOF
    log "Caddy DOMAIN set to: $DOMAIN_VAL"
else
    warn "DOMAIN not set in .env — set it before starting Caddy"
fi

systemctl daemon-reload
systemctl enable caddy
log "Caddy configured"

# ── 15. Systemd services ──────────────────────────────────────────────────────
section "Systemd Services"
cp "$INSTALL_DIR/systemd/ragbase-api.service" /etc/systemd/system/
cp "$INSTALL_DIR/systemd/ragbase-mcp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ragbase-api ragbase-mcp
log "Services enabled (not yet started — see final steps)"

# ── 16. rclone Dropbox sync setup ────────────────────────────────────────────
section "rclone for Dropbox Sync"

# Create rclone config directory for the ragbase user
mkdir -p "/home/$RAGBASE_USER/.config/rclone"
chown -R "$RAGBASE_USER:$RAGBASE_USER" "/home/$RAGBASE_USER/.config"

# Install the rclone systemd service that syncs Dropbox every 5 minutes
cat > /etc/systemd/system/ragbase-dropbox-sync.service << 'EOF'
[Unit]
Description=RAGBase Dropbox Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ragbase
ExecStart=/usr/bin/rclone sync dropbox:/ /home/ragbase/Dropbox \
    --exclude "*.tmp" \
    --exclude ".dropbox" \
    --exclude ".dropbox.cache/**" \
    --log-level INFO \
    --log-file /var/log/ragbase/rclone.log
EOF

cat > /etc/systemd/system/ragbase-dropbox-sync.timer << 'EOF'
[Unit]
Description=RAGBase Dropbox Sync Timer
Requires=ragbase-dropbox-sync.service

[Timer]
OnBootSec=60
OnUnitActiveSec=300
Unit=ragbase-dropbox-sync.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable ragbase-dropbox-sync.timer
log "rclone Dropbox sync timer installed (not active until rclone is configured)"

# ── 17. Log rotation ──────────────────────────────────────────────────────────
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
    postrotate
        systemctl kill -s USR1 ragbase-api 2>/dev/null || true
    endscript
}
EOF
log "Log rotation configured (14-day retention)"

# ── 18. SSH hardening reminder ────────────────────────────────────────────────
section "SSH Hardening Check"
if grep -q "^PasswordAuthentication yes" /etc/ssh/sshd_config 2>/dev/null; then
    warn "SSH password authentication is ENABLED."
    warn "Disable it after adding your SSH key:"
    warn "  echo 'PasswordAuthentication no' >> /etc/ssh/sshd_config"
    warn "  systemctl reload sshd"
else
    log "SSH password authentication is disabled — good."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  RAGBase setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${YELLOW}Required steps before starting:${NC}"
echo ""
echo -e "  1. ${BLUE}Edit /opt/ragbase/.env${NC} and fill in:"
echo -e "       ANTHROPIC_API_KEY     — for auto-tagging (optional)"
echo -e "       DROPBOX_APP_KEY       — from Dropbox developer console"
echo -e "       DROPBOX_APP_SECRET    — from Dropbox developer console"
echo -e "       DROPBOX_REFRESH_TOKEN — from: python scripts/dropbox_auth.py (run locally)"
echo -e "       DOMAIN                — your domain (e.g. ragbase.yourdomain.com)"
echo ""
echo -e "  2. ${BLUE}Configure rclone for Dropbox (run on your local machine):${NC}"
echo -e "       rclone config"
echo -e "       # Create a new remote named 'dropbox', type 'dropbox'"
echo -e "       # Follow OAuth prompts in your browser"
echo -e "       # Copy ~/.config/rclone/rclone.conf to server:"
echo -e "       scp ~/.config/rclone/rclone.conf ragbase@YOUR_IP:/home/ragbase/.config/rclone/"
echo ""
echo -e "  3. ${BLUE}Point your DNS A record to this droplet IP${NC}"
echo -e "       Then set DOMAIN in /opt/ragbase/.env and update Caddy:"
echo -e "       DOMAIN_VAL=\$(grep '^DOMAIN=' /opt/ragbase/.env | cut -d= -f2-)"
echo -e "       echo \"[Service]\" > /etc/systemd/system/caddy.service.d/ragbase.conf"
echo -e "       echo \"Environment=DOMAIN=\$DOMAIN_VAL\" >> /etc/systemd/system/caddy.service.d/ragbase.conf"
echo -e "       systemctl daemon-reload"
echo ""
echo -e "  4. ${BLUE}Start all services:${NC}"
echo -e "       systemctl start ragbase-api ragbase-mcp caddy"
echo -e "       systemctl start ragbase-dropbox-sync.timer"
echo ""
echo -e "  5. ${BLUE}Verify:${NC}"
echo -e "       curl https://YOUR_DOMAIN/health"
echo ""
echo -e "  6. ${BLUE}Add to Claude Desktop (~/.claude/claude_desktop_config.json):${NC}"
echo -e "       {"
echo -e "         \"mcpServers\": {"
echo -e "           \"ragbase\": {"
echo -e "             \"url\": \"https://YOUR_DOMAIN/mcp/sse\","
echo -e "             \"headers\": { \"Authorization\": \"Bearer MCP_BEARER_TOKEN\" }"
echo -e "           }"
echo -e "         }"
echo -e "       }"
echo ""
echo -e "  MCP Bearer Token (save this): ${YELLOW}$(grep '^MCP_BEARER_TOKEN=' /opt/ragbase/.env | cut -d= -f2-)${NC}"
echo ""
echo -e "  Dashboard:  https://YOUR_DOMAIN/"
echo -e "  Health:     https://YOUR_DOMAIN/health"
echo -e "  API docs:   disabled in production (security)"
echo -e "  Logs:       journalctl -u ragbase-api -f"
echo -e "              tail -f /var/log/ragbase/app.log"
echo ""