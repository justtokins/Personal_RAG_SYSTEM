"""
config_loader.py — Load and validate all JSON configuration files.
"""
import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent


def _load(filename: str) -> dict[str, Any]:
    path = BASE_DIR / "config" / filename
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


general_settings: dict = _load("general_settings.json")
tags_config:      dict = _load("tags.json")