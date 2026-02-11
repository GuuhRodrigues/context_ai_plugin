import asyncio
import json
import os
import websockets
from urllib.parse import quote

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

MEETING_ID = os.getenv("MEETING_ID", "")
TOKEN = os.getenv("TOKEN", "")
BACKFILL = os.getenv("BACKFILL", "60")


def build_ws_url() -> str:
    if not MEETING_ID or not TOKEN:
        raise RuntimeError("Set MEETING_ID and TOKEN env vars (run client/create_meeting.py first).")

    safe_token = quote(TOKEN, safe="")
    return f"ws://localhost:8000/ws/{MEETING_ID}?token={safe_token}&backfill={BACKFILL}"


async def main():
    ws_url = build_ws_url()
    print("🧩 Connecting to unified meeting websocket...")
    print("ws_url:", ws_url)

    async with websockets.connect(ws_url, max_size=None) as ws:
        print("✅ Connected! (will receive backfill + live)\n")

        async for msg in ws:
            data = json.loads(msg)
            kind = data.get("kind") or data.get("type")

            if kind == "qa":
                qa = data.get("qa", {})
                print("💬 QA")
                print("Q:", qa.get("question"))
                print("A:", qa.get("answer_short") or qa.get("answer"))
                print("confidence:", qa.get("confidence"))
                print("-" * 60)

            elif kind == "insight":
                print("✨ INSIGHT")
                print(json.dumps(data.get("insights", {}), ensure_ascii=False, indent=2))
                print("-" * 60)

            elif kind == "transcript.completed":
                print("📝 TRANSCRIPT COMPLETED:", (data.get("text") or "")[:160])

            else:
                pass


asyncio.run(main())
