"""
embeddings.py — BGE-small-en-v1.5 batch embedding engine.

Model comparison (all run CPU-only on 4GB RAM):

Model                   | Dims | MTEB  | Speed (cpu) | RAM   | Recommendation
BAAI/bge-small-en-v1.5  | 384  | 62.2  | ~800 ch/s   | ~90MB | DEFAULT — best speed/quality ratio
BAAI/bge-base-en-v1.5   | 768  | 63.5  | ~300 ch/s   | ~440MB | Better quality, 3x slower
nomic-embed-text        | 768  | 62.4  | ~280 ch/s   | ~570MB | Longer context window (8192 tokens)
all-MiniLM-L6-v2        | 384  | 56.3  | ~1200 ch/s  | ~80MB  | Fastest, lower quality

BGE-small with normalize=True produces unit vectors.
Cosine distance = 1 - cosine_similarity.
For unit vectors: cosine_similarity = dot_product.
sqlite-vec stores L2 distance, which equals cosine distance for unit vectors.

To swap models: change embedding.model_name in general_settings.json
and run: python scripts/reindex.py
"""
from __future__ import annotations

import threading
from typing import Optional

from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger()

_MODEL_NAME  = general_settings["embedding"]["model_name"]
_BATCH_SIZE  = general_settings["embedding"]["batch_size"]
_DEVICE      = general_settings["embedding"]["device"]
_NORMALIZE   = general_settings["embedding"]["normalize"]
_DIMS        = general_settings["embedding"]["dimensions"]

# Thread-safe singleton — SentenceTransformer is not thread-safe during load
_model = None
_lock  = threading.Lock()


def _get_model():
    """Lazy-load the embedding model. Thread-safe."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                logger.info(f"EMBED | Loading model: {_MODEL_NAME}")
                _model = SentenceTransformer(_MODEL_NAME, device=_DEVICE)
                logger.info(f"EMBED | Model ready | dims={_DIMS}")
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of texts in optimised batches.

    Internally SentenceTransformer.encode():
    1. Sorts inputs by token length → minimises padding
    2. Processes in batches of batch_size
    3. Runs one transformer forward pass per batch
    4. Returns numpy array → we convert to Python list for sqlite-vec

    For BGE models, prepend the query/passage prefixes:
    - At query time: "Represent this sentence for searching relevant passages: {query}"
    - At passage time: no prefix needed (bge-small handles this)

    Returns list of float lists, one per input text.
    """
    if not texts:
        return []

    model = _get_model()

    embeddings = model.encode(
        texts,
        batch_size=_BATCH_SIZE,
        show_progress_bar=len(texts) > 200,
        convert_to_numpy=True,
        normalize_embeddings=_NORMALIZE,
        device=_DEVICE,
    )

    logger.debug(f"EMBED | {len(texts)} texts → shape {embeddings.shape}")
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """
    Embed a single search query.

    BGE models recommend a prefix for queries to improve retrieval.
    This is the standard BGE instruction prefix.
    """
    prefixed = f"Represent this sentence for searching relevant passages: {query}"
    return embed_texts([prefixed])[0]


def embed_chunks(chunks: list[str]) -> list[list[float]]:
    """Embed document chunks (no prefix needed for BGE passage encoding)."""
    return embed_texts(chunks)