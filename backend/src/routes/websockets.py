from __future__ import annotations

import asyncio

import redis
from fastapi import APIRouter, WebSocket

from backend.configs import AUDIO_STREAM_MAXLEN
from backend.src.integrations.redis import (
    audio_stream_key,
    events_stream_key,
    insights_channel,
    redis_client_bytes,
    transcript_channel,
)
from backend.src.routes.meetings import (
    is_meeting_closed,
    refresh_meeting_ttl,
    validate_token,
)
from backend.src.utils import (
    decode_if_bytes,
    get_payload_from_fields,
    parse_backfill_seconds,
    stream_min_id,
)
from observability.backend.observability import (
    add_observability_event,
    increment_counter,
    increment_float_counter,
)

router = APIRouter()
r = redis_client_bytes()


async def require_websocket_auth(r: redis.Redis, ws: WebSocket, meeting_id: str) -> bool:
    if is_meeting_closed(r, meeting_id):
        await ws.close(code=1008)
        return False

    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return False

    if not validate_token(r, meeting_id, token):
        await ws.close(code=1008)
        return False

    refresh_meeting_ttl(r, meeting_id)
    return True


async def handle_audio_stream(r: redis.Redis, ws: WebSocket, meeting_id: str) -> None:
    await ws.accept()
    if not await require_websocket_auth(r, ws, meeting_id):
        return

    stream_key = audio_stream_key(meeting_id)
    add_observability_event(r, meeting_id, "audio_ws.connected")
    print(f"Client connected (audio) meeting_id={meeting_id}")

    counter = 0
    try:
        while True:
            if is_meeting_closed(r, meeting_id):
                await ws.close(code=1008)
                return

            audio_bytes = await ws.receive_bytes()
            counter += 1
            increment_counter(r, meeting_id, "audio_chunks_received_total", 1)
            increment_counter(r, meeting_id, "audio_bytes_received_total", len(audio_bytes))
            increment_float_counter(r, meeting_id, "audio_seconds_received_estimated", len(audio_bytes) / 2 / 24000)

            if is_meeting_closed(r, meeting_id):
                await ws.close(code=1008)
                return

            r.xadd(stream_key, {"pcm16": audio_bytes}, maxlen=AUDIO_STREAM_MAXLEN, approximate=True)

            if counter % 50 == 0:
                print(f"Received {counter} chunks for {meeting_id}")

    except Exception as exc:
        add_observability_event(
            r,
            meeting_id,
            "audio_ws.disconnected",
            metrics={"chunks_received_on_connection": counter},
            metadata={"reason": str(exc)},
        )
        print(f"Disconnected (audio) meeting_id={meeting_id}: {exc}")


async def _send_backfill(
    r: redis.Redis,
    ws: WebSocket,
    meeting_id: str,
    events_stream: str,
    backfill_seconds: int,
) -> bool:
    if backfill_seconds <= 0:
        return True

    start_id = stream_min_id(backfill_seconds)
    total_sent = 0

    try:
        while True:
            if is_meeting_closed(r, meeting_id):
                await ws.close(code=1008)
                return False

            batch = r.xrange(events_stream, min=start_id, max="+", count=200)
            if not batch:
                break

            for entry_id, fields in batch:
                payload = get_payload_from_fields(fields)
                if payload:
                    await ws.send_text(payload)
                    total_sent += 1

                entry_id_str = decode_if_bytes(entry_id)
                start_id = entry_id_str

            if isinstance(start_id, str) and "-" in start_id:
                ms, seq = start_id.split("-", 1)
                try:
                    start_id = f"{ms}-{int(seq) + 1}"
                except Exception:
                    start_id = start_id + "0"

        print(f"backfill sent={total_sent} meeting_id={meeting_id}")
        return True

    except Exception as exc:
        print(f"backfill error meeting_id={meeting_id}: {exc}")
        return True


async def _stream_live_events(
    r: redis.Redis,
    ws: WebSocket,
    meeting_id: str,
    channels: tuple[str, str],
) -> None:
    pubsub = r.pubsub()
    pubsub.subscribe(*channels)

    loop = asyncio.get_running_loop()
    try:
        while True:
            if is_meeting_closed(r, meeting_id):
                await ws.close(code=1008)
                return

            msg = await loop.run_in_executor(None, pubsub.get_message, True, 1.0)

            if not msg:
                await asyncio.sleep(0.05)
                continue

            if msg["type"] != "message":
                continue

            payload = decode_if_bytes(msg["data"])
            await ws.send_text(payload)

    except Exception as exc:
        print(f"Disconnected (event ws) meeting_id={meeting_id}: {exc}")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


async def handle_event_stream(r: redis.Redis, ws: WebSocket, meeting_id: str) -> None:
    await ws.accept()
    if not await require_websocket_auth(r, ws, meeting_id):
        return

    events_stream = events_stream_key(meeting_id)
    channels = (transcript_channel(meeting_id), insights_channel(meeting_id))
    backfill_seconds = parse_backfill_seconds(ws.query_params.get("backfill"))

    add_observability_event(r, meeting_id, "event_ws.connected", metrics={"backfill_seconds": backfill_seconds})
    print(f"Client connected (event ws) meeting_id={meeting_id} backfill={backfill_seconds}s")

    if not await _send_backfill(r, ws, meeting_id, events_stream, backfill_seconds):
        return

    await _stream_live_events(r, ws, meeting_id, channels)


@router.websocket("/audio/{meeting_id}")
async def audio_stream(ws: WebSocket, meeting_id: str):
    await handle_audio_stream(r, ws, meeting_id)


@router.websocket("/event/{meeting_id}")
async def event_ws(ws: WebSocket, meeting_id: str):
    await handle_event_stream(r, ws, meeting_id)
