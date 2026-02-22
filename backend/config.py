import os

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Public base URL for API (used in ws_url/audio_url in create_meeting and refresh).
# Examples: http://localhost:8000, https://api.example.com
# WebSocket URLs are derived: http->ws, https->wss
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")