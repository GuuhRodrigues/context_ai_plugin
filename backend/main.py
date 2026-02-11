from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from typing import Any, Optional

import redis
from fastapi import FastAPI, WebSocket
from pydantic import BaseModel

from backend.config import REDIS_HOST, REDIS_PORT

app = FastAPI()

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)

DEFAULT_BACKFILL_SECONDS = 60
MAX_BACKFILL_SECONDS = 600
EVENTS_STREAM_MAXLEN = 5000

MEETING_TOKEN_TTL_SECONDS = int(__import__("os").getenv("MEETING_TOKEN_TTL_SECONDS", "7200"))
MEETING_CLOSED_TTL_SECONDS = int(__import__("os").getenv("MEETING_CLOSED_TTL_SECONDS", str(MEETING_TOKEN_TTL_SECONDS)))


class CreateMeetingResponse(BaseModel):
    meeting_id: str
    token: str
    ws_url: str
    audio_url: str
    expires_in_seconds: int


# -----------------------
# Utils
# -----------------------
def _decode_if_bytes(x: Any) -> Any:
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")
    return x


def _stream_min_id(backfill_seconds: int) -> str:
    now_ms = int(time.time() * 1000)
    min_ms = max(0, now_ms - backfill_seconds * 1000)
    return f"{min_ms}-0"


def _get_payload_from_fields(fields: Any) -> Optional[str]:
    if not isinstance(fields, dict):
        return None

    if "payload" in fields:
        return _decode_if_bytes(fields.get("payload"))
    if b"payload" in fields:
        return _decode_if_bytes(fields.get(b"payload"))

    for k in ("data", "event"):
        if k in fields:
            return _decode_if_bytes(fields.get(k))
        if k.encode() in fields:
            return _decode_if_bytes(fields.get(k.encode()))
    return None


# -----------------------
# Meeting auth / state
# -----------------------
def _meeting_token_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:token"


def _meeting_closed_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:closed"


def _save_meeting_token(meeting_id: str, token: str) -> None:
    r.set(_meeting_token_key(meeting_id), token.encode("utf-8"), ex=MEETING_TOKEN_TTL_SECONDS)
    r.delete(_meeting_closed_key(meeting_id))


def _get_meeting_token(meeting_id: str) -> Optional[str]:
    v = r.get(_meeting_token_key(meeting_id))
    if not v:
        return None
    return _decode_if_bytes(v)


def _is_meeting_closed(meeting_id: str) -> bool:
    return bool(r.get(_meeting_closed_key(meeting_id)))


def _close_meeting(meeting_id: str) -> None:
    r.delete(_meeting_token_key(meeting_id))
    r.set(_meeting_closed_key(meeting_id), b"1", ex=MEETING_CLOSED_TTL_SECONDS)


def _validate_token(meeting_id: str, token: str) -> bool:
    expected = _get_meeting_token(meeting_id)
    if not expected:
        return False
    try:
        return secrets.compare_digest(expected, token)
    except Exception:
        return False


def _purge_meeting_data(meeting_id: str) -> None:
    """
    Cleanup Redis data so the router stops seeing dead meetings and Redis doesn't grow unbounded.
    """
    # stop router from seeing stale meetings
    r.delete(f"audio:{meeting_id}")

    # remove backfill history
    r.delete(f"events:{meeting_id}")

    # remove dedup keys (pattern)
    # NOTE: use SCAN to avoid blocking redis
    cursor = 0
    pattern = f"dedup:{meeting_id}:*"
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=200)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


async def _ws_require_auth(ws: WebSocket, meeting_id: str) -> bool:
    if _is_meeting_closed(meeting_id):
        await ws.close(code=1008)
        return False

    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return False

    if not _validate_token(meeting_id, token):
        await ws.close(code=1008)
        return False

    try:
        r.expire(_meeting_token_key(meeting_id), MEETING_TOKEN_TTL_SECONDS)
    except Exception:
        pass

    return True


# -----------------------
# HTTP endpoints
# -----------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "plugin-talk-sense", "status": "running"}


@app.post("/meetings", response_model=CreateMeetingResponse)
def create_meeting():
    meeting_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    _save_meeting_token(meeting_id, token)

    ws_url = f"ws://localhost:8000/ws/{meeting_id}?token={token}&backfill={DEFAULT_BACKFILL_SECONDS}"
    audio_url = f"ws://localhost:8000/audio/{meeting_id}?token={token}"

    return CreateMeetingResponse(
        meeting_id=meeting_id,
        token=token,
        ws_url=ws_url,
        audio_url=audio_url,
        expires_in_seconds=MEETING_TOKEN_TTL_SECONDS,
    )


