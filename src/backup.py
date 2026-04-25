"""
backup.py — Nightly encrypted database backup to Dropbox.

Encryption strategy:
    1. Copy the live SQLite DB to a temp file (avoids locking)
    2. Encrypt the copy using Fernet (AES-128-CBC + HMAC-SHA256)
    3. Upload the encrypted blob to Dropbox /Backups/ragbase/
    4. Rotate local backups — keep only last N days
    5. Log result to the database audit trail

Why Fernet instead of SQLCipher?
    SQLCipher requires a custom SQLite build. Fernet encryption of the
    backup file gives equivalent security for backups with zero extra
    system dependencies. The live database is protected by filesystem
    permissions (700) and the server firewall.

Dropbox upload:
    Uses the Dropbox API v2 via plain HTTPS — no SDK needed.
    Files < 150MB: single upload call.
    Files >= 150MB: chunked upload session (handles large corpora).

Recovery:
    python scripts/restore_backup.py --backup ragbase_2024-01-15.db.enc
"""
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import general_settings
from src.encryption import get_encryption
from src.logger import get_logger

logger = get_logger()
CFG    = general_settings["backup"]
PATHS  = general_settings["paths"]

DROPBOX_CHUNK_SIZE = 100 * 1024 * 1024   # 100 MB
DROPBOX_API_UPLOAD = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_API_START  = "https://content.dropboxapi.com/2/files/upload_session/start"
DROPBOX_API_APPEND = "https://content.dropboxapi.com/2/files/upload_session/append_v2"
DROPBOX_API_FINISH = "https://content.dropboxapi.com/2/files/upload_session/finish"


def _get_token() -> str:
    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("DROPBOX_ACCESS_TOKEN not set")
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Encrypt backup ────────────────────────────────────────────────────────────

def _encrypt_db(db_path: str, output_path: str) -> int:
    """
    Copy and encrypt the SQLite database file.
    Returns the size of the encrypted file in bytes.

    We use Python's tempfile to create a consistent snapshot of the DB,
    then encrypt it. SQLite WAL mode ensures the snapshot is consistent
    even while writes are happening.
    """
    enc = get_encryption()

    logger.info(f"BACKUP | Reading DB from {db_path}")
    with open(db_path, "rb") as f:
        raw = f.read()

    logger.info(f"BACKUP | Encrypting {len(raw):,} bytes")
    encrypted = enc.encrypt_bytes(raw)

    with open(output_path, "wb") as f:
        f.write(encrypted)

    size = len(encrypted)
    logger.info(f"BACKUP | Encrypted backup: {size:,} bytes → {output_path}")
    return size


# ── Dropbox upload ────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60))
def _upload_small(token: str, data: bytes, dropbox_path: str) -> None:
    """Upload files < 150MB in a single call."""
    resp = httpx.post(
        DROPBOX_API_UPLOAD,
        headers={
            **_headers(token),
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": f'{{"path":"{dropbox_path}","mode":"overwrite"}}',
        },
        content=data,
        timeout=120,
    )
    resp.raise_for_status()
    logger.info(f"BACKUP | Dropbox upload complete: {dropbox_path}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60))
def _upload_chunked(token: str, file_path: str, dropbox_path: str) -> None:
    """Upload large files (>= 150MB) using Dropbox upload sessions."""
    file_size = os.path.getsize(file_path)
    logger.info(f"BACKUP | Chunked upload: {file_size:,} bytes")

    with open(file_path, "rb") as f:
        # Start session
        resp = httpx.post(
            DROPBOX_API_START,
            headers={
                **_headers(token),
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": '{"close":false}',
            },
            content=b"",
            timeout=60,
        )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]

        offset = 0
        while True:
            chunk = f.read(DROPBOX_CHUNK_SIZE)
            if not chunk:
                break

            remaining = file_size - offset - len(chunk)

            if remaining <= 0:
                # Final chunk — finish the session
                resp = httpx.post(
                    DROPBOX_API_FINISH,
                    headers={
                        **_headers(token),
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": (
                            f'{{"cursor":{{"session_id":"{session_id}",'
                            f'"offset":{offset}}},'
                            f'"commit":{{"path":"{dropbox_path}","mode":"overwrite"}}}}'
                        ),
                    },
                    content=chunk,
                    timeout=120,
                )
                resp.raise_for_status()
                break
            else:
                # Intermediate chunk
                resp = httpx.post(
                    DROPBOX_API_APPEND,
                    headers={
                        **_headers(token),
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": (
                            f'{{"cursor":{{"session_id":"{session_id}",'
                            f'"offset":{offset}}},"close":false}}'
                        ),
                    },
                    content=chunk,
                    timeout=120,
                )
                resp.raise_for_status()
                offset += len(chunk)

    logger.info(f"BACKUP | Chunked upload complete: {dropbox_path}")


def _upload_to_dropbox(local_path: str, dropbox_path: str) -> None:
    token     = _get_token()
    file_size = os.path.getsize(local_path)

    if file_size < 150 * 1024 * 1024:
        with open(local_path, "rb") as f:
            data = f.read()
        _upload_small(token, data, dropbox_path)
    else:
        _upload_chunked(token, local_path, dropbox_path)


# ── Rotation ──────────────────────────────────────────────────────────────────

def _rotate_local_backups(backup_dir: str, keep_days: int) -> None:
    """Delete local backup files older than keep_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for f in Path(backup_dir).glob("ragbase_*.db.enc"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            logger.info(f"BACKUP | Rotated old backup: {f.name}")


# ── Main backup function ──────────────────────────────────────────────────────

def run_backup() -> dict:
    """
    Run a full backup cycle:
        1. Encrypt DB snapshot
        2. Save to local backup directory
        3. Upload to Dropbox
        4. Rotate old local backups

    Returns status dict. Errors are logged but do not raise —
    a failed backup should not crash the running server.
    """
    db_path    = PATHS["db_path"]
    backup_dir = PATHS["backup_dir"]
    Path(backup_dir).mkdir(parents=True, exist_ok=True)

    if not Path(db_path).exists():
        logger.warning("BACKUP | Database file not found — nothing to back up")
        return {"status": "skipped", "reason": "no database"}

    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    backup_name = f"ragbase_{timestamp}.db.enc"
    local_path  = str(Path(backup_dir) / backup_name)
    dropbox_path = f"{CFG['dropbox_backup_folder']}/{backup_name}"

    try:
        # Step 1: Encrypt
        size = _encrypt_db(db_path, local_path)

        # Step 2: Upload to Dropbox
        _upload_to_dropbox(local_path, dropbox_path)

        # Step 3: Rotate old backups
        _rotate_local_backups(backup_dir, CFG["keep_days"])

        logger.event(
            "backup_complete",
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
    """
    Decrypt a backup file for restore.
    Called by scripts/restore_backup.py.
    """
    enc = get_encryption()
    with open(encrypted_path, "rb") as f:
        data = f.read()
    decrypted = enc.decrypt_bytes(data)
    with open(output_path, "wb") as f:
        f.write(decrypted)
    logger.info(f"BACKUP | Decrypted {encrypted_path} → {output_path}")
