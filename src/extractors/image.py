"""
extractors/image.py — Tesseract OCR for images.

Preprocessing pipeline for better OCR accuracy:
    1. Convert to greyscale
    2. Upscale if small (Tesseract works best at 300+ DPI)
    3. Apply slight sharpening
    4. Run Tesseract with --oem 3 (LSTM) --psm 3 (auto layout)

OCR quality depends heavily on image quality.
For scanned documents, consider --psm 1 (orientation detection).
"""
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

from src.config_loader import general_settings
from src.logger import get_logger
from src.extractors.pdf import _chunk_text

logger   = get_logger()
CHUNK_SZ = general_settings["ingestion"]["chunk_size"]
OVERLAP  = general_settings["ingestion"]["chunk_overlap"]

TESSERACT_CONFIG = "--oem 3 --psm 3"
MIN_DPI_WIDTH    = 1800  # minimum pixel width for good OCR


def _preprocess(img: Image.Image) -> Image.Image:
    """Preprocess image for better OCR accuracy."""
    # Convert to RGB if necessary (handles RGBA, palette modes)
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")

    # Upscale small images
    if img.width < MIN_DPI_WIDTH:
        scale = MIN_DPI_WIDTH / img.width
        img   = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )

    # Greyscale + sharpen
    img = img.convert("L")
    img = img.filter(ImageFilter.SHARPEN)

    return img


def extract_image(file_path: str) -> dict:
    path = Path(file_path)
    img  = Image.open(str(path))

    original_size = img.size
    img           = _preprocess(img)
    text          = pytesseract.image_to_string(img, config=TESSERACT_CONFIG)

    if not text.strip():
        logger.warning(f"IMAGE | No text detected in {path.name}")
        text = "(No readable text detected in this image)"

    raw_chunks = _chunk_text(text, CHUNK_SZ, OVERLAP)
    all_chunks = [
        {
            "content":         c,
            "chunk_index":     i,
            "page_number":     None,
            "timestamp_start": None,
            "timestamp_end":   None,
            "metadata":        {"ocr": True, "original_size": list(original_size)},
        }
        for i, c in enumerate(raw_chunks)
    ]

    title = path.stem.replace("_", " ").replace("-", " ").title()
    logger.info(f"IMAGE | {path.name} | {len(text.split())} words | {len(all_chunks)} chunks")

    return {
        "text":       text,
        "title":      title,
        "chunks":     all_chunks,
        "word_count": len(text.split()),
        "metadata":   {
            "extractor":     "tesseract",
            "original_size": list(original_size),
            "ocr":           True,
        },
    }