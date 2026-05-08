"""SoulStore OpenAI-compatible LLM gateway request headers."""

from __future__ import annotations

import os


def soulstore_default_headers(base_url: str) -> dict[str, str] | None:
    """Return SoulStore-specific request headers when the target matches.

    Requires ``SOULSTORE_INSTANCE_ID``. When ``SOULSTORE_BASE_URL`` is set, it
    must match ``base_url`` after trimming trailing slashes. Otherwise, a loose
    ``"soulstore"`` substring heuristic is used so hand-configured endpoints
    still work without duplicating the URL in env.
    """
    instance_id = (os.getenv("SOULSTORE_INSTANCE_ID") or "").strip()
    if not instance_id:
        return None

    current = (base_url or "").strip().rstrip("/")
    env_base = (os.getenv("SOULSTORE_BASE_URL") or "").strip().rstrip("/")
    if env_base:
        if current != env_base:
            return None
    elif "soulstore" not in current.lower():
        return None

    source_type = (os.getenv("SOULSTORE_SOURCE_TYPE") or "openclaw").strip() or "openclaw"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-SoulStore-Source-Type": source_type,
        "X-SoulStore-Instance-Id": instance_id,
    }
    api_key = (os.getenv("SOULSTORE_KEY") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
