"""
extractors/video.py — Whisper audio/video transcription.

ffmpeg fix:
    Original used .run(quiet=True) which silently swallowed all errors.
    A corrupted video, unsupported codec, or ffmpeg failure produced an
    empty or corrupt WAV that caused a cryptic numpy error in Whisper.

    Fixed to use .run(capture_output=True) and explicitly check for
    ffmpeg.Error, which carries the stderr output for clear diagnostics.
"""
import os
import tempfile
from pathlib import Path

from src.config_loader import general_settings
from src.logger import get_logger

logger      = get_logger()
WHISPER_CFG = general_settings["whisper"]
CHUNK_SZ    = general_settings["ingestion"]["chunk_size"]


def _extract_audio(video_path: str) -> str:
    """
    Extract audio track to a 16kHz mono WAV temp file.

    Returns the path to the temp file.
    Caller is responsible for deleting it (finally block in extract_video).

    Raises RuntimeError with ffmpeg stderr on failure.
    """
    import ffmpeg

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()

    try:
        out, err = (
            ffmpeg
            .input(video_path)
            .output(
                tmp.name,
                acodec="pcm_s16le",
                ac=1,
                ar="16000",
            )
            .overwrite_output()
            .run(capture_output=True)   # capture stdout and stderr
        )
    except ffmpeg.Error as e:
        # Clean up temp file on error
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        stderr = e.stderr.decode(errors="replace") if e.stderr else "(no stderr)"
        raise RuntimeError(
            f"ffmpeg failed to extract audio from {Path(video_path).name}: "
            f"{stderr}"
        )

    # Verify the output file has content
    if not os.path.exists(tmp.name) or os.path.getsize(tmp.name) == 0:
        os.unlink(tmp.name)
        raise RuntimeError(
            f"ffmpeg produced empty audio file for {Path(video_path).name}. "
            "The file may have no audio track or an unsupported format."
        )

    size_kb = os.path.getsize(tmp.name) // 1024
    logger.info(f"VIDEO | Audio extracted: {size_kb}KB WAV")
    return tmp.name


def _build_chunks(segments: list[dict]) -> list[dict]:
    """
    Group Whisper segments into chunks of ~CHUNK_SZ characters,
    preserving start/end timestamps per chunk.
    """
    chunks        = []
    chunk_index   = 0
    current_text  = ""
    current_start = 0.0
    current_end   = 0.0

    for seg in segments:
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue

        if not current_text:
            current_start = seg.get("start", 0.0)

        current_text += " " + seg_text
        current_end   = seg.get("end", 0.0)

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
                    "duration_sec":    round(current_end - current_start, 2),
                },
            })
            chunk_index  += 1
            current_text  = ""
            current_start = 0.0

    # Flush last partial chunk
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
                "duration_sec":    round(current_end - current_start, 2),
            },
        })

    return chunks


def extract_video(file_path: str) -> dict:
    """
    Transcribe audio/video using Whisper.

    Audio files (.mp3, .wav, .m4a): passed directly to Whisper.
    Video files: audio extracted via ffmpeg first, temp file deleted after.

    Raises on ffmpeg failure, Whisper failure, or empty transcription.
    The caller (ingest_file) catches all exceptions and marks the
    queue entry as failed with the error message.
    """
    import whisper

    path     = Path(file_path)
    is_audio = path.suffix.lower() in {".mp3", ".wav", ".m4a"}

    logger.info(
        f"VIDEO | Loading Whisper '{WHISPER_CFG['model']}' model "
        f"(download_root={general_settings['paths']['whisper_cache']})"
    )

    try:
        model = whisper.load_model(
            WHISPER_CFG["model"],
            device=WHISPER_CFG["device"],
            download_root=general_settings["paths"]["whisper_cache"],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to load Whisper model: {e}") from e

    audio_path = file_path
    temp_audio = None

    if not is_audio:
        logger.info(f"VIDEO | Extracting audio from {path.name}")
        temp_audio = _extract_audio(file_path)
        audio_path = temp_audio

    try:
        logger.info(f"VIDEO | Transcribing {path.name} ...")
        result = model.transcribe(
            audio_path,
            language=WHISPER_CFG.get("language"),   # None = auto-detect
            verbose=False,
            word_timestamps=False,
        )
    except Exception as e:
        raise RuntimeError(
            f"Whisper transcription failed for {path.name}: {e}"
        ) from e
    finally:
        if temp_audio and os.path.exists(temp_audio):
            os.unlink(temp_audio)
            logger.debug(f"VIDEO | Temp audio file deleted: {temp_audio}")

    segments  = result.get("segments", [])
    full_text = (result.get("text") or "").strip()

    if not full_text:
        raise ValueError(
            f"Whisper produced no transcript for {path.name}. "
            "The audio may be silent, corrupted, or in an unsupported language."
        )

    chunks         = _build_chunks(segments)
    total_duration = segments[-1]["end"] if segments else 0.0

    logger.info(
        f"VIDEO | {path.name} | {len(segments)} segments | "
        f"{len(chunks)} chunks | {total_duration:.0f}s | "
        f"lang={result.get('language','?')}"
    )

    return {
        "text":       full_text,
        "title":      path.stem.replace("_", " ").replace("-", " ").title(),
        "chunks":     chunks,
        "word_count": len(full_text.split()),
        "metadata": {
            "extractor":     "whisper",
            "whisper_model": WHISPER_CFG["model"],
            "duration_sec":  round(total_duration, 2),
            "language":      result.get("language", "unknown"),
            "segment_count": len(segments),
        },
    }