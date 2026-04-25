"""
extractors/video.py — Whisper audio/video transcription.

Performance notes (medium model, CPU):
    1-hour video ≈ 12-18 minutes on a single core
    Whisper is compute-bound — cannot be parallelised easily per file
    but we run it in a background thread so the API stays responsive.

Chunking strategy for video:
    Whisper returns segments with timestamps.
    We chunk by grouping segments until chunk_size chars,
    preserving timestamp_start and timestamp_end for each chunk.
    This enables get_video_segment() in the MCP server to seek
    to the exact point in the video.

Audio extraction:
    ffmpeg extracts audio to a temp WAV file.
    WAV is uncompressed — faster for Whisper than MP3 decoding.
"""
import os
import tempfile
from pathlib import Path

from src.config_loader import general_settings
from src.logger import get_logger

logger       = get_logger()
CHUNK_SZ     = general_settings["ingestion"]["chunk_size"]
WHISPER_CFG  = general_settings["whisper"]


def _extract_audio(video_path: str) -> str:
    """Extract audio to a temp WAV file using ffmpeg. Returns temp path."""
    import ffmpeg

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()

    (
        ffmpeg
        .input(video_path)
        .output(tmp.name, acodec="pcm_s16le", ac=1, ar="16000")
        .overwrite_output()
        .run(quiet=True)
    )
    return tmp.name


def _build_chunks_from_segments(segments: list[dict]) -> list[dict]:
    """
    Group Whisper segments into chunks of ~CHUNK_SZ characters.
    Each chunk carries the start/end timestamp of its constituent segments.
    """
    chunks      = []
    chunk_index = 0
    current_text  = ""
    current_start = 0.0
    current_end   = 0.0

    for seg in segments:
        seg_text = seg["text"].strip()
        if not seg_text:
            continue

        if not current_text:
            current_start = seg["start"]

        current_text  += " " + seg_text
        current_end    = seg["end"]

        if len(current_text) >= CHUNK_SZ:
            chunks.append({
                "content":         current_text.strip(),
                "chunk_index":     chunk_index,
                "page_number":     None,
                "timestamp_start": current_start,
                "timestamp_end":   current_end,
                "metadata": {
                    "timestamp_start": current_start,
                    "timestamp_end":   current_end,
                    "duration":        round(current_end - current_start, 2),
                },
            })
            chunk_index  += 1
            current_text  = ""
            current_start = 0.0

    # Flush remaining
    if current_text.strip():
        chunks.append({
            "content":         current_text.strip(),
            "chunk_index":     chunk_index,
            "page_number":     None,
            "timestamp_start": current_start,
            "timestamp_end":   current_end,
            "metadata": {
                "timestamp_start": current_start,
                "timestamp_end":   current_end,
            },
        })

    return chunks


def extract_video(file_path: str) -> dict:
    import whisper

    path     = Path(file_path)
    is_audio = path.suffix.lower() in {".mp3", ".wav", ".m4a"}

    logger.info(
        f"VIDEO | {path.name} | Loading Whisper {WHISPER_CFG['model']} ..."
    )
    model = whisper.load_model(
        WHISPER_CFG["model"],
        device=WHISPER_CFG["device"],
        download_root=general_settings["paths"]["whisper_cache"],
    )

    # Extract audio if this is a video file
    audio_path = file_path
    temp_audio  = None

    if not is_audio:
        logger.info(f"VIDEO | Extracting audio from {path.name} ...")
        temp_audio  = _extract_audio(file_path)
        audio_path  = temp_audio

    try:
        logger.info(f"VIDEO | Transcribing {path.name} ...")
        result = model.transcribe(
            audio_path,
            language=WHISPER_CFG.get("language"),
            verbose=False,
            word_timestamps=False,
        )
    finally:
        if temp_audio and os.path.exists(temp_audio):
            os.unlink(temp_audio)

    segments  = result.get("segments", [])
    full_text = result.get("text", "").strip()
    chunks    = _build_chunks_from_segments(segments)

    total_duration = segments[-1]["end"] if segments else 0

    logger.info(
        f"VIDEO | {path.name} | {len(segments)} segments | "
        f"{len(chunks)} chunks | duration={total_duration:.0f}s"
    )

    return {
        "text":       full_text,
        "title":      path.stem.replace("_", " ").replace("-", " ").title(),
        "chunks":     chunks,
        "word_count": len(full_text.split()),
        "metadata":   {
            "extractor":      "whisper",
            "whisper_model":  WHISPER_CFG["model"],
            "duration_sec":   round(total_duration, 2),
            "language":       result.get("language", "unknown"),
            "segment_count":  len(segments),
        },
    }