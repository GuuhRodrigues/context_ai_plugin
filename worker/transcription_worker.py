from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional, Set, Tuple

import redis
import websockets
from websockets.exceptions import ConnectionClosed

from backend.observability import (
    add_observability_event,
    increment_counter,
    increment_float_counter,
    now_ms as obs_now_ms,
)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

OPENAI_REALTIME_URL = os.getenv(
    "OPENAI_REALTIME_URL",
    "wss://api.openai.com/v1/realtime?intent=transcription",
)

TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "pt")
TURN_DETECTION_THRESHOLD = float(os.getenv("TURN_DETECTION_THRESHOLD", "0.5"))
TURN_PREFIX_PADDING_MS = int(os.getenv("TURN_PREFIX_PADDING_MS", "250"))
TURN_SILENCE_DURATION_MS = int(os.getenv("TURN_SILENCE_DURATION_MS", "700"))

SCAN_INTERVAL_SECONDS = float(os.getenv("AUDIO_SCAN_INTERVAL_SECONDS", "1.0"))
STREAM_READ_COUNT = int(os.getenv("AUDIO_STREAM_READ_COUNT", "10"))
STREAM_READ_BLOCK_MS = int(os.getenv("AUDIO_STREAM_READ_BLOCK_MS", "1000"))

EVENTS_STREAM_MAXLEN = int(os.getenv("EVENTS_STREAM_MAXLEN", "5000"))
OBSERVABILITY_STORE_TRANSCRIPT_DELTAS = os.getenv("OBSERVABILITY_STORE_TRANSCRIPT_DELTAS", "false").lower() in (
    "1",
    "true",
    "yes",
)
MEETING_IDLE_TIMEOUT_SECONDS = float(os.getenv("MEETING_IDLE_TIMEOUT_SECONDS", "30"))
MAX_ACTIVE_MEETINGS = int(os.getenv("MAX_ACTIVE_MEETINGS", "8"))


def redis_client_bytes() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


def now_ts() -> int:
    return int(time.time())


def decode_if_bytes(x: Any) -> Any:
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")
    return x


def parse_meeting_id_from_audio_key(key: str) -> Optional[str]:
    if not key.startswith("audio:"):
        return None
    return key.split("audio:", 1)[1] or None


def events_stream_key(meeting_id: str) -> str:
    return f"events:{meeting_id}"


def transcript_channel(meeting_id: str) -> str:
    return f"transcript:{meeting_id}"


def audio_stream_key(meeting_id: str) -> str:
    return f"audio:{meeting_id}"


def meeting_closed_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:closed"


def is_meeting_closed(r: redis.Redis, meeting_id: str) -> bool:
    try:
        return bool(r.get(meeting_closed_key(meeting_id)))
    except Exception:
        return False


def add_event_to_stream(r: redis.Redis, meeting_id: str, payload: Dict[str, Any]) -> None:
    try:
        s = json.dumps(payload, ensure_ascii=False)
        r.xadd(
            events_stream_key(meeting_id),
            {"payload": s},
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        pass


async def openai_ws():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente no ambiente")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    return await websockets.connect(
        OPENAI_REALTIME_URL,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=10,
        max_size=None,
    )


async def send_event(ws, payload: dict):
    await ws.send(json.dumps(payload))


async def configure_transcription_session(ws):
    await send_event(
        ws,
        {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {"model": TRANSCRIBE_MODEL, "language": TRANSCRIBE_LANGUAGE},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": TURN_DETECTION_THRESHOLD,
                            "prefix_padding_ms": TURN_PREFIX_PADDING_MS,
                            "silence_duration_ms": TURN_SILENCE_DURATION_MS,
                        },
                    }
                },
            },
        },
    )


@dataclass
class MeetingState:
    meeting_id: str
    last_stream_id: str = "$"
    last_audio_ts: float = 0.0
    last_audio_redis_entry_ms: Optional[int] = None
    last_audio_sent_to_openai_ms: Optional[int] = None


