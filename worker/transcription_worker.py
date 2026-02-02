from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass

import redis
import websockets
from websockets.exceptions import ConnectionClosed

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

MEETING_ID = os.getenv("MEETING_ID", "test-meeting-1")

AUDIO_STREAM_KEY = f"audio:{MEETING_ID}"
TRANSCRIPT_CHANNEL = f"transcript:{MEETING_ID}"

# transcription-only session
REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"


@dataclass
class RedisStreamCursor:
    last_id: str = "$"


def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


async def openai_ws():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente no ambiente")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    return await websockets.connect(
        REALTIME_URL,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=10,
        max_size=None,
    )


async def send_event(ws, payload: dict):
    try:
        await ws.send(json.dumps(payload))
    except Exception as e:
        code = getattr(ws, "close_code", None)
        reason = getattr(ws, "close_reason", None)
        print(f"❌ ws.send failed: {e} close_code={code} close_reason={reason}", flush=True)
        raise


async def realtime_reader(ws, r: redis.Redis):
    async for message in ws:
        evt = json.loads(message)
        t = evt.get("type")

        if t == "error":
            print("🧨 Realtime ERROR:", json.dumps(evt, ensure_ascii=False), flush=True)
            continue

        # Útil para confirmar que o VAD está detectando turnos
        if t in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
            print(f"🎛 VAD event: {t} | {json.dumps(evt, ensure_ascii=False)}", flush=True)

        if t in (
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.completed",
        ):
            out = {
                "meeting_id": MEETING_ID,
                "type": t,
                "item_id": evt.get("item_id"),
                "content_index": evt.get("content_index"),
                "text": evt.get("delta", "") if t.endswith(".delta") else evt.get("transcript", ""),
            }
            r.publish(TRANSCRIPT_CHANNEL, json.dumps(out, ensure_ascii=False))


async def redis_audio_pump(ws, r: redis.Redis, cursor: RedisStreamCursor):
    print(f"📼 Consuming Redis stream {AUDIO_STREAM_KEY} from {cursor.last_id}", flush=True)

    while True:
        resp = r.xread({AUDIO_STREAM_KEY: cursor.last_id}, block=1000, count=10)
        if not resp:
            continue

        for _, entries in resp:
            for entry_id, fields in entries:
                cursor.last_id = entry_id

                pcm16 = fields.get(b"pcm16")
                if not pcm16:
                    continue

                # Realtime expects base64 audio bytes for input_audio_buffer.append
                b64_audio = base64.b64encode(pcm16).decode("ascii")
                await send_event(ws, {"type": "input_audio_buffer.append", "audio": b64_audio})

                # throttle leve
                await asyncio.sleep(0.005)


async def run_once(r: redis.Redis, cursor: RedisStreamCursor):
    print("🌐 Connecting to OpenAI Realtime (transcription)...", flush=True)
    ws = await openai_ws()
    try:
        print("✅ Connected to Realtime", flush=True)

        # ✅ Schema correto para transcription session:
        # session.audio.input.format / transcription / turn_detection
        # Exemplo oficial no guia de Realtime transcription. :contentReference[oaicite:1]{index=1}
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
                            "transcription": {
                                "model": "gpt-4o-mini-transcribe",
                                "language": "pt",
                            },
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                            },
                        }
                    },
                    # opcional: logprobs
                    # "include": ["item.input_audio_transcription.logprobs"],
                },
            },
        )

        reader_task = asyncio.create_task(realtime_reader(ws, r))
        pump_task = asyncio.create_task(redis_audio_pump(ws, r, cursor))

        done, pending = await asyncio.wait(
            {reader_task, pump_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for t in pending:
            t.cancel()

        for t in done:
            exc = t.exception()
            if exc:
                raise exc

    finally:
        code = getattr(ws, "close_code", None)
        reason = getattr(ws, "close_reason", None)
        print(f"🔌 Realtime WS closed code={code} reason={reason}", flush=True)
        try:
            await ws.close()
        except Exception:
            pass


async def main():
    print("✅ worker booted", flush=True)
    print(f"MEETING_ID={MEETING_ID}", flush=True)
    print(f"AUDIO_STREAM_KEY={AUDIO_STREAM_KEY}", flush=True)

    r = redis_client()
    cursor = RedisStreamCursor(last_id="$")

    while True:
        try:
            await run_once(r, cursor)
        except ConnectionClosed as e:
            print(f"⚠️ ConnectionClosed: {e}. Reconnecting in 2s...", flush=True)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"❌ Worker error: {e}. Reconnecting in 2s...", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
