import asyncio
import queue
import os

import numpy as np
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

SAMPLE_RATE = 24000  # Realtime pcm16 exige 24kHz
MEETING_ID = os.getenv("MEETING_ID", "test-meeting-1")
TOKEN = os.getenv("TOKEN", "")

if not TOKEN:
    raise RuntimeError("TOKEN not set. Export TOKEN=...")

WS_URL = f"ws://localhost:8000/audio/{MEETING_ID}?token={TOKEN}"

audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=50)

FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 720 samples em 24kHz


def float32_to_pcm16_bytes(x: np.ndarray) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    i16 = (x * 32767.0).astype(np.int16)
    return i16.tobytes()


def audio_callback(indata, frames, time_info, status):
    try:
        mono = indata[:, 0].astype(np.float32)
        audio_queue.put_nowait(float32_to_pcm16_bytes(mono))
    except queue.Full:
        pass


async def send_audio(ws):
    print("🚀 Sender started")
    loop = asyncio.get_running_loop()

    while True:
        chunk_bytes: bytes = await loop.run_in_executor(None, audio_queue.get)
        await ws.send(chunk_bytes)


async def stream_audio():
    print(f"🎙 Target: {WS_URL}")
    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                max_size=None,
            ) as ws:
                print(f"🎙 Streaming connected: {WS_URL}")

                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=FRAME_SAMPLES,
                    callback=audio_callback,
                ):
                    await send_audio(ws)

        except ConnectionClosedError as e:
            # 1008 = policy violation (meeting fechado / token inválido)
            if getattr(e, "code", None) == 1008:
                print("🛑 Server rejected (1008). Meeting likely closed or token invalid. Stopping.")
                return
            print("🔁 Reconnecting in 2s...", e)
            await asyncio.sleep(2)

        except ConnectionClosedOK as e:
            print("🛑 Connection closed cleanly:", e)
            return

        except Exception as e:
            print("🔁 Reconnecting in 2s...", e)
            await asyncio.sleep(2)


asyncio.run(stream_audio())
