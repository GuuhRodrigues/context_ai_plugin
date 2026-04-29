from __future__ import annotations

import json
import time
from typing import Any, Optional, Set

import redis

from backend.configs import EVENTS_STREAM_MAXLEN, REDIS_HOST, REDIS_PORT
from backend.src.utils import decode_if_bytes


def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def redis_client_bytes() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


def now_ts() -> int:
    return int(time.time())


def parse_meeting_id_from_audio_key(key: str) -> Optional[str]:
    if not key.startswith("audio:"):
        return None
    return key.split("audio:", 1)[1] or None


def events_stream_key(meeting_id: str) -> str:
    return f"events:{meeting_id}"


def transcript_channel(meeting_id: str) -> str:
    return f"transcript:{meeting_id}"


def transcript_pattern() -> str:
    return "transcript:*"


def insights_channel(meeting_id: str) -> str:
    return f"insights:{meeting_id}"


def audio_stream_key(meeting_id: str) -> str:
    return f"audio:{meeting_id}"


def meeting_closed_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:closed"


def meeting_token_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:token"


def observability_counters_key(meeting_id: str) -> str:
    return f"observability:{meeting_id}:counters"


def dedup_key_pattern(meeting_id: str) -> str:
    return f"dedup:{meeting_id}:*"


def redis_dedup_key(meeting_id: str, prefix: str, value_hash: str) -> str:
    return f"dedup:{meeting_id}:{prefix}:{value_hash}"


def is_meeting_closed(r: redis.Redis, meeting_id: str) -> bool:
    try:
        return bool(r.get(meeting_closed_key(meeting_id)))
    except Exception:
        return False


def add_event_to_stream(
    r: redis.Redis,
    meeting_id: str,
    payload: dict[str, Any],
    maxlen: int = EVENTS_STREAM_MAXLEN,
) -> None:
    try:
        r.xadd(
            events_stream_key(meeting_id),
            {"payload": json.dumps(payload, ensure_ascii=False)},
            maxlen=maxlen,
            approximate=True,
        )
    except Exception:
        pass


def publish_json(r: redis.Redis, channel: str, payload: dict[str, Any]) -> None:
    r.publish(channel, json.dumps(payload, ensure_ascii=False))


def read_audio_stream(
    r: redis.Redis,
    stream_key: str,
    last_stream_id: str,
    block_ms: int,
    count: int,
):
    return r.xread({stream_key: last_stream_id}, block=block_ms, count=count)


def redis_setnx_ttl(r: redis.Redis, key: str, ttl: int) -> bool:
    try:
        return bool(r.set(key, "1", nx=True, ex=ttl))
    except Exception:
        return True


async def scan_audio_streams(r: redis.Redis) -> Set[str]:
    cursor = 0
    found: Set[str] = set()

    while True:
        cursor, keys = r.scan(cursor=cursor, match="audio:*", count=200)
        for key in keys:
            key_str = decode_if_bytes(key)
            meeting_id = parse_meeting_id_from_audio_key(key_str)
            if meeting_id:
                found.add(meeting_id)
        if cursor == 0:
            break

    return found
