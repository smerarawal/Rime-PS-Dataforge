"""Robust unique identifiers. Sequence numbers are debug-only."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Return a collision-resistant id such as ``req_a1b2c3d4e5f6``."""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"
