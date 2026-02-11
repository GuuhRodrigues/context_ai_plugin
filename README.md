# Context AI Plugin --- Real‑time Meeting Copilot 🎧🧠

A real‑time conversation assistant that listens to live audio,
transcribes speech, generates insights, detects questions, and streams
everything back to a browser plugin via WebSocket.

This project is designed as the backend for a browser extension that
helps users during meetings, interviews, or conversations.

------------------------------------------------------------------------

# ✨ Features

• Live microphone audio streaming (PCM16 @ 24kHz)\
• Real‑time transcription using OpenAI Realtime API\
• AI insights generated from conversation context\
• Question detection + instant answer generation (QA mode)\
• WebSocket unified stream for UI/plugin\
• Multi‑meeting support with token authentication\
• Meeting lifecycle: create → use → close → cleanup\
• Redis used as real‑time event bus + backfill storage\
• Dockerized architecture

------------------------------------------------------------------------

# 🧱 Architecture Overview

Audio Sender → API → Redis Streams → Transcription Worker → Redis PubSub
→ Insights Worker → Redis PubSub → Unified WebSocket → Browser Plugin

Key concepts:

• `/audio/{meeting}` → receives PCM audio chunks\
• `transcription_worker` → sends audio to OpenAI Realtime and publishes
transcripts\
• `insights_worker` → generates insights + answers from transcripts\
• `/ws/{meeting}` → unified WebSocket for UI (transcripts + insights +
QA)

------------------------------------------------------------------------

# 📂 Project Structure

backend/ - main.py → FastAPI server (meetings, websockets, auth,
lifecycle) - config.py → environment settings

worker/ - transcription_worker.py → multi‑meeting realtime transcription
router - insights_worker.py → generates insights + QA from transcripts -
prompts.py → all system prompts

client/ - audio_sender.py → streams microphone audio -
unified_ws_listener.py → simulates browser plugin - create_meeting.py →
creates meeting + token

docker-compose.yml → runs API + workers + Redis

------------------------------------------------------------------------

# 🔐 Meeting Lifecycle

Create meeting: POST /meetings

Returns: - meeting_id - token - ws_url - audio_url

Close meeting: POST /meetings/{id}/close

This automatically: • revokes token • kills active WebSockets • stops
transcription session • cleans Redis data (audio/events/dedup)

------------------------------------------------------------------------

# 🚀 Running Locally

## 1) Start services

docker compose up --build

## 2) Create a meeting

python client/create_meeting.py

Export variables: export MEETING_ID="..." export TOKEN="..."

## 3) Start UI listener

python client/unified_ws_listener.py

## 4) Start audio streaming

python client/audio_sender.py

## 5) Close meeting

curl -X POST http://localhost:8000/meetings/\$MEETING_ID/close

------------------------------------------------------------------------

# 🧠 AI Agents Instructions

If you are an AI assistant helping with this repository:

You MUST read this section before suggesting changes.

Project stage: This is an early product prototype with
production‑oriented architecture.

Important invariants: 1. Multi‑meeting support must remain. 2. Token
auth must protect all WebSocket routes. 3. Closing a meeting must: •
revoke token • kill active sessions • clean Redis data 4. Workers must
scale horizontally. 5. Redis is the single event bus.

Do NOT suggest: • removing Redis • removing WebSockets • reverting to
single‑meeting architecture

When proposing improvements: • Prefer small PRs • Preserve streaming
design • Avoid breaking realtime flow

Main goal of the product: A browser plugin that provides real‑time
conversational assistance during meetings.

------------------------------------------------------------------------

# 🛣️ Roadmap

Next planned milestones: • Meeting status endpoint • Browser extension
UI • Token refresh • Cloud deployment

------------------------------------------------------------------------

Built with ❤️ for real‑time AI experiences.
