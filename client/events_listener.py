import asyncio
import websockets

MEETING_ID = "test-meeting-1"
WS_URL = f"ws://localhost:8000/events/{MEETING_ID}"

async def main():
    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("🛰 listening events...")
        async for msg in ws:
            print(msg)

asyncio.run(main())
