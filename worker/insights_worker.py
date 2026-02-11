from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Set, Tuple

import redis
import requests

from worker.prompts import build_insights_prompt, build_qa_prompt

# -----------------------
# Env / config
# -----------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

MODEL = os.getenv("INSIGHTS_MODEL", "gpt-4o-mini")

HISTORY_MAXLEN = int(os.getenv("INSIGHTS_HISTORY_MAXLEN", "12"))
CONTEXT_TAKE_LAST = int(os.getenv("INSIGHTS_CONTEXT_TAKE_LAST", "8"))

MIN_UTTERANCE_CHARS = int(os.getenv("INSIGHTS_MIN_UTTERANCE_CHARS", "12"))
MIN_NEW_TOKENS = int(os.getenv("INSIGHTS_MIN_NEW_TOKENS", "4"))
CONTEXT_SIMILARITY_THRESHOLD = float(os.getenv("INSIGHTS_CONTEXT_SIMILARITY_THRESHOLD", "0.92"))
SUMMARY_TOPIC_SHIFT_THRESHOLD = float(os.getenv("INSIGHTS_SUMMARY_TOPIC_SHIFT_THRESHOLD", "0.75"))

ACRONYM_MIN_LEN = int(os.getenv("INSIGHTS_ACRONYM_MIN_LEN", "2"))
ACRONYM_MAX_LEN = int(os.getenv("INSIGHTS_ACRONYM_MAX_LEN", "10"))

QUESTION_FORCE_MODEL = os.getenv("INSIGHTS_QUESTION_FORCE_MODEL", "true").lower() in ("1", "true", "yes")
QUESTION_MIN_LEN = int(os.getenv("INSIGHTS_QUESTION_MIN_LEN", "8"))

DEDUP_TTL_SECONDS = int(os.getenv("INSIGHTS_DEDUP_TTL_SECONDS", "120"))
QA_TTL_SECONDS = int(os.getenv("INSIGHTS_QA_TTL_SECONDS", str(DEDUP_TTL_SECONDS)))
TERMS_TTL_SECONDS = int(os.getenv("INSIGHTS_TERMS_TTL_SECONDS", str(DEDUP_TTL_SECONDS)))

EVENTS_STREAM_MAXLEN = int(os.getenv("EVENTS_STREAM_MAXLEN", "5000"))

# how long to keep meeting state without new transcripts
MEETING_STATE_IDLE_SECONDS = float(os.getenv("INSIGHTS_MEETING_STATE_IDLE_SECONDS", "600"))


# -----------------------
# Redis helpers
# -----------------------
def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def insights_channel(meeting_id: str) -> str:
    return f"insights:{meeting_id}"


def events_stream(meeting_id: str) -> str:
    return f"events:{meeting_id}"


def now_ts() -> int:
    return int(time.time())


def add_event_to_stream(r: redis.Redis, meeting_id: str, payload: Dict[str, Any]) -> None:
    try:
        s = json.dumps(payload, ensure_ascii=False)
        r.xadd(events_stream(meeting_id), {"payload": s}, maxlen=EVENTS_STREAM_MAXLEN, approximate=True)
    except Exception:
        pass


# -----------------------
# OpenAI
# -----------------------
def openai_chat(messages: list[dict]) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "temperature": 0.3}

    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def safe_parse_json(text: str) -> Dict[str, Any]:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# -----------------------
# Text utils / heuristics
# -----------------------
_word_re = re.compile(r"[A-Za-zÀ-ÿ0-9_]+", re.UNICODE)
_acronym_re = re.compile(r"\b[A-Z0-9]{2,10}\b")
_question_mark_re = re.compile(r"\?\s*$")

_question_starters = (
    "o que",
    "oq ",
    "como",
    "por que",
    "porque",
    "pra que",
    "qual",
    "quais",
    "quando",
    "onde",
    "quem",
    "quanto",
    "does ",
    "what ",
    "how ",
    "why ",
    "which ",
    "can ",
    "could ",
    "should ",
    "is ",
    "are ",
    "do ",
)

_decision_markers = (
    "decidimos",
    "fica decidido",
    "fechamos",
    "combinado",
    "alinhado",
    "vamos",
    "vamos fazer",
    "vamos seguir",
    "então fica",
    "a decisão",
    "decisão",
    "we will",
    "let's",
    "agreed",
)


def tokenize(text: str) -> Set[str]:
    tokens = set()
    for m in _word_re.finditer((text or "").lower()):
        tok = m.group(0).strip()
        if len(tok) <= 1:
            continue
        tokens.add(tok)
    return tokens


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def extract_acronyms(text: str) -> Set[str]:
    found = set()
    for m in _acronym_re.finditer(text or ""):
        ac = m.group(0)
        if ACRONYM_MIN_LEN <= len(ac) <= ACRONYM_MAX_LEN:
            found.add(ac)
    return found


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < QUESTION_MIN_LEN:
        return False
    low = t.lower()
    if _question_mark_re.search(t):
        return True
    return any(low.startswith(s) for s in _question_starters)


