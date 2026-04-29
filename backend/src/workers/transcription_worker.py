from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from websockets.exceptions import ConnectionClosed

from backend.configs import (
    AUDIO_SCAN_INTERVAL_SECONDS as SCAN_INTERVAL_SECONDS,
    AUDIO_STREAM_READ_BLOCK_MS as STREAM_READ_BLOCK_MS,
    AUDIO_STREAM_READ_COUNT as STREAM_READ_COUNT,
    MAX_ACTIVE_MEETINGS,
    MEETING_IDLE_TIMEOUT_SECONDS,
    OBSERVABILITY_STORE_TRANSCRIPT_DELTAS,
    OPENAI_API_KEY,
    TRANSCRIPTION_SAMPLE_RATE,
)
from observability.backend.observability import (
    add_observability_event,
    increment_counter,
    increment_float_counter,
    now_ms as obs_now_ms,
)
from backend.src.integrations.openai import configure_transcription_session, openai_ws, send_event
from backend.src.integrations.redis import (
    add_event_to_stream,
    audio_stream_key,
    decode_if_bytes,
    is_meeting_closed,
    now_ts,
    publish_json,
    read_audio_stream,
    redis_client_bytes,
    scan_audio_streams,
    transcript_channel,
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

                publish_json(self.r, transcript_channel(self.meeting_id), out)
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

            resp = read_audio_stream(
                self.r,
                skey,
                self.state.last_stream_id,
                STREAM_READ_BLOCK_MS,
                STREAM_READ_COUNT,
            )
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
                        len(pcm16) / 2 / TRANSCRIPTION_SAMPLE_RATE,
                    )
                    b64_audio = base64.b64encode(pcm16).decode("ascii")
                    await send_event(ws, {"type": "input_audio_buffer.append", "audio": b64_audio})
                    await asyncio.sleep(0.005)


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
