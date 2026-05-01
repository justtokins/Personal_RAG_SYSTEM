#!/usr/bin/env python3
"""
scripts/preflight.py — Pre-launch system check.

Run this BEFORE starting services to catch every configuration problem
that would cause RAGBase to silently fail or crash.

Usage:
    python scripts/preflight.py              # check everything
    python scripts/preflight.py --fix        # auto-fix what can be fixed
    python scripts/preflight.py --no-color   # plain output (for logs)

Exit codes:
    0 — all checks passed (or only warnings)
    1 — one or more FAIL checks — do not start services
"""
import os
import sys
import shutil
import sqlite3
import subprocess
import importlib
from pathlib import Path
from typing import Callable

# ── Add project root to path ──────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Colour helpers ────────────────────────────────────────────────────────────
USE_COLOR = "--no-color" not in sys.argv

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

RED    = lambda t: _c("0;31", t)
GREEN  = lambda t: _c("0;32", t)
YELLOW = lambda t: _c("1;33", t)
BLUE   = lambda t: _c("0;34", t)
BOLD   = lambda t: _c("1",    t)

# ── Result tracking ───────────────────────────────────────────────────────────
results: list[tuple[str, str, str]] = []   # (status, label, detail)

def PASS(label: str, detail: str = ""):
    results.append(("PASS", label, detail))
    mark = GREEN("  ✓  ")
    print(f"{mark} {label}" + (f"  {_c('0;90', detail)}" if detail else ""))

def FAIL(label: str, detail: str = ""):
    results.append(("FAIL", label, detail))
    mark = RED("  ✗  ")
    print(f"{mark} {BOLD(label)}" + (f"\n       {RED(detail)}" if detail else ""))

def WARN(label: str, detail: str = ""):
    results.append(("WARN", label, detail))
    mark = YELLOW("  ⚠  ")
    print(f"{mark} {label}" + (f"\n       {YELLOW(detail)}" if detail else ""))

def section(title: str):
    print(f"\n{BLUE(BOLD(f'── {title} ──'))}")

# ── Load .env early so checks can read it ────────────────────────────────────
ENV_PATH = Path("/opt/ragbase/.env")
_env_loaded = False
if ENV_PATH.exists():
    from dotenv import dotenv_values
    _env = dotenv_values(str(ENV_PATH))
    os.environ.update({k: v for k, v in _env.items() if v})
    _env_loaded = True

def env(key: str) -> str:
    return os.environ.get(key, "").strip()

# =============================================================================
# CHECK FUNCTIONS
# =============================================================================

def check_env_file():
    section("Environment File")
    if not ENV_PATH.exists():
        FAIL(".env file missing", f"Expected at {ENV_PATH}")
        FAIL("Cannot continue — all credential checks will fail")
        return
    perms = oct(ENV_PATH.stat().st_mode)[-3:]
    if perms != "600":
        WARN(".env permissions are not 600",
             f"Current: {perms}. Fix: chmod 600 {ENV_PATH}")
    else:
        PASS(".env permissions are 600")
    PASS(".env file exists", str(ENV_PATH))


def check_required_credentials():
    section("Required Credentials")

    # MCP token
    tok = env("MCP_BEARER_TOKEN")
    if not tok or tok in ("your_secret_token_here", "your_generated_mcp_bearer_token_here"):
        FAIL("MCP_BEARER_TOKEN not set or is placeholder")
    elif len(tok) < 32:
        WARN("MCP_BEARER_TOKEN is short", "Recommend at least 32 characters")
    else:
        PASS("MCP_BEARER_TOKEN set", f"{tok[:8]}…")

    # Encryption key
    enc = env("ENCRYPTION_KEY")
    if not enc or enc == "your_fernet_key_here":
        FAIL("ENCRYPTION_KEY not set or is placeholder",
             "Backups cannot be encrypted. Run setup.sh to auto-generate.")
    else:
        # Validate it's a real Fernet key
        try:
            from cryptography.fernet import Fernet
            Fernet(enc.encode() if isinstance(enc, str) else enc)
            PASS("ENCRYPTION_KEY is a valid Fernet key")
        except Exception:
            FAIL("ENCRYPTION_KEY is invalid",
                 "Must be a URL-safe base64 32-byte key. Re-run setup.sh.")

    # Domain
    domain = env("DOMAIN")
    if not domain or domain in ("ragbase.yourdomain.com", ""):
        FAIL("DOMAIN not set", "Caddy cannot provision a TLS certificate without it.")
    else:
        PASS("DOMAIN set", domain)

    # Dropbox
    for var in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        val = env(var)
        placeholder = f"your_{var.lower()}_here"
        if not val or val == placeholder:
            FAIL(f"{var} not set", "Backups will fail silently. Run scripts/dropbox_auth.py.")
        else:
            PASS(f"{var} set", f"{val[:6]}…")