def has_decision_marker(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _decision_markers)


def normalize_str(x: Any) -> str:
    s = (x or "")
    if not isinstance(s, str):
        s = str(s)
    return " ".join(s.strip().split())


def normalize_insights_payload(insights: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"summary": normalize_str(insights.get("summary", "")), "key_points": [], "terms": [], "actions": []}

    kp = insights.get("key_points") or []
    if isinstance(kp, list):
        out["key_points"] = [normalize_str(x) for x in kp if normalize_str(x)]

    actions = insights.get("actions") or []
    if isinstance(actions, list):
        out["actions"] = [normalize_str(x) for x in actions if normalize_str(x)]

    terms = insights.get("terms") or []
    if isinstance(terms, list):
        clean_terms = []
        for t in terms:
            if not isinstance(t, dict):
                continue
            term = normalize_str(t.get("term", ""))
            explain = normalize_str(t.get("explain", ""))
            if term:
                clean_terms.append({"term": term, "explain": explain})
        out["terms"] = clean_terms

    return out


def normalize_qa_payload(qa: Dict[str, Any]) -> Dict[str, Any]:
    q = normalize_str(qa.get("question", ""))
    a = normalize_str(qa.get("answer", ""))
    short = normalize_str(qa.get("answer_short", ""))
    c = normalize_str(qa.get("confidence", ""))
    if c not in ("low", "medium", "high"):
        c = "low"
    return {"question": q, "answer": a, "answer_short": short, "confidence": c}


