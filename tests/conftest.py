"""Shared pytest configuration — .env-driven paths for integration tests."""

import os
from pathlib import Path

import pytest


# ── .env file loader ────────────────────────────────────────────────
# Integration tests need local BG3 data paths. Rather than requiring
# OS-level environment variables, paths are read from a .env file in the
# lsfx-converter root.  Existing env vars still take priority (for CI).

def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from .env if it exists, without overriding existing env vars."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# ── Environment-driven paths ────────────────────────────────────────

LSFX_DIR = os.environ.get("BG3_LSFX_DIR", "")
LSEFX_DIR = os.environ.get("BG3_LSEFX_DIR", "")

# XCD/XMD are derived from the game install directory.
_ALLSPARK_REL = Path("Data") / "Editor" / "Config" / "AllSpark"
_GAME_DIR = os.environ.get("BG3_GAME_DIR", "")
XCD_PATH = str(Path(_GAME_DIR) / _ALLSPARK_REL / "ComponentDefinition.xcd") if _GAME_DIR else ""
XMD_PATH = str(Path(_GAME_DIR) / _ALLSPARK_REL / "ModuleDefinition.xmd") if _GAME_DIR else ""


def has_game_data() -> bool:
    """Return True if the local BG3 data files exist."""
    return bool(LSFX_DIR) and os.path.isdir(LSFX_DIR) and bool(XCD_PATH) and os.path.isfile(XCD_PATH)


requires_game_data = pytest.mark.skipif(
    not has_game_data(),
    reason="BG3 game data not available (configure paths in .env — see .env.example)",
)
