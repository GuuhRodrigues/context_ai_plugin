import os
import requests

BASE = os.getenv("API_BASE", "http://localhost:8000")

resp = requests.post(f"{BASE}/meetings", timeout=10)
resp.raise_for_status()
data = resp.json()

print("✅ Meeting created")
print("meeting_id:", data["meeting_id"])
print("token:", data["token"])
print("ws_url:", data["ws_url"])
print("audio_url:", data["audio_url"])
print("expires_in_seconds:", data["expires_in_seconds"])
