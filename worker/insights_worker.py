from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Deque, Dict, Any, Optional

import redis
import requests

from worker.prompts import build_insights_prompt

# -----------------------
# Env / config
# -----------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

MEETING_ID = os.getenv("MEETING_ID", "test-meeting-1")

TRANSCRIPT_CHANNEL = f"transcript:{MEETING_ID}"
INSIGHTS_CHANNEL = f"insights:{MEETING_ID}"

MODEL = os.getenv("INSIGHTS_MODEL", "gpt-4o-mini")

# Evita custo/ruído: só gerar insight no máximo a cada X segundos
MIN_SECONDS_BETWEEN_INSIGHTS = float(os.getenv("INSIGHTS_DEBOUNCE_SECONDS", "5"))

# Quantas falas completas manter como contexto (rolling window)
HISTORY_MAXLEN = int(os.getenv("INSIGHTS_HISTORY_MAXLEN", "12"))
CONTEXT_TAKE_LAST = int(os.getenv("INSIGHTS_CONTEXT_TAKE_LAST", "8"))


# -----------------------
# Helpers
# -----------------------
def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def openai_chat(messages: list[dict]) -> str:
    """
    Chama Chat Completions (texto) para gerar insights.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def safe_parse_json(text: str) -> Dict[str, Any]:
    """
    Garante que sempre devolvemos um dict.
    Se o modelo quebrar o JSON, faz fallback seguro.
    """
    try:
        return json.loads(text)
    except Exception:
        # fallback mínimo
        return {
            "summary": (text or "").strip()[:220],
            "key_points": [],
            "terms": [],
            "actions": [],
        }


def is_completed_transcript(evt: Dict[str, Any]) -> bool:
    return evt.get("type") == "conversation.item.input_audio_transcription.completed"


# -----------------------
# Main
# -----------------------
def main():
    r = redis_client()

    pubsub = r.pubsub()
    pubsub.subscribe(TRANSCRIPT_CHANNEL)

    history: Deque[str] = deque(maxlen=HISTORY_MAXLEN)
    last_insight_ts: float = 0.0

    print(f"✅ insights_worker booted | meeting={MEETING_ID}")
    print(f"📡 subscribed to {TRANSCRIPT_CHANNEL}")
    print(f"🧠 publishing insights to {INSIGHTS_CHANNEL}")
    print(
        f"⚙️ model={MODEL} debounce={MIN_SECONDS_BETWEEN_INSIGHTS}s history={HISTORY_MAXLEN} context_last={CONTEXT_TAKE_LAST}"
    )

    for msg in pubsub.listen():
        if msg["type"] != "message":
            continue

        raw = msg["data"]
        if not raw:
            continue

        try:
            evt = json.loads(raw)
        except Exception:
            continue

        if not is_completed_transcript(evt):
            continue

        text = (evt.get("text") or "").strip()
        if not text:
            continue

        history.append(text)

        now = time.time()
        if (now - last_insight_ts) < MIN_SECONDS_BETWEEN_INSIGHTS:
            # Debounce: guarda contexto, mas não chama o modelo agora
            continue

        # Monta contexto: últimas N falas completas
        recent = list(history)[-CONTEXT_TAKE_LAST:]
        context = "\n".join(f"- {t}" for t in recent)

        messages = build_insights_prompt(context=context, last_utterance=text)

        try:
            content = openai_chat(messages)
            insights = safe_parse_json(content)

            out: Dict[str, Any] = {
                "meeting_id": MEETING_ID,
                "ts": int(now),
                "source_item_id": evt.get("item_id"),
                "insights": insights,
            }

            r.publish(INSIGHTS_CHANNEL, json.dumps(out, ensure_ascii=False))
            last_insight_ts = now
            print("✨ insights published")

        except Exception as e:
            print("❌ insights error:", e)


if __name__ == "__main__":
    main()
