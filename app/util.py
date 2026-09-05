"""Shared helpers — small, testable utilities for API/path safety."""
from __future__ import annotations

import re

from app.schemas import Region

VALID_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def is_safe_run_id(run_id: str) -> bool:
    """Reject path traversal / odd characters in URL path segments (review #20)."""
    return bool(VALID_RUN_ID.fullmatch(run_id or ""))


def parse_region(value) -> Region | None:
    """Parse a Region enum or raise ValueError (review #19: consistent 422s)."""
    if value is None or str(value).strip() == "":
        return None
    return Region(str(value).strip().upper())
