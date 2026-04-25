"""
tagger.py — Claude API auto-tagging during ingestion.

Calls claude-3-haiku (cheapest, fast) with the first 2000 chars
of extracted text. Returns a list of tags from the configured taxonomy.

Retries on rate limits with exponential backoff.
If tagging fails, ingestion continues — tags default to [].
"""
import json
import os
from typing import Optional

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import general_settings, tags_config
from src.logger import get_logger

logger  = get_logger()
CFG     = general_settings["tagging"]
TAG_CFG = tags_config


def _get_client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=False,
)
def _call_claude(client: anthropic.Anthropic, prompt: str) -> Optional[list[str]]:
    """Call Claude with retry. Returns tag list or None on failure."""
    try:
        msg = client.messages.create(
            model=CFG["model"],
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        tags = json.loads(raw)
        if isinstance(tags, list):
            # Filter to only valid tags from the taxonomy
            valid = set(TAG_CFG["tags"])
            return [t for t in tags if t in valid][: CFG["max_tags"]]
        return []
    except Exception as e:
        logger.warning(f"TAGGER | Claude call failed: {e}")
        return None


def assign_tags(text: str) -> list[str]:
    """
    Assign up to max_tags from the configured taxonomy to a document.
    Returns [] if tagging is disabled or fails.
    """
    if not CFG.get("enabled", True):
        return []

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.warning("TAGGER | ANTHROPIC_API_KEY not set — skipping tagging")
        return []

    excerpt = text[: CFG["context_chars"]]
    prompt  = TAG_CFG["auto_tag_prompt"].format(
        max_tags=CFG["max_tags"],
        tag_list=", ".join(TAG_CFG["tags"]),
        content=excerpt,
    )

    try:
        client = _get_client()
        tags   = _call_claude(client, prompt)
        result = tags or []
        logger.info(f"TAGGER | Tags assigned: {result}")
        return result
    except Exception as e:
        logger.error(f"TAGGER | Tagging failed: {e} — continuing without tags")
        return []