def hash_obj(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def is_effectively_empty_insights(insights: Dict[str, Any]) -> bool:
    summary = (insights.get("summary") or "").strip()
    key_points = insights.get("key_points") or []
    terms = insights.get("terms") or []
    actions = insights.get("actions") or []
    return not summary and not key_points and not terms and not actions


def redis_dedup_key(meeting_id: str, prefix: str, value_hash: str) -> str:
    return f"dedup:{meeting_id}:{prefix}:{value_hash}"


def redis_setnx_ttl(r: redis.Redis, key: str, ttl: int) -> bool:
    try:
        return bool(r.set(key, "1", nx=True, ex=ttl))
    except Exception:
        return True


def filter_terms_with_ttl(r: redis.Redis, meeting_id: str, terms: list[dict]) -> Tuple[list[dict], int]:
    kept: list[dict] = []
    removed = 0

    for t in terms:
        term = normalize_str(t.get("term", ""))
        if not term:
            continue

        h = hashlib.sha1(term.lower().encode("utf-8", errors="replace")).hexdigest()
        key = redis_dedup_key(meeting_id, "term", h)

        if redis_setnx_ttl(r, key, TERMS_TTL_SECONDS):
            kept.append(t)
        else:
            removed += 1

    return kept, removed


# -----------------------
# Per-meeting state
# -----------------------
@dataclass
class PerMeeting:
    meeting_id: str
    history: Deque[str] = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    last_context_tokens: Set[str] = field(default_factory=set)
    last_published_insight_hash: Optional[str] = None
    last_published_qa_hash: Optional[str] = None
    seen_acronyms: Set[str] = field(default_factory=set)
    last_activity_ts: float = field(default_factory=time.time)


# -----------------------
# Main
# -----------------------
def main():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    r = redis_client()
    pubsub = r.pubsub()

    # Pattern subscribe all meetings transcripts
    pubsub.psubscribe("transcript:*")

    meetings: Dict[str, PerMeeting] = {}

    print("✅ insights_router booted")
    print("📡 psubscribed to transcript:*")
    print(f"⚙️ model={MODEL} ttl={DEDUP_TTL_SECONDS}s")

    for msg in pubsub.listen():
        if msg["type"] not in ("pmessage", "message"):
            continue

        raw = msg.get("data")
        if not raw:
            continue

        try:
            evt = json.loads(raw)
        except Exception:
            continue

        meeting_id = evt.get("meeting_id")
        if not meeting_id:
            continue

        # keep/cleanup per-meeting state
        st = meetings.get(meeting_id)
        if not st:
            st = PerMeeting(meeting_id=meeting_id)
            meetings[meeting_id] = st

        st.last_activity_ts = time.time()

        # cleanup idle meetings occasionally
        if len(meetings) > 50:
            cutoff = time.time() - MEETING_STATE_IDLE_SECONDS
            for mid in list(meetings.keys()):
                if meetings[mid].last_activity_ts < cutoff:
                    meetings.pop(mid, None)

        # we only react to completed transcripts
        if evt.get("kind") != "transcript.completed" and evt.get("type") != "conversation.item.input_audio_transcription.completed":
            continue

        text = (evt.get("text") or "").strip()
        if not text or len(text) < MIN_UTTERANCE_CHARS:
            continue

        st.history.append(text)

        recent = list(st.history)[-CONTEXT_TAKE_LAST:]
        context = "\n".join(f"- {t}" for t in recent)

        # -----------------------
        # 1) Q&A
        # -----------------------
        if looks_like_question(text) and QUESTION_FORCE_MODEL:
            try:
                qa_messages = build_qa_prompt(context=context, question=text)
                qa_content = openai_chat(qa_messages)
                qa_raw = safe_parse_json(qa_content)
                qa_norm = normalize_qa_payload(qa_raw)

                if qa_norm.get("question") and qa_norm.get("answer"):
                    qa_hash = hash_obj(qa_norm)
                    qa_key = redis_dedup_key(meeting_id, "qa", qa_hash)

                    if redis_setnx_ttl(r, qa_key, QA_TTL_SECONDS):
                        if qa_hash != st.last_published_qa_hash:
                            payload = {
                                "meeting_id": meeting_id,
                                "ts": now_ts(),
                                "source_item_id": evt.get("item_id"),
                                "kind": "qa",
                                "qa": qa_norm,
                            }
                            r.publish(insights_channel(meeting_id), json.dumps(payload, ensure_ascii=False))
                            add_event_to_stream(r, meeting_id, payload)
                            st.last_published_qa_hash = qa_hash

            except Exception as e:
                print(f"❌ [{meeting_id}] QA error: {e}")

        # -----------------------
        # 2) heurística sem timer para insights
        # -----------------------
        acronyms_now = extract_acronyms(text)
        has_new_acronym = any(a not in st.seen_acronyms for a in acronyms_now)
        if acronyms_now:
            st.seen_acronyms |= acronyms_now

        ctx_tokens = tokenize(context)
        similarity = jaccard(ctx_tokens, st.last_context_tokens)
        new_tokens = len(ctx_tokens - st.last_context_tokens)

        should_call_insights = False
        if has_new_acronym:
            should_call_insights = True
        elif similarity < CONTEXT_SIMILARITY_THRESHOLD and new_tokens >= MIN_NEW_TOKENS:
            should_call_insights = True

        if not should_call_insights:
            st.last_context_tokens = ctx_tokens
            continue

        # -----------------------
        # 3) insights
        # -----------------------
        try:
            messages = build_insights_prompt(context=context, last_utterance=text)
            content = openai_chat(messages)
            insights_raw = safe_parse_json(content)
            insights_norm = normalize_insights_payload(insights_raw)

            topic_shift = similarity < SUMMARY_TOPIC_SHIFT_THRESHOLD
            decisionish = has_decision_marker(text)
            if not (topic_shift or decisionish):
                insights_norm["summary"] = ""
                insights_norm["key_points"] = []

            original_terms = insights_norm.get("terms") or []
            removed = 0
            if isinstance(original_terms, list) and original_terms:
                filtered_terms, removed = filter_terms_with_ttl(r, meeting_id, original_terms)
                insights_norm["terms"] = filtered_terms

            if is_effectively_empty_insights(insights_norm):
                st.last_context_tokens = ctx_tokens
                continue

            insight_hash = hash_obj(insights_norm)

            insight_key = redis_dedup_key(meeting_id, "insight", insight_hash)
            if not redis_setnx_ttl(r, insight_key, DEDUP_TTL_SECONDS):
                st.last_context_tokens = ctx_tokens
                continue

            if insight_hash == st.last_published_insight_hash:
                st.last_context_tokens = ctx_tokens
                continue

            payload = {
                "meeting_id": meeting_id,
                "ts": now_ts(),
                "source_item_id": evt.get("item_id"),
                "kind": "insight",
                "insights": insights_norm,
                "debug": {
                    "similarity": round(similarity, 3),
                    "new_tokens": new_tokens,
                    "new_acronym": has_new_acronym,
                    "topic_shift": topic_shift,
                    "decision_marker": decisionish,
                    "terms_removed_ttl": removed,
                },
            }

            r.publish(insights_channel(meeting_id), json.dumps(payload, ensure_ascii=False))
            add_event_to_stream(r, meeting_id, payload)

            st.last_published_insight_hash = insight_hash
            st.last_context_tokens = ctx_tokens

        except Exception as e:
            print(f"❌ [{meeting_id}] insights error: {e}")


if __name__ == "__main__":
    main()
