"""Minimal runtime_root() for the skill-cache package.

When installed via `knarr skill install`, the provider directory is two
levels up from the skill folder (skills/<name>/session_infra_utils.py).
The cache DB lands at: <provider>/data/skill-runtime/skill-cache/cache.db

Override with SKILL_RUNTIME_ROOT env var for custom placement.
"""

from __future__ import annotations

import os
from pathlib import Path


def provider_root() -> Path:
    # Installed layout: <config_dir>/skills/skill-cache/session_infra_utils.py
    # parents[0] = skill-cache/, parents[1] = skills/, parents[2] = config_dir/
    return Path(__file__).resolve().parents[2]


def runtime_root() -> Path:
    configured = os.getenv("SKILL_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return (provider_root() / "data" / "skill-runtime").resolve()
