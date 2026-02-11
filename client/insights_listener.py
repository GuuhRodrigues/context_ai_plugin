import asyncio
import json
import websockets

MEETING_ID = "test-meeting-1"
WS_URL = f"ws://localhost:8000/insights/{MEETING_ID}"

async def main():
    print("🧠 Connecting to API insights websocket...")
    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("✅ Connected! Waiting events...\n")

        async for msg in ws:
            data = json.loads(msg)

            kind = data.get("kind")

            if kind == "qa":
                print("💬 QUESTION DETECTED")
                print("Q:", data["qa"]["question"])
                print("A:", data["qa"]["answer"])
                print("confidence:", data["qa"]["confidence"])
                print("-" * 60)

            elif kind == "insight":
                print("✨ INSIGHT")
                print(json.dumps(data["insights"], indent=2, ensure_ascii=False))
                print("-" * 60)

            else:
                print("RAW:", msg)

asyncio.run(main())