class MeetingSession:
    def __init__(self, meeting_id: str):
        self.meeting_id = meeting_id
        self.r = redis_client_bytes()
        self.state = MeetingState(meeting_id=meeting_id, last_stream_id="$", last_audio_ts=time.time())
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        while not self._stop.is_set():
            try:
                await self._run_once()
            except ConnectionClosed as e:
                print(f"⚠️ [{self.meeting_id}] Realtime ConnectionClosed: {e}. Reconnecting in 2s...")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ [{self.meeting_id}] Session error: {e}. Reconnecting in 2s...")
                await asyncio.sleep(2)

    async def _run_once(self):
        print(f"🌐 [{self.meeting_id}] Connecting to OpenAI Realtime...", flush=True)
        ws = await openai_ws()

        try:
            print(f"✅ [{self.meeting_id}] Connected", flush=True)
            add_observability_event(self.r, self.meeting_id, "realtime.connected", source="transcription_worker")
            await configure_transcription_session(ws)

            reader_task = asyncio.create_task(self._realtime_reader(ws))
            pump_task = asyncio.create_task(self._redis_audio_pump(ws))

            done, pending = await asyncio.wait({reader_task, pump_task}, return_when=asyncio.FIRST_EXCEPTION)

            for t in pending:
                t.cancel()

            for t in done:
                exc = t.exception()
                if exc:
                    raise exc

        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def _realtime_reader(self, ws):
        async for message in ws:
            evt = json.loads(message)
            t = evt.get("type")

            if t == "error":
                print(f"🧨 [{self.meeting_id}] Realtime ERROR: {json.dumps(evt, ensure_ascii=False)}", flush=True)
                continue

            if t in (
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            ):
                received_at_ms = obs_now_ms()
                timings = {
                    "transcript_event_received_at_ms": received_at_ms,
                    "last_audio_redis_entry_ms": self.state.last_audio_redis_entry_ms,
                    "last_audio_sent_to_openai_at_ms": self.state.last_audio_sent_to_openai_ms,
                }
                if self.state.last_audio_sent_to_openai_ms:
                    timings["estimated_last_audio_to_transcript_ms"] = (
                        received_at_ms - self.state.last_audio_sent_to_openai_ms
                    )

                out = {
                    "meeting_id": self.meeting_id,
                    "ts": now_ts(),
                    "ts_ms": received_at_ms,
                    "kind": "transcript.delta" if t.endswith(".delta") else "transcript.completed",
                    "type": t,
                    "item_id": evt.get("item_id"),
                    "content_index": evt.get("content_index"),
                    "text": evt.get("delta", "") if t.endswith(".delta") else evt.get("transcript", ""),
                    "timings": timings,
                }

                self.r.publish(transcript_channel(self.meeting_id), json.dumps(out, ensure_ascii=False))
                if out["kind"] != "transcript.delta" or OBSERVABILITY_STORE_TRANSCRIPT_DELTAS:
                    add_event_to_stream(self.r, self.meeting_id, out)

    async def _redis_audio_pump(self, ws):
        skey = audio_stream_key(self.meeting_id)
        print(f"📼 [{self.meeting_id}] Consuming {skey}", flush=True)

        while not self._stop.is_set():
            if is_meeting_closed(self.r, self.meeting_id):
                print(f"🛑 [{self.meeting_id}] meeting closed - stopping session")
                self.stop()
                return

            if (time.time() - self.state.last_audio_ts) > MEETING_IDLE_TIMEOUT_SECONDS:
                print(f"🛑 [{self.meeting_id}] idle for {MEETING_IDLE_TIMEOUT_SECONDS}s - stopping session")
                self.stop()
                return

            resp = self.r.xread({skey: self.state.last_stream_id}, block=STREAM_READ_BLOCK_MS, count=STREAM_READ_COUNT)
            if not resp:
                continue

            for _, entries in resp:
                for entry_id, fields in entries:
                    self.state.last_stream_id = entry_id
                    pcm16 = fields.get(b"pcm16")
                    if not pcm16:
                        continue

                    self.state.last_audio_ts = time.time()
                    entry_id_str = decode_if_bytes(entry_id)
                    if isinstance(entry_id_str, str) and "-" in entry_id_str:
                        try:
                            self.state.last_audio_redis_entry_ms = int(entry_id_str.split("-", 1)[0])
                        except Exception:
                            pass
                    self.state.last_audio_sent_to_openai_ms = obs_now_ms()
                    increment_counter(self.r, self.meeting_id, "audio_chunks_forwarded_total", 1)
                    increment_counter(self.r, self.meeting_id, "audio_bytes_forwarded_total", len(pcm16))
                    increment_float_counter(
                        self.r,
                        self.meeting_id,
                        "audio_seconds_forwarded_estimated",
                        len(pcm16) / 2 / 24000,
                    )
                    b64_audio = base64.b64encode(pcm16).decode("ascii")
                    await send_event(ws, {"type": "input_audio_buffer.append", "audio": b64_audio})
                    await asyncio.sleep(0.005)


async def scan_audio_streams(r: redis.Redis) -> Set[str]:
    cursor = 0
    found: Set[str] = set()

    while True:
        cursor, keys = r.scan(cursor=cursor, match="audio:*", count=200)
        for k in keys:
            key_str = decode_if_bytes(k)
            mid = parse_meeting_id_from_audio_key(key_str)
            if mid:
                found.add(mid)
        if cursor == 0:
            break

    return found


async def main():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente no ambiente")

    r = redis_client_bytes()
    sessions: Dict[str, MeetingSession] = {}

    last_stats: Optional[Tuple[int, int]] = None

    print("✅ transcription_router booted")
    print(f"🔎 scanning audio:* every {SCAN_INTERVAL_SECONDS}s")
    print(f"🧯 MAX_ACTIVE_MEETINGS={MAX_ACTIVE_MEETINGS}")

    while True:
        try:
            meeting_ids = await scan_audio_streams(r)

            # remove stopped sessions
            for mid in list(sessions.keys()):
                if sessions[mid]._stop.is_set():
                    sessions.pop(mid, None)
                    print(f"➖ removed meeting session: {mid}")

            # start new sessions (respect cap)
            for mid in sorted(meeting_ids):
                if mid in sessions:
                    continue
                if is_meeting_closed(r, mid):
                    continue
                if len(sessions) >= MAX_ACTIVE_MEETINGS:
                    break

                print(f"➕ starting meeting session: {mid}")
                s = MeetingSession(mid)
                sessions[mid] = s
                asyncio.create_task(s.run())

            # ✅ log only when it changes
            stats = (len(meeting_ids), len(sessions))
            if stats != last_stats:
                print(f"📊 meetings_seen={stats[0]} active_sessions={stats[1]}")
                last_stats = stats

        except Exception as e:
            print(f"❌ router error: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