def check_optional_credentials():
    section("Optional Credentials")
    key = env("ANTHROPIC_API_KEY")
    if not key or key in ("sk-ant-...", ""):
        WARN("ANTHROPIC_API_KEY not set",
             "Auto-tagging is disabled. Documents will ingest without tags.")
    else:
        PASS("ANTHROPIC_API_KEY set", f"{key[:12]}…")


def check_system_binaries():
    section("System Binaries")
    for binary, pkg in [
        ("tesseract", "tesseract-ocr"),
        ("ffmpeg",    "ffmpeg"),
        ("python3.11",None),
        ("caddy",     "caddy"),
        ("systemctl", None),
    ]:
        path = shutil.which(binary)
        if path:
            PASS(f"{binary} found", path)
        else:
            hint = f"Install: apt-get install {pkg}" if pkg else ""
            FAIL(f"{binary} not found", hint)


def check_python_packages():
    section("Python Packages")
    VENV_PYTHON = "/opt/ragbase/venv/bin/python"

    if not Path(VENV_PYTHON).exists():
        FAIL("venv not found at /opt/ragbase/venv",
             "Run setup.sh first.")
        return

    packages = [
        ("fastapi",              None),
        ("uvicorn",              None),
        ("sqlite_vec",           None),
        ("sentence_transformers",None),
        ("fitz",                 "pymupdf"),
        ("docx",                 "python-docx"),
        ("extract_msg",          None),
        ("pytesseract",          None),
        ("whisper",              "openai-whisper"),
        ("ffmpeg",               "ffmpeg-python"),
        ("anthropic",            None),
        ("dropbox",              None),
        ("cryptography",         None),
        ("apscheduler",          None),
        ("tenacity",             None),
        ("xxhash",               None),
    ]

    result = subprocess.run(
        [VENV_PYTHON, "-c",
         "import importlib, sys\n"
         "pkgs=" + str([p[0] for p in packages]) + "\n"
         "missing=[p for p in pkgs if importlib.util.find_spec(p) is None]\n"
         "print('\\n'.join(missing))"],
        capture_output=True, text=True
    )
    missing = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

    for import_name, install_name in packages:
        if import_name in missing:
            FAIL(f"{import_name} not installed",
                 f"pip install {install_name or import_name}")
        else:
            PASS(f"{import_name}", "")


def check_sqlite_vec():
    section("sqlite-vec Extension")
    VENV_PYTHON = "/opt/ragbase/venv/bin/python"
    if not Path(VENV_PYTHON).exists():
        FAIL("venv missing — skipping sqlite-vec check")
        return

    test = subprocess.run(
        [VENV_PYTHON, "-c", """
import sqlite3, sqlite_vec
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
rows = conn.execute("SELECT vec_version()").fetchone()
print(rows[0])
"""],
        capture_output=True, text=True
    )
    if test.returncode == 0:
        PASS("sqlite-vec loads and queries correctly", f"version {test.stdout.strip()}")
    else:
        FAIL("sqlite-vec failed to load",
             test.stderr.strip().split("\n")[-1] if test.stderr else "unknown error")
        WARN("sqlite-vec fix",
             "Ubuntu's libsqlite3 may have extension loading disabled.\n"
             "       Try: pip install sqlite-vec --force-reinstall")


