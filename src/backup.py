"""
backup.py — Nightly encrypted backup to Dropbox.

Dropbox auth fix:
    Dropbox deprecated long-lived access tokens in September 2021.
    New apps must use short-lived tokens with offline access + refresh tokens.

    Setup (one-time, done locally):
        1. Create a Dropbox app at https://www.dropbox.com/developers
           - Type: "Scoped access" → "Full Dropbox"
           - Permissions: files.content.write, files.content.read
        2. Get your APP_KEY and APP_SECRET from the app console
        3. Run: python scripts/dropbox_auth.py
           (prints a URL, you click it, paste the code back)
           This writes DROPBOX_REFRESH_TOKEN to stdout — add to .env
        4. Set in .env:
           DROPBOX_APP_KEY=...
           DROPBOX_APP_SECRET=...
           DROPBOX_REFRESH_TOKEN=...

    The dropbox SDK handles token refresh automatically using the
    refresh token + app credentials. No manual token rotation needed.

Required: pip install dropbox  (added to requirements.txt)
"""
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import general_settings
from src.encryption import get_encryption
from src.logger import get_logger

logger = get_logger()
CFG    = general_settings["backup"]
PATHS  = general_settings["paths"]


# ── Dropbox client ────────────────────────────────────────────────────────────

def _get_dbx():
    """
    Create an authenticated Dropbox client using refresh token flow.

    Raises RuntimeError with clear instructions if credentials missing.
    """
    try:
        import dropbox
    except ImportError:
        raise RuntimeError(
            "dropbox package not installed. Run: pip install dropbox"
        )

    app_key     = os.getenv("DROPBOX_APP_KEY")
    app_secret  = os.getenv("DROPBOX_APP_SECRET")
    refresh_tok = os.getenv("DROPBOX_REFRESH_TOKEN")

    if not all([app_key, app_secret, refresh_tok]):
        missing = [
            k for k, v in {
                "DROPBOX_APP_KEY":      app_key,
                "DROPBOX_APP_SECRET":   app_secret,
                "DROPBOX_REFRESH_TOKEN": refresh_tok,
            }.items() if not v
        ]
        raise RuntimeError(
            f"Missing Dropbox credentials: {missing}. "
            "Run scripts/dropbox_auth.py to generate them."
        )

    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_tok,
        app_key=app_key,
        app_secret=app_secret,
    )


# ── Encrypt backup ────────────────────────────────────────────────────────────

def _encrypt_db(db_path: str, output_path: str) -> int:
    """
    Read the live DB and write an encrypted copy.

    SQLite WAL mode ensures a consistent read even during active writes.
    Returns encrypted file size in bytes.
    """
    enc = get_encryption()
    logger.info(f"BACKUP | Reading {db_path}")

    with open(db_path, "rb") as f:
        raw = f.read()

    logger.info(f"BACKUP | Encrypting {len(raw):,} bytes")
    encrypted = enc.encrypt_bytes(raw)

    with open(output_path, "wb") as f:
        f.write(encrypted)

    logger.info(f"BACKUP | Encrypted → {output_path} ({len(encrypted):,} bytes)")
    return len(encrypted)


# ── Upload to Dropbox ─────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60))
def _upload(local_path: str, dropbox_path: str) -> None:
    """
    Upload file to Dropbox using the official SDK.

    Files < 150MB: single upload_session.
    Files ≥ 150MB: chunked upload_session_start/append/finish.
    The SDK handles auth token refresh automatically.
    """
    import dropbox

    dbx       = _get_dbx()
    file_size = Path(local_path).stat().st_size
    CHUNK     = 100 * 1024 * 1024   # 100MB chunks

    logger.info(
        f"BACKUP | Uploading {Path(local_path).name} "
        f"({file_size // 1024:,}KB) → Dropbox:{dropbox_path}"
    )

    with open(local_path, "rb") as f:
        if file_size <= CHUNK:
            dbx.files_upload(
                f.read(),
                dropbox_path,
                mode=dropbox.files.WriteMode("overwrite"),
            )
        else:
            # Chunked upload
            session = dbx.files_upload_session_start(f.read(CHUNK))
            cursor  = dropbox.files.UploadSessionCursor(
                session_id=session.session_id,
                offset=f.tell(),
            )
            commit = dropbox.files.CommitInfo(
                path=dropbox_path,
                mode=dropbox.files.WriteMode("overwrite"),
            )
            while f.tell() < file_size:
                remaining = file_size - f.tell()
                if remaining <= CHUNK:
                    dbx.files_upload_session_finish(f.read(CHUNK), cursor, commit)
                else:
                    dbx.files_upload_session_append_v2(f.read(CHUNK), cursor)
                    cursor = dropbox.files.UploadSessionCursor(
                        session_id=session.session_id,
                        offset=f.tell(),
                    )

    logger.info(f"BACKUP | Upload complete → {dropbox_path}")


# ── Rotation ──────────────────────────────────────────────────────────────────

def _rotate_local(backup_dir: str, keep_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for f in Path(backup_dir).glob("ragbase_*.db.enc"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            logger.info(f"BACKUP | Rotated {f.name}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_backup() -> dict:
    """
    Full backup cycle:
        1. Encrypt DB snapshot
        2. Save locally
        3. Upload to Dropbox
        4. Rotate old local backups

    Never raises — errors are logged and returned in status dict
    so a failed backup does not crash the running server.
    """
    db_path    = PATHS["db_path"]
    backup_dir = PATHS["backup_dir"]
    Path(backup_dir).mkdir(parents=True, exist_ok=True)

    if not Path(db_path).exists():
        logger.warning("BACKUP | Database not found — nothing to back up")
        return {"status": "skipped", "reason": "no database"}

    timestamp    = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    backup_name  = f"ragbase_{timestamp}.db.enc"
    local_path   = str(Path(backup_dir) / backup_name)
    dropbox_path = f"{CFG['dropbox_backup_folder']}/{backup_name}"

    try:
        size = _encrypt_db(db_path, local_path)
        _upload(local_path, dropbox_path)
        _rotate_local(backup_dir, CFG["keep_days"])

        logger.event(
            "backup_complete",
            actor="scheduler",
            file=backup_name,
            size_bytes=size,
            dropbox=dropbox_path,
        )
        return {
            "status":       "complete",
            "backup_file":  backup_name,
            "size_bytes":   size,
            "dropbox_path": dropbox_path,
        }

    except Exception as e:
        logger.error(f"BACKUP | Failed: {e}")
        return {"status": "failed", "error": str(e)}


def decrypt_backup(encrypted_path: str, output_path: str) -> None:
    """Decrypt a backup for restore. Called by scripts/restore_backup.py."""
    enc = get_encryption()
    with open(encrypted_path, "rb") as f:
        data = f.read()
    decrypted = enc.decrypt_bytes(data)
    with open(output_path, "wb") as f:
        f.write(decrypted)
    logger.info(f"BACKUP | Decrypted {encrypted_path} → {output_path}")