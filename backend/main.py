from __future__ import annotations

from fastapi import FastAPI

from backend.src.routes import meetings, websockets

app = FastAPI()
app.include_router(meetings.router)
app.include_router(websockets.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "context-ai", "status": "running"}
