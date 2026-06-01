"""Pipeline build metadata for deploy verification and correction_meta."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

# Bump when Step 4.3 correction behavior changes materially.
PIPELINE_CORRECTION_VERSION = "20260601-world-knowledge-phase2-v1"


@lru_cache(maxsize=1)
def get_pipeline_build_info() -> dict[str, str]:
    git_commit = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
        or os.environ.get("GIT_COMMIT", "").strip()
        or _git_head_short()
        or "unknown"
    )
    return {
        "pipeline_correction_version": PIPELINE_CORRECTION_VERSION,
        "git_commit": git_commit,
        "railway_environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip() or "local",
    }


def _git_head_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
