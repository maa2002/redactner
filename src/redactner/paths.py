"""Where RedactNER finds its files: in a source checkout and inside the packaged desktop application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "RedactNER"


def is_frozen() -> bool:
    """True inside the packaged application built with PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Folder that holds the data files: the unpacked bundle when packaged, the project root otherwise."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def default_model_dir() -> Path:
    """The fine-tuned checkpoint: ``REDACTNER_MODEL_DIR`` if set, otherwise ``models/redactner-best``."""
    override = os.environ.get("REDACTNER_MODEL_DIR")
    return Path(override) if override else bundle_root() / "models" / "redactner-best"


def user_data_dir() -> Path:
    """Per-user folder for the application's log files. Documents are never written here."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME
