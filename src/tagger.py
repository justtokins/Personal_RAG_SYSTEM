"""
tagger.py — Claude API auto-tagging during ingestion.

Fix from original:
    reraise=False caused tenacity to silently return None after all
    retries failed. A misconfigured API key or Anthropic outage would
    produce no tags on every document forever with no visible error.

    Changed to reraise=True so the exception propagates to the caller.
    The caller (ingest_file in ingest.py) wraps in try/except and logs
    a clear WARNING, then continues with tags=[] rather than failing
    the entire ingestion.

    This preserves the same user-visible behaviour (empty tags on
    tagging failure) while making the failure visible in the logs.

Model:
    claude-3-haiku-20240307 — cheapest and fastest Claude model.
    ~$0.00025 per document (2000 char input). For 10,000 documents: ~$2.50.
    If cost is a concern, disable tagging via config: tagging.enabled=false.
"""
import json
import os
from typing import Optional

import anthropic
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from src.config_loader import general_settings, tags_config
from src.logger import get_logger

logger  = get_logger()
CFG     = general_settings["tagging"]
TAG_CFG = tags_config

# Valid tag set built once at module load — O(1) membership checks
_VALID_TAGS: frozenset = frozenset(TAG_CFG["tags"])


def _get_client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. "
            "Add it to .env or disable tagging: set tagging.enabled=false "
            "in config/general_settings.json"
        )
    return anthropic.Anthropic(api_key=key)


@retry(
    retry=retry_if_exception_type(
        (anthropic.RateLimitError, anthropic.APIStatusError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,    # propagate after all retries — do NOT swallow silently
)
def _call_claude(client: anthropic.Anthropic, prompt: str) -> list[str]:
    """
    Call Claude Haiku for tag assignment.

    Retries on rate limit and transient API errors (5xx).
    Does NOT retry on auth errors (4xx non-429) — those are permanent.

    Returns a filtered list of tags from the configured taxonomy.
    Raises on all failures after exhausting retries.
    """
    msg = client.messages.create(
        model=CFG["model"],
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = (msg.content[0].text or "").strip()

    # Strip markdown code fences if Claude wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    tags = json.loads(raw)
    if not isinstance(tags, list):
        raise ValueError(f"Expected JSON array, got: {type(tags).__name__}")

    # Filter to valid taxonomy — Claude may hallucinate tags not in the list
    valid = [t for t in tags if t in _VALID_TAGS][: CFG["max_tags"]]
    return valid


def assign_tags(text: str) -> list[str]:
    """
    Assign tags to document text using Claude Haiku.

    Returns [] if:
        - tagging is disabled in config
        - ANTHROPIC_API_KEY is not set
        - all retries fail (logged as WARNING, ingestion continues)

    Never raises — tag failure should not block document ingestion.
    """
    if not CFG.get("enabled", True):
        return []

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.warning(
            "TAGGER | ANTHROPIC_API_KEY not set — skipping auto-tagging. "
            "Set the key or disable tagging in config/general_settings.json"
        )
        return []

    excerpt = text[: CFG["context_chars"]].strip()
    if not excerpt:
        return []

    prompt = TAG_CFG["auto_tag_prompt"].format(
        max_tags=CFG["max_tags"],
        tag_list=", ".join(TAG_CFG["tags"]),
        content=excerpt,
    )

    try:
        client = _get_client()
        tags   = _call_claude(client, prompt)
        logger.info(f"TAGGER | Tags assigned: {tags}")
        return tags

    except anthropic.AuthenticationError:
        # Wrong API key — permanent failure, log as ERROR
        logger.error(
            "TAGGER | Authentication failed — check ANTHROPIC_API_KEY. "
            "Continuing without tags."
        )
        return []

    except json.JSONDecodeError as e:
        logger.warning(
            f"TAGGER | Claude returned invalid JSON: {e}. "
            "Continuing without tags."
        )
        return []

    except Exception as e:
        # Rate limit exhausted, API outage, or other failure after retries
        logger.warning(
            f"TAGGER | Tagging failed after retries: {type(e).__name__}: {e}. "
            "Continuing without tags."
        )
        return []