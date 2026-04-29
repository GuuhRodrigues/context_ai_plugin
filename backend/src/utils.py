from __future__ import annotations

import time
from typing import Any, Optional

from backend.configs import API_BASE_URL, DEFAULT_BACKFILL_SECONDS, MAX_BACKFILL_SECONDS


def websocket_base_url() -> str:
    """Derive WebSocket base URL from API_BASE_URL (http->ws, https->wss)."""
    if API_BASE_URL.startswith("https://"):
        return API_BASE_URL.replace("https://", "wss://", 1)
    return API_BASE_URL.replace("http://", "ws://", 1)


def event_websocket_url(base_url: str, meeting_id: str, token: str) -> str:
    return f"{base_url}/event/{meeting_id}?token={token}&backfill={DEFAULT_BACKFILL_SECONDS}"


def audio_websocket_url(base_url: str, meeting_id: str, token: str) -> str:
    return f"{base_url}/audio/{meeting_id}?token={token}"


def decode_if_bytes(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def stream_min_id(backfill_seconds: int) -> str:
    now_ms = int(time.time() * 1000)
    min_ms = max(0, now_ms - backfill_seconds * 1000)
    return f"{min_ms}-0"


def parse_backfill_seconds(raw_value: Optional[str]) -> int:
    if raw_value is None:
        return DEFAULT_BACKFILL_SECONDS

    try:
        seconds = int(raw_value)
    except Exception:
        seconds = DEFAULT_BACKFILL_SECONDS

    if seconds < 0:
        return 0
    if seconds > MAX_BACKFILL_SECONDS:
        return MAX_BACKFILL_SECONDS
    return seconds


def get_payload_from_fields(fields: Any) -> Optional[str]:
    if not isinstance(fields, dict):
        return None

    if "payload" in fields:
        return decode_if_bytes(fields.get("payload"))
    if b"payload" in fields:
        return decode_if_bytes(fields.get(b"payload"))

    for key in ("data", "event"):
        if key in fields:
            return decode_if_bytes(fields.get(key))
        if key.encode() in fields:
            return decode_if_bytes(fields.get(key.encode()))
    return None