def check_numpy_whisper_compat():
    section("NumPy / Whisper Compatibility")
    VENV_PYTHON = "/opt/ragbase/venv/bin/python"
    if not Path(VENV_PYTHON).exists():
        return

    test = subprocess.run(
        [VENV_PYTHON, "-c", """
import numpy as np
major = int(np.__version__.split('.')[0])
print(np.__version__)
if major >= 2:
    raise RuntimeError("numpy>=2.0 breaks openai-whisper")
"""],
        capture_output=True, text=True
    )
    if test.returncode == 0:
        PASS("NumPy version compatible with Whisper", test.stdout.strip())
    else:
        np_ver = test.stdout.strip() or "unknown"
        FAIL(f"NumPy {np_ver} is incompatible with openai-whisper",
             'Fix: pip install "numpy<2.0" --force-reinstall')


def check_whisper_model():
    section("Whisper Model")
    cache = Path("/opt/ragbase/.whisper_cache")
    # Whisper medium model is ~1.5GB, stored as a .pt file
    models = list(cache.glob("*.pt")) if cache.exists() else []
    if models:
        PASS("Whisper model file found", models[0].name)
    else:
        WARN("No Whisper .pt model found in cache",
             f"Expected in {cache}. First video ingest will trigger a ~1.5GB download.")


def check_directories():
    section("Directory Structure")
    dirs = {
        "/opt/ragbase":              ("ragbase", "755"),
        "/opt/ragbase/data":         ("ragbase", "700"),
        "/opt/ragbase/backups":      ("ragbase", "700"),
        "/opt/ragbase/uploads":      ("ragbase", "755"),
        "/opt/ragbase/failed":       ("ragbase", "755"),
        "/opt/ragbase/.whisper_cache":("ragbase","755"),
        "/var/log/ragbase":          ("ragbase", "755"),
    }
    import pwd, grp
    for path, (expected_owner, _) in dirs.items():
        p = Path(path)
        if not p.exists():
            FAIL(f"Directory missing: {path}")
            continue
        try:
            owner = pwd.getpwuid(p.stat().st_uid).pw_name
        except KeyError:
            owner = str(p.stat().st_uid)
        if owner != expected_owner:
            WARN(f"{path} owner is '{owner}'", f"Expected '{expected_owner}'")
        else:
            PASS(f"{path}", f"owner={owner}")


def check_caddyfile():
    section("Caddyfile")
    caddy_path = Path("/etc/caddy/Caddyfile")
    if not caddy_path.exists():
        FAIL("Caddyfile not found at /etc/caddy/Caddyfile")
        return

    content = caddy_path.read_text()

    # Check domain is injected (not still a raw {$DOMAIN} with no env var set)
    domain = env("DOMAIN")
    if "{$DOMAIN}" in content and not domain:
        FAIL("Caddyfile uses {$DOMAIN} but DOMAIN env var is not set",
             "Caddy will fail to start. Set DOMAIN in .env and restart caddy.")
    elif "{$DOMAIN}" in content and domain:
        PASS("Caddyfile uses {$DOMAIN}", f"DOMAIN={domain} — ensure caddy service loads .env")
    else:
        PASS("Caddyfile domain configured")

    # Check MCP path stripping — the critical bug
    if "uri strip_prefix /mcp" not in content:
        FAIL("Caddyfile missing 'uri strip_prefix /mcp'",
             "MCP /sse endpoint will return 404. Claude Desktop cannot connect.\n"
             "       Add 'uri strip_prefix /mcp' inside the 'handle /mcp/*' block.")
    else:
        PASS("Caddyfile has MCP path stripping")

    # Check security headers present
    for header in ("X-Frame-Options", "X-Content-Type-Options"):
        if header in content:
            PASS(f"Security header: {header}")
        else:
            WARN(f"Security header missing: {header}")


def check_systemd_services():
    section("Systemd Services")
    for svc in ("ragbase-api", "ragbase-mcp", "caddy"):
        result = subprocess.run(
            ["systemctl", "is-enabled", svc],
            capture_output=True, text=True
        )
        enabled = result.stdout.strip() == "enabled"
        if enabled:
            PASS(f"{svc} is enabled")
        else:
            WARN(f"{svc} is not enabled for auto-start",
                 f"Fix: systemctl enable {svc}")

        result2 = subprocess.run(
            ["systemctl", "is-active", svc],
            capture_output=True, text=True
        )
        active = result2.stdout.strip() == "active"
        if active:
            PASS(f"{svc} is running")
        else:
            WARN(f"{svc} is not running",
                 f"Start: systemctl start {svc}")


