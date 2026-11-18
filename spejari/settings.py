"""Tiny per-user settings file: remembers GUI choices (theme, drive mode, privacy, auto-upload) between runs.

Precedence everywhere: a command-line flag > the remembered choice > config/spejari.toml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .config import PROJECT_ROOT
from .exporter import _chown_to_sudo_user

SETTINGS_PATH = PROJECT_ROOT / "config" / ".spejari_user.json"


def _read() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(key: str, default: Any = None) -> Any:
    return _read().get(key, default)


def save(key: str, value: Any) -> bool:
    data = _read()
    data[key] = value
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        _chown_to_sudo_user(SETTINGS_PATH)
        return True
    except OSError:
        return False


def load_saved_theme(path: Optional[Path] = None) -> Optional[str]:
    global SETTINGS_PATH
    if path is not None:
        SETTINGS_PATH = path
    theme = _read().get("theme")
    return str(theme) if theme else None


def save_theme(key: str) -> bool:
    return save("theme", key)
