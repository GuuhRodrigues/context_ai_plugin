import json
import redis
import os

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MEETING_ID = os.getenv("MEETING_ID", "test-meeting-1")

CHANNEL = f"insights:{MEETING_ID}"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
pubsub = r.pubsub()
pubsub.subscribe(CHANNEL)

print("🧠 listening insights...")

for msg in pubsub.listen():
    if msg["type"] != "message":
        continue
    data = json.loads(msg["data"])
    print(json.dumps(data, ensure_ascii=False, indent=2))
