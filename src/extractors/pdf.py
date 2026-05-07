"""
extractors/pdf.py — PDF text extraction using PyMuPDF (fitz).

PyMuPDF is significantly faster than pdfminer or PyPDF2 and handles
complex layouts (multi-column, tables) better.

Chunking strategy for PDFs:
    - Try to keep chunks within page boundaries where possible
    - If a page is longer than chunk_size, split on paragraph then sentence
    - Track page numbers in chunk metadata for citations
"""
import re
from pathlib import Path

import fitz  # PyMuPDF

from src.config_loader import general_settings
from src.logger import get_logger

logger    = get_logger()
CHUNK_SZ  = general_settings["ingestion"]["chunk_size"]
OVERLAP   = general_settings["ingestion"]["chunk_overlap"]


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries."""
    paragraphs = re.split(r"\n\n+", text.strip())
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if not para.strip():
            continue
        if len(current) + len(para) <= chunk_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            current = ""   # FIX: reset before sentence loop or overlap calculation
            # If paragraph itself is too large, split on sentence
            if len(para) > chunk_size:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                for sent in sentences:
                    if len(current) + len(sent) <= chunk_size:
                        current = (current + " " + sent).strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = sent
            else:
                # Start new chunk with overlap from previous
                if chunks:
                    # Take last `overlap` chars of previous chunk as context
                    tail    = chunks[-1][-overlap:] if len(chunks[-1]) > overlap else chunks[-1]
                    current = (tail + "\n\n" + para).strip()
                else:
                    current = para

    if current:
        chunks.append(current)

    return [c for c in chunks if len(c.strip()) > 50]


def extract_pdf(file_path: str) -> dict:
    path = Path(file_path)
    doc  = fitz.open(str(path))

    full_text  = []
    page_texts = []

    try:
        for page_num, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if text.strip():
                full_text.append(text)
                page_texts.append((page_num, text))
    finally:
        doc.close()   # FIX: always close even if extraction raises

    # Build chunks with page number metadata
    all_chunks  = []
    chunk_index = 0

    for page_num, page_text in page_texts:
        page_chunks = _chunk_text(page_text, CHUNK_SZ, OVERLAP)
        for c in page_chunks:
            all_chunks.append({
                "content":         c,
                "chunk_index":     chunk_index,
                "page_number":     page_num,
                "timestamp_start": None,
                "timestamp_end":   None,
                "metadata":        {"source_page": page_num},
            })
            chunk_index += 1

    combined = "\n\n".join(full_text)
    title    = path.stem.replace("_", " ").replace("-", " ").title()

    logger.info(
        f"PDF | {path.name} | {len(page_texts)} pages | {len(all_chunks)} chunks"
    )

    return {
        "text":       combined,
        "title":      title,
        "chunks":     all_chunks,
        "word_count": len(combined.split()),
        "metadata":   {"pages": len(page_texts), "extractor": "pymupdf"},
    }