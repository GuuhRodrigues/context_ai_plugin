from __future__ import annotations

import secrets
import uuid
from typing import Any, Optional

import redis
from fastapi import APIRouter, HTTPException, Query

from backend.configs import MEETING_CLOSED_TTL_SECONDS, MEETING_TOKEN_TTL_SECONDS
from backend.src.integrations.redis import (
    audio_stream_key,
    dedup_key_pattern,
    events_stream_key,
    meeting_closed_key,
    meeting_token_key,
    observability_counters_key,
    redis_client_bytes,
)
from backend.src.schemas import (
    CreateMeetingResponse,
    MeetingStatusResponse,
    ObservabilityEventRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
)
from backend.src.utils import (
    audio_websocket_url,
    decode_if_bytes,
    event_websocket_url,
    websocket_base_url,
)
from observability.backend.observability import add_observability_event, generate_meeting_artifacts

router = APIRouter()
r = redis_client_bytes()


class MeetingError(Exception):
    pass


class InvalidMeetingTokenError(MeetingError):
    pass


class MeetingClosedError(MeetingError):
    pass


def save_meeting_token(r: redis.Redis, meeting_id: str, token: str) -> None:
    r.set(meeting_token_key(meeting_id), token.encode("utf-8"), ex=MEETING_TOKEN_TTL_SECONDS)
    r.delete(meeting_closed_key(meeting_id))


def get_meeting_token(r: redis.Redis, meeting_id: str) -> Optional[str]:
    value = r.get(meeting_token_key(meeting_id))
    if not value:
        return None
    return decode_if_bytes(value)


def is_meeting_closed(r: redis.Redis, meeting_id: str) -> bool:
    return bool(r.get(meeting_closed_key(meeting_id)))


def close_meeting_state(r: redis.Redis, meeting_id: str) -> None:
    r.delete(meeting_token_key(meeting_id))
    r.set(meeting_closed_key(meeting_id), b"1", ex=MEETING_CLOSED_TTL_SECONDS)


def validate_token(r: redis.Redis, meeting_id: str, token: str) -> bool:
    expected = get_meeting_token(r, meeting_id)
    if not expected:
        return False
    try:
        return secrets.compare_digest(expected, token)
    except Exception:
        return False


def ensure_valid_token(r: redis.Redis, meeting_id: str, token: str) -> None:
    if not validate_token(r, meeting_id, token):
        raise InvalidMeetingTokenError("Invalid or expired token")


def refresh_meeting_ttl(r: redis.Redis, meeting_id: str) -> None:
    try:
        r.expire(meeting_token_key(meeting_id), MEETING_TOKEN_TTL_SECONDS)
    except Exception:
        pass


def purge_meeting_data(r: redis.Redis, meeting_id: str) -> None:
    """
    Cleanup Redis data so the router stops seeing dead meetings and Redis doesn't grow unbounded.
    """
    r.delete(audio_stream_key(meeting_id))
    r.delete(events_stream_key(meeting_id))
    r.delete(observability_counters_key(meeting_id))

    cursor = 0
    pattern = dedup_key_pattern(meeting_id)
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=200)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _meeting_urls(meeting_id: str, token: str) -> dict[str, str]:
    base = websocket_base_url()
    return {
        "event_url": event_websocket_url(base, meeting_id, token),
        "audio_url": audio_websocket_url(base, meeting_id, token),
    }


def create_meeting(r: redis.Redis) -> dict[str, Any]:
    meeting_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    save_meeting_token(r, meeting_id, token)
    add_observability_event(r, meeting_id, "meeting.created")

    return {
        "meeting_id": meeting_id,
        "token": token,
        **_meeting_urls(meeting_id, token),
        "expires_in_seconds": MEETING_TOKEN_TTL_SECONDS,
    }


def get_meeting_expires_in(r: redis.Redis, meeting_id: str, token: str) -> int:
    ensure_valid_token(r, meeting_id, token)

    ttl = r.ttl(meeting_token_key(meeting_id))
    return ttl if ttl > 0 else MEETING_TOKEN_TTL_SECONDS


def refresh_meeting_token(r: redis.Redis, meeting_id: str, current_token: str) -> dict[str, Any]:
    if is_meeting_closed(r, meeting_id):
        raise MeetingClosedError("Meeting is closed")

    ensure_valid_token(r, meeting_id, current_token)

    new_token = secrets.token_urlsafe(32)
    save_meeting_token(r, meeting_id, new_token)
    add_observability_event(r, meeting_id, "meeting.token_refreshed")

    return {
        "token": new_token,
        **_meeting_urls(meeting_id, new_token),
        "expires_in_seconds": MEETING_TOKEN_TTL_SECONDS,
    }


def record_observability_event(
    r: redis.Redis,
    meeting_id: str,
    token: str,
    kind: str,
    metrics: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    ensure_valid_token(r, meeting_id, token)
    add_observability_event(
        r,
        meeting_id,
        kind,
        metrics=metrics,
        metadata=metadata,
        source="extension",
    )


def close_meeting(r: redis.Redis, meeting_id: str) -> dict[str, Any]:
    add_observability_event(r, meeting_id, "meeting.close_requested")
    close_meeting_state(r, meeting_id)

    try:
        observability = generate_meeting_artifacts(r, meeting_id)
    except Exception as exc:
        observability = {"error": str(exc)}

    purge_meeting_data(r, meeting_id)

    return {"meeting_id": meeting_id, "status": "closed", "observability": observability}


@router.get("/meetings/{meeting_id}", response_model=MeetingStatusResponse)
def get_meeting_status(
    meeting_id: str,
    token: str = Query(..., description="Meeting token (required for auth)"),
):
    try:
        expires_in = get_meeting_expires_in(r, meeting_id, token)
    except InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    return MeetingStatusResponse(
        meeting_id=meeting_id,
        status="open",
        expires_in_seconds=expires_in,
    )


@router.post("/meetings", response_model=CreateMeetingResponse)
def create_meeting_endpoint():
    return CreateMeetingResponse(**create_meeting(r))


@router.post("/meetings/{meeting_id}/refresh", response_model=RefreshTokenResponse)
def refresh_meeting_token_endpoint(meeting_id: str, body: RefreshTokenRequest):
    try:
        return RefreshTokenResponse(**refresh_meeting_token(r, meeting_id, body.token))
    except MeetingClosedError:
        raise HTTPException(status_code=401, detail="Meeting is closed") from None
    except InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None


@router.post("/meetings/{meeting_id}/observability")
def record_meeting_observability(meeting_id: str, body: ObservabilityEventRequest):
    try:
        record_observability_event(
            r,
            meeting_id,
            body.token,
            body.kind,
            metrics=body.metrics,
            metadata=body.metadata,
        )
    except InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    return {"ok": True}


@router.post("/meetings/{meeting_id}/close")
def close_meeting_endpoint(meeting_id: str):
    return close_meeting(r, meeting_id)