@app.post("/meetings/{meeting_id}/close")
def close_meeting(meeting_id: str):
    # 1) revoke + mark closed
    _close_meeting(meeting_id)

    # 2) cleanup redis meeting data (C)
    _purge_meeting_data(meeting_id)

    return {"meeting_id": meeting_id, "status": "closed"}


# =========================
# 1) Audio ingest (AUTH + kill switch)
# =========================
@app.websocket("/audio/{meeting_id}")
async def audio_stream(ws: WebSocket, meeting_id: str):
    await ws.accept()
    if not await _ws_require_auth(ws, meeting_id):
        return

    stream_key = f"audio:{meeting_id}"
    print(f"🎧 Client connected (audio) meeting_id={meeting_id}")

    counter = 0
    try:
        while True:
            if _is_meeting_closed(meeting_id):
                await ws.close(code=1008)
                return

            audio_bytes = await ws.receive_bytes()
            counter += 1

            if _is_meeting_closed(meeting_id):
                await ws.close(code=1008)
                return

            r.xadd(stream_key, {"pcm16": audio_bytes}, maxlen=5000, approximate=True)

            if counter % 50 == 0:
                print(f"📥 Received {counter} chunks for {meeting_id}")

    except Exception as e:
        print(f"🔌 Disconnected (audio) meeting_id={meeting_id}: {e}")


# =========================
# 2) Unified WS (AUTH + backfill + live + kill switch)
# =========================
@app.websocket("/ws/{meeting_id}")
async def unified_ws(ws: WebSocket, meeting_id: str):
    await ws.accept()
    if not await _ws_require_auth(ws, meeting_id):
        return

    events_stream = f"events:{meeting_id}"
    transcript_channel = f"transcript:{meeting_id}"
    insights_channel = f"insights:{meeting_id}"

    backfill_raw = ws.query_params.get("backfill")
    backfill_seconds: int = DEFAULT_BACKFILL_SECONDS

    if backfill_raw is not None:
        try:
            backfill_seconds = int(backfill_raw)
        except Exception:
            backfill_seconds = DEFAULT_BACKFILL_SECONDS

    if backfill_seconds < 0:
        backfill_seconds = 0
    if backfill_seconds > MAX_BACKFILL_SECONDS:
        backfill_seconds = MAX_BACKFILL_SECONDS

    print(f"🧩 Client connected (unified ws) meeting_id={meeting_id} backfill={backfill_seconds}s")

    # -----------------------
    # Backfill
    # -----------------------
    if backfill_seconds > 0:
        min_id = _stream_min_id(backfill_seconds)
        try:
            start_id = min_id
            total_sent = 0

            while True:
                if _is_meeting_closed(meeting_id):
                    await ws.close(code=1008)
                    return

                batch = r.xrange(events_stream, min=start_id, max="+", count=200)
                if not batch:
                    break

                for entry_id, fields in batch:
                    payload = _get_payload_from_fields(fields)
                    if payload:
                        await ws.send_text(payload)
                        total_sent += 1

                    entry_id_str = _decode_if_bytes(entry_id)
                    start_id = entry_id_str

                if isinstance(start_id, str) and "-" in start_id:
                    ms, seq = start_id.split("-", 1)
                    try:
                        start_id = f"{ms}-{int(seq) + 1}"
                    except Exception:
                        start_id = start_id + "0"

            print(f"↩️ backfill sent={total_sent} meeting_id={meeting_id}")

        except Exception as e:
            print(f"❌ backfill error meeting_id={meeting_id}: {e}")

    # -----------------------
    # Live
    # -----------------------
    pubsub = r.pubsub()
    pubsub.subscribe(transcript_channel, insights_channel)

    loop = asyncio.get_running_loop()
    try:
        while True:
            if _is_meeting_closed(meeting_id):
                await ws.close(code=1008)
                return

            msg = await loop.run_in_executor(None, pubsub.get_message, True, 1.0)

            if not msg:
                await asyncio.sleep(0.05)
                continue

            if msg["type"] != "message":
                continue

            payload = _decode_if_bytes(msg["data"])
            await ws.send_text(payload)

    except Exception as e:
        print(f"🔌 Disconnected (unified ws) meeting_id={meeting_id}: {e}")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
