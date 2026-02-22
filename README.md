
# Context AI Plugin — Real‑time Meeting Copilot 🎧🧠

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSockets-009688)
![Redis](https://img.shields.io/badge/Redis-Streams%20%2B%20PubSub-D82C20)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)
![Status](https://img.shields.io/badge/Stage-Prototype%20%2F%20MVP-orange)

A real‑time conversation assistant that captures live audio, produces real‑time transcripts, generates helpful insights, detects questions, and streams everything back to a UI (browser extension) over WebSockets.

This repository currently contains:
- A FastAPI backend (meeting lifecycle + WebSocket gateways)
- Background workers (transcription router + insights generation)
- Client scripts that simulate the browser extension behavior

---

## ✨ What it does

- **Live microphone streaming** (PCM16 @ 24kHz)
- **Realtime transcription** (OpenAI Realtime API — transcription intent)
- **Insights generation** (summaries, key terms, action hints)
- **Question detection + answers (QA)** when the user asks something directly
- **Unified meeting WebSocket** for UI/plugin: backfill + live stream in one socket
- **Multi‑meeting support** with per‑meeting token auth
- **Meeting lifecycle**: create → use → close → cleanup (Redis purge + session shutdown)

---

## 🧱 System Architecture

### High‑level flow

```mermaid
flowchart LR
  A[Browser Extension / UI] -->|WS: /audio/:meeting| B[FastAPI API]
  A -->|WS: /ws/:meeting| B
  B -->|Redis Stream: audio::meeting| R[(Redis)]
  R -->|Stream consumer| T[transcription_worker]
  T -->|PubSub: transcript::meeting| R
  T -->|XADD: events::meeting| R
  R -->|PubSub: transcript::meeting| I[insights_worker]
  I -->|PubSub: insights::meeting| R
  R -->|WS push| B
  B -->|WS messages| A
```

### Event stream model (UI receives)

The UI receives a single WebSocket stream (`/ws/{meeting_id}`) that can include:

- `transcript.completed` (final transcript chunk for a spoken turn)
- `insight` (generated insight payload)
- `qa` (question detected + answer)

Backfill is replayed from `events:{meeting_id}` and then live events continue via PubSub.

---

## 🔐 Meeting Lifecycle & Security

### Token model

- A meeting is created using `POST /meetings` which returns:
  - `meeting_id`
  - `token`
  - `ws_url` (unified websocket)
  - `audio_url` (audio websocket)
  - TTL

- `GET /meetings/{meeting_id}?token=...` returns meeting status (`open`) and `expires_in_seconds` when token is valid; returns 401 otherwise.
- `POST /meetings/{meeting_id}/refresh` with body `{"token": "..."}` returns a new token and updated `ws_url`/`audio_url`; use it to renew before expiry (e.g. reconnecting or long meetings).
- All WebSocket routes require `?token=`.
- On meeting close:
  - token is revoked
  - active WS connections are killed (`1008 policy violation`)
  - Redis keys are purged (`audio:*`, `events:*`, `dedup:*`)

### Sequence: create → use → close

```mermaid
sequenceDiagram
  participant UI as UI/Plugin
  participant API as FastAPI
  participant Redis as Redis
  participant TR as transcription_worker
  participant IN as insights_worker
  participant OpenAI as OpenAI Realtime

  UI->>API: POST /meetings
  API-->>UI: meeting_id + token + ws_url + audio_url

  UI->>API: WS connect /ws/{meeting}?token=...
  API->>Redis: XRANGE events:{meeting} (backfill)
  Redis-->>API: backfill events
  API-->>UI: backfill messages

  UI->>API: WS connect /audio/{meeting}?token=...
  UI->>API: send pcm16 chunks
  API->>Redis: XADD audio:{meeting} {pcm16}

  TR->>Redis: XREAD audio:{meeting}
  TR->>OpenAI: Realtime transcription session
  OpenAI-->>TR: transcript events
  TR->>Redis: PUBLISH transcript:{meeting}
  TR->>Redis: XADD events:{meeting} {payload}

  IN->>Redis: SUBSCRIBE transcript:{meeting}
  IN->>Redis: PUBLISH insights:{meeting}
  IN->>Redis: XADD events:{meeting} {payload}

  API->>Redis: SUBSCRIBE transcript:{meeting}, insights:{meeting}
  Redis-->>API: events
  API-->>UI: live messages

  UI->>API: POST /meetings/{meeting}/close
  API->>Redis: revoke token + mark closed
  API->>Redis: DEL audio:{meeting}, events:{meeting}, dedup:{meeting}:*
  API-->>UI: {status: closed}
  API-->>UI: WS close 1008 (active connections)
```

---

## 📂 Project Structure

```text
CONTEXT_AI_PLUGIN/
│
├── backend/
│   ├── config.py               # Environment configuration
│   ├── main.py                 # FastAPI server (meetings, websockets, auth, lifecycle)
│   └── requirements.txt
│
├── client/
│   ├── __init__.py
│   ├── audio_sender.py         # Streams microphone audio (PCM16 24kHz)
│   ├── create_meeting.py       # Creates meeting + token
│   ├── events_listener.py      # Legacy (split streams) listener
│   ├── insights_listener.py    # Legacy insights listener
│   └── unified_ws_listener.py  # Simulates plugin: unified backfill + live stream
│
├── worker/
│   ├── __init__.py
│   ├── transcription_worker.py # Multi-meeting realtime transcription router
│   ├── insights_worker.py      # Generates insights + QA
│   └── prompts.py              # Centralized AI prompts
│
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── .env
└── README.md
```

---

## ✅ Requirements

- Python **3.11+**
- Docker + Docker Compose
- A Redis instance (included via compose)
- `OPENAI_API_KEY` set in your environment (or `.env`)

---

## 🔧 Configuration

### Environment variables

| Variable | Used by | Default | Notes |
|---|---|---:|---|
| `OPENAI_API_KEY` | workers | — | Required |
| `API_BASE_URL` | api | `http://localhost:8000` | Public base URL for `ws_url`/`audio_url` in create/refresh (http→ws, https→wss) |
| `REDIS_HOST` | api/workers | `localhost` | In Docker: `redis` |
| `REDIS_PORT` | api/workers | `6379` | — |
| `MEETING_TOKEN_TTL_SECONDS` | api | `7200` | Token lifetime |
| `MEETING_CLOSED_TTL_SECONDS` | api | `7200` | Closed flag retention |
| `INSIGHTS_MODEL` | insights_worker | `gpt-4o-mini` | Chat model |
| `INSIGHTS_HISTORY_MAXLEN` | insights_worker | `12` | Rolling window |
| `INSIGHTS_CONTEXT_TAKE_LAST` | insights_worker | `8` | Context slice |
| `INSIGHTS_DEBOUNCE_SECONDS` | insights_worker | `120` | Insight rate limit |
| `MAX_ACTIVE_MEETINGS` | transcription_worker | `8` | Router capacity |
| `MEETING_IDLE_TIMEOUT_SECONDS` | transcription_worker | `30` | Auto stop |

> Tip: keep `INSIGHTS_DEBOUNCE_SECONDS=120` to reduce spam and cost.

---

## 🚀 Run locally (end‑to‑end)

### 1) Start services

```bash
docker compose up --build
```

### 2) Create a meeting (returns meeting_id + token)

```bash
python client/create_meeting.py
```

Export the returned values:

```bash
export MEETING_ID="..."
export TOKEN="..."
```

### 3) Start unified listener (simulates the plugin)

```bash
python client/unified_ws_listener.py
```

### 4) Start audio streaming

```bash
python client/audio_sender.py
```

### 5) Check meeting status (optional)

```bash
curl "http://localhost:8000/meetings/$MEETING_ID?token=$TOKEN"
```

### 6) Refresh token (optional, for long meetings)

```bash
curl -X POST "http://localhost:8000/meetings/$MEETING_ID/refresh" -H "Content-Type: application/json" -d "{\"token\": \"$TOKEN\"}"
```

Update `TOKEN` (and reconnect WebSockets if needed) with the new token from the response.

### 7) Close meeting (kills active sockets + cleans Redis)

```bash
curl -X POST "http://localhost:8000/meetings/$MEETING_ID/close"
```

---

## 🧪 Quick sanity checks

### Health

```bash
curl http://localhost:8000/health
```

### Confirm Redis cleanup after close (optional)

```bash
docker exec -it <redis_container> redis-cli EXISTS "audio:$MEETING_ID"
docker exec -it <redis_container> redis-cli EXISTS "events:$MEETING_ID"
docker exec -it <redis_container> redis-cli SCAN 0 MATCH "dedup:$MEETING_ID:*" COUNT 10
```

Expected:
- `audio:*` and `events:*` → `0` after close
- `dedup:*` → empty

---

## 🧯 Troubleshooting

### Audio connects then closes with 1008
This means the meeting is **closed** or the `token` is **invalid/expired**.

- Re-create a meeting with `python client/create_meeting.py`
- Export the new `MEETING_ID` + `TOKEN`

### No transcripts
- Check `OPENAI_API_KEY` is set for the `transcription` container.
- Ensure your microphone works and sample rate is 24kHz.
- Confirm `audio_sender.py` is pointing to the correct meeting/token.

### Insights not showing
- Confirm `insights_worker` is running.
- Verify `INSIGHTS_MODEL` and API key.
- Check `INSIGHTS_DEBOUNCE_SECONDS` (e.g., 120s means fewer insights).

---

## 🧠 AI Agents Instructions (IMPORTANT)

If you are an AI assistant helping with this repository:

**Read this section before proposing any change.**

### Current project state
This is an MVP/prototype with production‑oriented boundaries:
- Multi‑meeting support
- Token‑secured WebSockets
- Event‑driven streaming
- Worker scalability

### Architectural invariants (DO NOT BREAK)
1. **Multi‑meeting** must remain.
2. **Token auth** must protect **all** WebSocket routes.
3. Closing a meeting must:
   - revoke token
   - kill active sessions
   - clean Redis data
4. Workers must remain horizontally scalable.
5. Redis remains the single event bus + backfill storage.
6. Keep streaming event-driven (no redesign into batch polling).

### Do NOT suggest
- Removing Redis
- Removing WebSockets
- Reverting to single‑meeting architecture
- Synchronous/blocking designs that break realtime flow
- Polling-based “batch transcription” redesign

### How to propose improvements
- Prefer small, reviewable PRs
- Preserve streaming behavior and meeting lifecycle
- Keep clear boundaries: `backend/` vs `worker/` vs `client/`
- Avoid breaking existing client scripts

### Product goal
A browser plugin that provides real‑time conversational assistance during meetings.

---

## 🛣️ Roadmap (next milestones)

- ~~Meeting status endpoint (`GET /meetings/{meeting_id}`)~~ ✅
- ~~Token refresh / session renewal flow~~ ✅
- ~~URLs dinâmicas (`API_BASE_URL`)~~ ✅
- Browser extension UI overlay
- Metrics + observability
- Production deployment (cloud)

---

Built for real‑time AI experiences 🚀
