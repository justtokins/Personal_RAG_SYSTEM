"""
extractors/__init__.py — Route a file path to the correct extractor.

Each extractor returns:
    {
        "text":     str,          # full extracted text
        "title":    str | None,   # document title if detectable
        "chunks":   list[dict],   # pre-chunked with metadata
        "metadata": dict,         # file-type-specific metadata
        "word_count": int,
    }

A "chunk" dict has:
    {
        "content":        str,
        "chunk_index":    int,
        "page_number":    int | None,
        "timestamp_start": float | None,  # video only
        "timestamp_end":   float | None,  # video only
        "metadata":       dict,
    }
"""
from pathlib import Path
from typing import Optional

from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger()

_IMAGE_EXTS = set(general_settings["ingestion"]["image_extensions"])
_VIDEO_EXTS = set(general_settings["ingestion"]["video_extensions"])
_EMAIL_EXTS = set(general_settings["ingestion"]["email_extensions"])


def extract(file_path: str) -> dict:
    """
    Dispatch a file to the correct extractor based on extension.
    Returns a standardised extraction dict.
    Raises ValueError for unsupported file types.
    """
    path = Path(file_path)
    ext  = path.suffix.lower()

    logger.info(f"EXTRACT | {path.name} ({ext})")

    if ext == ".pdf":
        from src.extractors.pdf import extract_pdf
        return extract_pdf(file_path)

    if ext in {".docx", ".doc"}:
        from src.extractors.word import extract_word
        return extract_word(file_path)

    if ext in {".md", ".txt", ".markdown"}:
        from src.extractors.text import extract_text
        return extract_text(file_path)

    if ext in _EMAIL_EXTS:
        from src.extractors.email import extract_email
        return extract_email(file_path)

    if ext in _IMAGE_EXTS:
        from src.extractors.image import extract_image
        return extract_image(file_path)

    if ext in _VIDEO_EXTS:
        from src.extractors.video import extract_video
        return extract_video(file_path)

    raise ValueError(f"Unsupported file type: {ext}")


def is_supported(file_path: str) -> bool:
    ext = Path(file_path).suffix.lower()
    return ext in set(general_settings["ingestion"]["supported_extensions"])