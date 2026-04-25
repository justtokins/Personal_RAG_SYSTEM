"""
extractors/email.py — .msg (Outlook) and .eml email extraction.

.msg files: extract-msg library
.eml files: Python standard library email module
"""
from pathlib import Path
import email as stdlib_email
from email import policy

from src.config_loader import general_settings
from src.logger import get_logger
from src.extractors.pdf import _chunk_text

logger   = get_logger()
CHUNK_SZ = general_settings["ingestion"]["chunk_size"]
OVERLAP  = general_settings["ingestion"]["chunk_overlap"]


def _build_chunks(text: str, metadata: dict) -> list[dict]:
    raw = _chunk_text(text, CHUNK_SZ, OVERLAP)
    return [
        {
            "content":         c,
            "chunk_index":     i,
            "page_number":     None,
            "timestamp_start": None,
            "timestamp_end":   None,
            "metadata":        metadata,
        }
        for i, c in enumerate(raw)
    ]


def extract_email(file_path: str) -> dict:
    path = Path(file_path)
    ext  = path.suffix.lower()

    if ext == ".msg":
        return _extract_msg(file_path)
    return _extract_eml(file_path)


def _extract_msg(file_path: str) -> dict:
    import extract_msg
    path = Path(file_path)
    msg  = extract_msg.Message(str(path))

    subject = msg.subject or "(no subject)"
    sender  = msg.sender or ""
    date    = str(msg.date) if msg.date else ""
    body    = msg.body or ""

    header = f"Subject: {subject}\nFrom: {sender}\nDate: {date}\n\n"
    text   = header + body

    metadata = {
        "subject":   subject,
        "sender":    sender,
        "date":      date,
        "extractor": "extract-msg",
    }

    logger.info(f"EMAIL | .msg | {path.name} | subject='{subject}'")
    return {
        "text":       text,
        "title":      subject,
        "chunks":     _build_chunks(text, metadata),
        "word_count": len(text.split()),
        "metadata":   metadata,
    }


def _extract_eml(file_path: str) -> dict:
    path = Path(file_path)
    raw  = path.read_bytes()
    msg  = stdlib_email.message_from_bytes(raw, policy=policy.default)

    subject = str(msg.get("subject", "(no subject)"))
    sender  = str(msg.get("from", ""))
    date    = str(msg.get("date", ""))

    # Extract plain text body
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body += part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
    else:
        body = msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="replace"
        )

    header = f"Subject: {subject}\nFrom: {sender}\nDate: {date}\n\n"
    text   = header + body

    metadata = {
        "subject":   subject,
        "sender":    sender,
        "date":      date,
        "extractor": "stdlib-email",
    }

    logger.info(f"EMAIL | .eml | {path.name} | subject='{subject}'")
    return {
        "text":       text,
        "title":      subject,
        "chunks":     _build_chunks(text, metadata),
        "word_count": len(text.split()),
        "metadata":   metadata,
    }