from __future__ import annotations

import asyncio
import json

import redis
from fastapi import FastAPI, WebSocket

from backend.config import REDIS_HOST, REDIS_PORT

app = FastAPI()

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "plugin-talk-sense", "status": "running"}


# =========================
# 1) Ingestão de áudio PCM16
# =========================
@app.websocket("/audio/{meeting_id}")
async def audio_stream(ws: WebSocket, meeting_id: str):
    await ws.accept()
    stream_key = f"audio:{meeting_id}"

    print(f"🎧 Client connected (audio) meeting_id={meeting_id}")

    counter = 0
    try:
        while True:
            audio_bytes = await ws.receive_bytes()
            counter += 1

            # Grava chunks crus (PCM16) no Redis Stream
            r.xadd(stream_key, {"pcm16": audio_bytes}, maxlen=5000, approximate=True)

            if counter % 50 == 0:
                print(f"📥 Received {counter} chunks for {meeting_id}")

    except Exception as e:
        print(f"🔌 Disconnected (audio) meeting_id={meeting_id}: {e}")


# =========================
# 2) Stream de eventos (transcrições) pro client/UI
# =========================
@app.websocket("/events/{meeting_id}")
async def events_stream(ws: WebSocket, meeting_id: str):
    await ws.accept()
    channel = f"transcript:{meeting_id}"
    pubsub = r.pubsub()
    pubsub.subscribe(channel)

    print(f"🛰 Client connected (events) meeting_id={meeting_id}")

    loop = asyncio.get_running_loop()

    try:
        while True:
            # redis-py é bloqueante; roda em thread pra não travar o event loop
            msg = await loop.run_in_executor(None, pubsub.get_message, True, 1.0)

            if not msg:
                await asyncio.sleep(0.05)
                continue

            if msg["type"] != "message":
                continue

            payload = msg["data"]
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")

            await ws.send_text(payload)

    except Exception as e:
        print(f"🔌 Disconnected (events) meeting_id={meeting_id}: {e}")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


# =========================
# 3) Stream de insights pro client/UI
# =========================
@app.websocket("/insights/{meeting_id}")
async def insights_stream(ws: WebSocket, meeting_id: str):
    await ws.accept()
    channel = f"insights:{meeting_id}"
    pubsub = r.pubsub()
    pubsub.subscribe(channel)

    print(f"🧠 Client connected (insights) meeting_id={meeting_id}")

    loop = asyncio.get_running_loop()

    try:
        while True:
            # redis-py é bloqueante; roda em thread pra não travar o event loop
            msg = await loop.run_in_executor(None, pubsub.get_message, True, 1.0)

            if not msg:
                await asyncio.sleep(0.05)
                continue

            if msg["type"] != "message":
                continue

            payload = msg["data"]
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")

            await ws.send_text(payload)

    except Exception as e:
        print(f"🔌 Disconnected (insights) meeting_id={meeting_id}: {e}")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
