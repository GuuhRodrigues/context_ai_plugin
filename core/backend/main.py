from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, WebSocket

from core.backend.src.schemas import (
    CreateMeetingResponse,
    MeetingStatusResponse,
    ObservabilityEventRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
)
from core.backend.src.services import meeting
from core.backend.src.services.redis import redis_client_bytes
from core.backend.src.services.websocket import handle_audio_stream, handle_event_stream

app = FastAPI()
r = redis_client_bytes()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "context-ai", "status": "running"}


@app.get("/meetings/{meeting_id}", response_model=MeetingStatusResponse)
def get_meeting_status(
    meeting_id: str,
    token: str = Query(..., description="Meeting token (required for auth)"),
):
    try:
        expires_in = meeting.get_meeting_expires_in(r, meeting_id, token)
    except meeting.InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    return MeetingStatusResponse(
        meeting_id=meeting_id,
        status="open",
        expires_in_seconds=expires_in,
    )


@app.post("/meetings", response_model=CreateMeetingResponse)
def create_meeting():
    return CreateMeetingResponse(**meeting.create_meeting(r))


@app.post("/meetings/{meeting_id}/refresh", response_model=RefreshTokenResponse)
def refresh_meeting_token(meeting_id: str, body: RefreshTokenRequest):
    try:
        return RefreshTokenResponse(**meeting.refresh_meeting_token(r, meeting_id, body.token))
    except meeting.MeetingClosedError:
        raise HTTPException(status_code=401, detail="Meeting is closed") from None
    except meeting.InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None


@app.post("/meetings/{meeting_id}/observability")
def record_meeting_observability(meeting_id: str, body: ObservabilityEventRequest):
    try:
        meeting.record_observability_event(
            r,
            meeting_id,
            body.token,
            body.kind,
            metrics=body.metrics,
            metadata=body.metadata,
        )
    except meeting.InvalidMeetingTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    return {"ok": True}


@app.post("/meetings/{meeting_id}/close")
def close_meeting(meeting_id: str):
    return meeting.close_meeting(r, meeting_id)


@app.websocket("/audio/{meeting_id}")
async def audio_stream(ws: WebSocket, meeting_id: str):
    await handle_audio_stream(r, ws, meeting_id)


@app.websocket("/event/{meeting_id}")
async def event_ws(ws: WebSocket, meeting_id: str):
    await handle_event_stream(r, ws, meeting_id)