def check_dropbox_daemon():
    section("Dropbox Daemon")
    dropbox_dir = Path("/home/ragbase/Dropbox")
    proc = subprocess.run(
        ["pgrep", "-x", "dropbox"], capture_output=True
    )
    if proc.returncode == 0:
        PASS("Dropbox daemon is running")
    else:
        WARN("Dropbox daemon is not running",
             "Files won't sync. See deployment guide step 5.")

    if dropbox_dir.exists():
        PASS("Dropbox sync folder exists", str(dropbox_dir))
    else:
        WARN("Dropbox sync folder not found",
             f"Expected {dropbox_dir}. Start Dropbox daemon and link your account.")


def check_database():
    section("Database")
    db_path = Path("/opt/ragbase/data/ragbase.db")
    if not db_path.exists():
        WARN("Database file not found",
             "It will be created on first startup — this is normal before first run.")
        return

    VENV_PYTHON = "/opt/ragbase/venv/bin/python"
    test = subprocess.run(
        [VENV_PYTHON, "-c", f"""
import sys; sys.path.insert(0, '/opt/ragbase')
from dotenv import load_dotenv; load_dotenv('/opt/ragbase/.env')
from src.database import get_conn
with get_conn('{db_path}') as conn:
    docs   = conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
    chunks = conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    print(f'documents={{docs}} chunks={{chunks}}')
"""],
        capture_output=True, text=True
    )
    if test.returncode == 0:
        PASS("Database is readable", test.stdout.strip())
    else:
        FAIL("Database query failed",
             test.stderr.strip().split("\n")[-1] if test.stderr else "unknown")


def check_port_bindings():
    section("Port Availability")
    import socket
    for port, name in [(8000, "API"), (8001, "MCP")]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            PASS(f"Port {port} ({name}) is responding")
        else:
            WARN(f"Port {port} ({name}) not responding",
                 "Service may not be started yet — run: systemctl start ragbase-api ragbase-mcp")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print()
    print(BOLD("=" * 68))
    print(BOLD("  RAGBase Pre-Launch Checklist"))
    print(BOLD("=" * 68))
    print()
    print(YELLOW(BOLD(
        "  ⚠  Read this entire output before starting any services.\n"
        "     FAIL items will cause silent breakage or crashes.\n"
        "     WARN items will cause features to be unavailable."
    )))

    check_env_file()
    check_required_credentials()
    check_optional_credentials()
    check_system_binaries()
    check_python_packages()
    check_sqlite_vec()
    check_numpy_whisper_compat()
    check_whisper_model()
    check_directories()
    check_caddyfile()
    check_systemd_services()
    check_dropbox_daemon()
    check_database()
    check_port_bindings()

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r[0] == "PASS")
    warned = sum(1 for r in results if r[0] == "WARN")
    failed = sum(1 for r in results if r[0] == "FAIL")

    print()
    print(BOLD("=" * 68))
    print(f"  {GREEN(f'✓ {passed} passed')}   "
          f"{YELLOW(f'⚠ {warned} warnings')}   "
          f"{RED(f'✗ {failed} failed')}")
    print(BOLD("=" * 68))

    if failed > 0:
        print()
        print(RED(BOLD("  ✗ DO NOT start services — fix FAIL items above first.")))
        print()
        print(RED("  Failed checks:"))
        for status, label, detail in results:
            if status == "FAIL":
                print(f"    • {label}")
                if detail:
                    print(f"      {detail}")
        print()
        sys.exit(1)
    elif warned > 0:
        print()
        print(YELLOW("  ⚠ Services can start, but some features will be unavailable."))
        print(YELLOW("    Review warnings above before going to production."))
        print()
        sys.exit(0)
    else:
        print()
        print(GREEN(BOLD("  ✓ All checks passed. Safe to start services:")))
        print(f"      {BLUE('systemctl start ragbase-api ragbase-mcp caddy')}")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()