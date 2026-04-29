from __future__ import annotations

import json
import time
from typing import Any, Optional

import requests
import websockets

from backend.configs import (
    INSIGHTS_INPUT_COST_PER_1M_USD,
    INSIGHTS_MODEL,
    INSIGHTS_OUTPUT_COST_PER_1M_USD,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_REALTIME_URL,
    TRANSCRIBE_LANGUAGE,
    TRANSCRIBE_MODEL,
    TRANSCRIPTION_SAMPLE_RATE,
    TURN_DETECTION_THRESHOLD,
    TURN_PREFIX_PADDING_MS,
    TURN_SILENCE_DURATION_MS,
)


def estimate_chat_cost_usd(usage: dict[str, Any]) -> float:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    input_cost = (prompt_tokens / 1_000_000) * INSIGHTS_INPUT_COST_PER_1M_USD
    output_cost = (completion_tokens / 1_000_000) * INSIGHTS_OUTPUT_COST_PER_1M_USD
    return input_cost + output_cost


def openai_chat(messages: list[dict], max_tokens: Optional[int] = None) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": INSIGHTS_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    started = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage") or {}
    return {
        "content": data["choices"][0]["message"]["content"],
        "usage": usage,
        "model": data.get("model") or INSIGHTS_MODEL,
        "latency_ms": latency_ms,
        "estimated_cost_usd": estimate_chat_cost_usd(usage),
    }


async def openai_ws():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente no ambiente")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    return await websockets.connect(
        OPENAI_REALTIME_URL,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=10,
        max_size=None,
    )


async def send_event(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload))


async def configure_transcription_session(ws) -> None:
    await send_event(
        ws,
        {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": TRANSCRIPTION_SAMPLE_RATE},
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {"model": TRANSCRIBE_MODEL, "language": TRANSCRIBE_LANGUAGE},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": TURN_DETECTION_THRESHOLD,
                            "prefix_padding_ms": TURN_PREFIX_PADDING_MS,
                            "silence_duration_ms": TURN_SILENCE_DURATION_MS,
                        },
                    }
                },
            },
        },
    )
