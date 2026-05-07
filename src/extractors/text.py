"""
extractors/text.py — Plain text and Markdown extraction.
"""
import re
from pathlib import Path

from src.config_loader import general_settings
from src.logger import get_logger
from src.extractors.pdf import _chunk_text

logger   = get_logger()
CHUNK_SZ = general_settings["ingestion"]["chunk_size"]
OVERLAP  = general_settings["ingestion"]["chunk_overlap"]


def extract_text(file_path: str) -> dict:
    path = Path(file_path)
    text = path.read_text(encoding="utf-8", errors="replace")

    # For markdown, strip syntax markers for embedding
    # but keep the text — headings are semantic signal
    clean = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # headings
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)        # links → text
    clean = re.sub(r"`{1,3}[^`]*`{1,3}", "", clean)               # code spans
    clean = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", clean)   # bold/italic

    raw_chunks = _chunk_text(clean, CHUNK_SZ, OVERLAP)
    all_chunks = [
        {
            "content":         c,
            "chunk_index":     i,
            "page_number":     None,
            "timestamp_start": None,
            "timestamp_end":   None,
            "metadata":        {},
        }
        for i, c in enumerate(raw_chunks)
    ]

    # Use first non-empty line as title
    first_line = next(
        (l.strip().lstrip("# ") for l in text.splitlines() if l.strip()), None
    )
    title = first_line or path.stem.replace("_", " ").title()

    logger.info(f"TEXT | {path.name} | {len(all_chunks)} chunks")
    return {
        "text":       clean,
        "title":      title,
        "chunks":     all_chunks,
        "word_count": len(clean.split()),
        "metadata":   {"extractor": "text", "original_ext": path.suffix},
    }