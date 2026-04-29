from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateMeetingResponse(BaseModel):
    meeting_id: str
    token: str
    event_url: str
    audio_url: str
    expires_in_seconds: int


class MeetingStatusResponse(BaseModel):
    meeting_id: str
    status: str
    expires_in_seconds: Optional[int] = None


class RefreshTokenRequest(BaseModel):
    token: str


class RefreshTokenResponse(BaseModel):
    token: str
    event_url: str
    audio_url: str
    expires_in_seconds: int


class ObservabilityEventRequest(BaseModel):
    token: str
    kind: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
