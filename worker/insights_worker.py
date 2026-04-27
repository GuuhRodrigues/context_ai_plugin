from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Set, Tuple

import redis
import requests

from backend.observability import now_ms as obs_now_ms
from worker.prompts import build_insights_prompt, build_qa_prompt

# -----------------------
# Env / config
# -----------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

MODEL = os.getenv("INSIGHTS_MODEL", "gpt-4o-mini")
INSIGHTS_MAX_OUTPUT_TOKENS = int(os.getenv("INSIGHTS_MAX_OUTPUT_TOKENS", "180"))
QA_MAX_OUTPUT_TOKENS = int(os.getenv("QA_MAX_OUTPUT_TOKENS", "220"))
INSIGHTS_INPUT_COST_PER_1M_USD = float(os.getenv("INSIGHTS_INPUT_COST_PER_1M_USD", "0.15"))
INSIGHTS_OUTPUT_COST_PER_1M_USD = float(os.getenv("INSIGHTS_OUTPUT_COST_PER_1M_USD", "0.60"))

HISTORY_MAXLEN = int(os.getenv("INSIGHTS_HISTORY_MAXLEN", "12"))
CONTEXT_TAKE_LAST = int(os.getenv("INSIGHTS_CONTEXT_TAKE_LAST", "8"))

MIN_UTTERANCE_CHARS = int(os.getenv("INSIGHTS_MIN_UTTERANCE_CHARS", "12"))
MIN_NEW_TOKENS = int(os.getenv("INSIGHTS_MIN_NEW_TOKENS", "8"))
CONTEXT_SIMILARITY_THRESHOLD = float(os.getenv("INSIGHTS_CONTEXT_SIMILARITY_THRESHOLD", "0.85"))
SUMMARY_TOPIC_SHIFT_THRESHOLD = float(os.getenv("INSIGHTS_SUMMARY_TOPIC_SHIFT_THRESHOLD", "0.75"))

ACRONYM_MIN_LEN = int(os.getenv("INSIGHTS_ACRONYM_MIN_LEN", "2"))
ACRONYM_MAX_LEN = int(os.getenv("INSIGHTS_ACRONYM_MAX_LEN", "10"))

QUESTION_FORCE_MODEL = os.getenv("INSIGHTS_QUESTION_FORCE_MODEL", "true").lower() in ("1", "true", "yes")
QUESTION_MIN_LEN = int(os.getenv("INSIGHTS_QUESTION_MIN_LEN", "8"))
QUESTION_MIN_WORDS = int(os.getenv("INSIGHTS_QUESTION_MIN_WORDS", "4"))
QA_MIN_INTERVAL_SECONDS = float(os.getenv("INSIGHTS_QA_MIN_INTERVAL_SECONDS", "1.5"))
QUESTION_ASSEMBLY_WINDOW_SECONDS = float(os.getenv("INSIGHTS_QUESTION_ASSEMBLY_WINDOW_SECONDS", "5.0"))
QUESTION_ASSEMBLY_MAX_PARTS = int(os.getenv("INSIGHTS_QUESTION_ASSEMBLY_MAX_PARTS", "4"))
QA_WORKERS = int(os.getenv("INSIGHTS_QA_WORKERS", "2"))
INSIGHT_WORKERS = int(os.getenv("INSIGHTS_INSIGHT_WORKERS", "1"))
INSIGHTS_PENDING_LIMIT_PER_MEETING = int(os.getenv("INSIGHTS_PENDING_LIMIT_PER_MEETING", "1"))
INSIGHTS_BACKGROUND_DELAY_SECONDS = float(os.getenv("INSIGHTS_BACKGROUND_DELAY_SECONDS", "0.5"))

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


def event_ts_ms(evt: Dict[str, Any]) -> Optional[int]:
    timings = evt.get("timings")
    if isinstance(timings, dict):
        value = timings.get("transcript_event_received_at_ms") or timings.get("published_at_ms")
        if isinstance(value, (int, float)):
            return int(value)

    value = evt.get("ts_ms")
    if isinstance(value, (int, float)):
        return int(value)

    ts = evt.get("ts")
    if isinstance(ts, (int, float)):
        return int(ts * 1000)

    return None


def add_ai_call_event(
    r: redis.Redis,
    meeting_id: str,
    purpose: str,
    result: Optional[Dict[str, Any]],
    output_status: str,
    source_item_id: Optional[str],
    transcript_completed_at_ms: Optional[int],
    model_request_started_at_ms: int,
    published_at_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    result = result or {}
    response_received_at_ms = model_request_started_at_ms + int(result.get("latency_ms") or 0)
    metrics = {
        "purpose": purpose,
        "output_status": output_status,
        "model": result.get("model") or MODEL,
        "usage": result.get("usage") or {},
        "model_latency_ms": result.get("latency_ms"),
        "estimated_cost_usd": result.get("estimated_cost_usd", 0),
    }
    if transcript_completed_at_ms and published_at_ms:
        metrics["transcript_to_publish_ms"] = published_at_ms - transcript_completed_at_ms
    if error:
        metrics["error"] = error

    payload = {
        "meeting_id": meeting_id,
        "ts": now_ts(),
        "ts_ms": obs_now_ms(),
        "kind": "observability.ai_call",
        "source": "insights_worker",
        "source_item_id": source_item_id,
        "metrics": metrics,
        "timings": {
            "transcript_completed_at_ms": transcript_completed_at_ms,
            "model_request_started_at_ms": model_request_started_at_ms,
            "model_response_received_at_ms": response_received_at_ms,
            "published_at_ms": published_at_ms,
        },
    }
    add_event_to_stream(r, meeting_id, payload)


# -----------------------
# OpenAI
# -----------------------
def estimate_chat_cost_usd(usage: Dict[str, Any]) -> float:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    input_cost = (prompt_tokens / 1_000_000) * INSIGHTS_INPUT_COST_PER_1M_USD
    output_cost = (completion_tokens / 1_000_000) * INSIGHTS_OUTPUT_COST_PER_1M_USD
    return input_cost + output_cost


def openai_chat(messages: list[dict], max_tokens: Optional[int] = None) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
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
        "model": data.get("model") or MODEL,
        "latency_ms": latency_ms,
        "estimated_cost_usd": estimate_chat_cost_usd(usage),
    }


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
    "e como",
    "por que",
    "e por que",
    "porque",
    "e porque",
    "pra que",
    "qual",
    "e qual",
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

_non_question_starters = (
    "como proxima acao",
    "como próxima ação",
    "como proxima etapa",
    "como próxima etapa",
)

_smalltalk_question_patterns = (
    "ola",
    "olá",
    "tudo bem",
    "como vai",
    "como voce esta",
    "como você está",
)

_decision_markers = (
    "decidimos",
    "fica decidido",
    "ficou decidido",
    "fechamos",
    "combinado que",
    "alinhado que",
    "vamos manter",
    "vamos adotar",
    "vamos seguir com",
    "então fica",
    "a decisão",
    "decisão",
    "we will",
    "let's",
    "agreed",
)

_decision_noise_patterns = (
    "vamos revisar",
    "sem adicionar banco de dados ne",
    "sem adicionar banco de dados né",
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


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )


def canonical_question(text: str) -> str:
    low = strip_accents((text or "").lower())
    replacements = {
        "vadi": "vad",
        "red pubsub": "redis pubsub",
        "reds pubsub": "redis pubsub",
        "pub sub": "pubsub",
    }
    for src, dst in replacements.items():
        low = low.replace(src, dst)
    low = re.sub(r"[^a-z0-9]+", " ", low)
    return " ".join(low.split())


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < QUESTION_MIN_LEN:
        return False
    low = t.lower()
    low_ascii = strip_accents(low)
    if any(low_ascii.startswith(s) for s in _non_question_starters):
        return False
    if low_ascii.endswith("ne?") and not any(low.startswith(s) for s in _question_starters):
        return False
    if _question_mark_re.search(t):
        return True
    return any(low.startswith(s) for s in _question_starters)


def is_actionable_question(text: str) -> bool:
    if not looks_like_question(text):
        return False

    words = [w for w in _word_re.findall((text or "").lower()) if w]
    if len(words) < QUESTION_MIN_WORDS:
        return False

    low = (text or "").lower()
    return not any(p in low for p in _smalltalk_question_patterns)


def has_decision_marker(text: str) -> bool:
    low = (text or "").lower()
    low_ascii = strip_accents(low)
    if any(p in low_ascii for p in _decision_noise_patterns):
        return False
    return any(m in low for m in _decision_markers) or any(strip_accents(m) in low_ascii for m in _decision_markers)


def normalize_str(x: Any) -> str:
    s = (x or "")
    if not isinstance(s, str):
        s = str(s)
    return " ".join(s.strip().split())


def looks_like_question_continuation(text: str) -> bool:
    low = strip_accents((text or "").strip().lower())
    return low.startswith(("e por que", "e porque", "e como", "e qual", "por que", "porque"))


def assemble_question_candidate(recent_turns: Deque[Tuple[float, str]]) -> str:
    if not recent_turns:
        return ""

    now = recent_turns[-1][0]
    parts: list[str] = []
    for ts, text in reversed(recent_turns):
        if now - ts > QUESTION_ASSEMBLY_WINDOW_SECONDS:
            break
        clean = normalize_str(text)
        if clean:
            parts.append(clean)
        if len(parts) >= QUESTION_ASSEMBLY_MAX_PARTS:
            break

    parts.reverse()
    if not parts:
        return ""

    current = parts[-1]
    if is_actionable_question(current):
        if len(parts) == 1:
            return current
        useful_prev = [
            p for p in parts[:-1]
            if len(p.split()) <= 8 or looks_like_question(p)
        ]
        if useful_prev and (looks_like_question_continuation(current) or any("?" in p for p in useful_prev)):
            return " ".join(useful_prev + [current])
        return current

    if looks_like_question_continuation(current) and len(parts) > 1:
        candidate = " ".join(parts)
        if is_actionable_question(candidate):
            return candidate

    return ""


def normalize_insights_payload(insights: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "summary": normalize_str(insights.get("summary", "")),
        "key_points": [],
        "terms": [],
        "actions": [],
        "decisions": [],
    }

    kp = insights.get("key_points") or []
    if isinstance(kp, list):
        out["key_points"] = [normalize_str(x) for x in kp if normalize_str(x)]

    actions = insights.get("actions") or []
    if isinstance(actions, list):
        out["actions"] = [normalize_str(x) for x in actions if normalize_str(x)]

    decisions = insights.get("decisions") or []
    if isinstance(decisions, list):
        out["decisions"] = [
            normalize_str(x)
            for x in decisions
            if normalize_str(x) and has_decision_marker(normalize_str(x))
        ]

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


def build_fallback_qa(question: str) -> Dict[str, Any]:
    q = normalize_str(question)
    return {
        "question": q,
        "answer": "Pode repetir em uma frase curta? Nao captei contexto suficiente para responder com seguranca.",
        "answer_short": "Pode repetir de forma mais direta?",
        "confidence": "low",
    }


def hash_obj(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def is_effectively_empty_insights(insights: Dict[str, Any]) -> bool:
    summary = (insights.get("summary") or "").strip()
    key_points = insights.get("key_points") or []
    terms = insights.get("terms") or []
    actions = insights.get("actions") or []
    decisions = insights.get("decisions") or []
    return not summary and not key_points and not terms and not actions and not decisions


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
    recent_turns: Deque[Tuple[float, str]] = field(default_factory=lambda: deque(maxlen=12))
    last_context_tokens: Set[str] = field(default_factory=set)
    last_published_insight_hash: Optional[str] = None
    seen_acronyms: Set[str] = field(default_factory=set)
    last_activity_ts: float = field(default_factory=time.time)
    last_qa_ts: float = 0.0
    pending_qa: int = 0
    pending_insights: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


# -----------------------
# Background model tasks
# -----------------------
def process_qa_task(
    meeting_id: str,
    source_item_id: Optional[str],
    text: str,
    context: str,
    transcript_completed_at_ms: Optional[int],
    st: PerMeeting,
    pre_reserved: bool = False,
) -> None:
    r = redis_client()
    qa_result: Optional[Dict[str, Any]] = None
    model_request_started_at_ms = obs_now_ms()
    output_status = "error"
    published_at_ms: Optional[int] = None

    try:
        qa_messages = build_qa_prompt(context=context, question=text)
        model_request_started_at_ms = obs_now_ms()
        qa_result = openai_chat(qa_messages, max_tokens=QA_MAX_OUTPUT_TOKENS)
        qa_content = qa_result["content"]
        qa_raw = safe_parse_json(qa_content)
        qa_norm = normalize_qa_payload(qa_raw)
        used_fallback = False
        if not qa_norm.get("question") or not qa_norm.get("answer"):
            qa_norm = build_fallback_qa(text)
            used_fallback = True

        q_fingerprint = hashlib.sha1(
            canonical_question(qa_norm.get("question", "")).encode("utf-8", errors="replace")
        ).hexdigest()
        qa_key = redis_dedup_key(meeting_id, "qa_question", q_fingerprint)

        if pre_reserved or redis_setnx_ttl(r, qa_key, QA_TTL_SECONDS):
            published_at_ms = obs_now_ms()
            payload = {
                "meeting_id": meeting_id,
                "ts": now_ts(),
                "ts_ms": published_at_ms,
                "source_item_id": source_item_id,
                "kind": "qa",
                "qa": qa_norm,
                "debug": {
                    "fallback_used": used_fallback,
                },
                "timings": {
                    "transcript_completed_at_ms": transcript_completed_at_ms,
                    "model_request_started_at_ms": model_request_started_at_ms,
                    "model_response_received_at_ms": model_request_started_at_ms + int(qa_result.get("latency_ms") or 0),
                    "published_at_ms": published_at_ms,
                },
            }
            r.publish(insights_channel(meeting_id), json.dumps(payload, ensure_ascii=False))
            add_event_to_stream(r, meeting_id, payload)
            output_status = "fallback_published" if used_fallback else "published"
        else:
            output_status = "deduped"

        add_ai_call_event(
            r,
            meeting_id,
            "qa",
            qa_result,
            output_status,
            source_item_id,
            transcript_completed_at_ms,
            model_request_started_at_ms,
            published_at_ms,
        )

    except Exception as e:
        add_ai_call_event(
            r,
            meeting_id,
            "qa",
            qa_result,
            output_status,
            source_item_id,
            transcript_completed_at_ms,
            model_request_started_at_ms,
            error=str(e),
        )
        print(f"❌ [{meeting_id}] QA error: {e}")
    finally:
        with st.lock:
            st.pending_qa = max(0, st.pending_qa - 1)


def process_insight_task(
    meeting_id: str,
    source_item_id: Optional[str],
    text: str,
    context: str,
    transcript_completed_at_ms: Optional[int],
    similarity: float,
    new_tokens: int,
    has_new_acronym: bool,
    topic_shift: bool,
    decisionish: bool,
    st: PerMeeting,
) -> None:
    r = redis_client()
    insights_result: Optional[Dict[str, Any]] = None
    model_request_started_at_ms = obs_now_ms()
    output_status = "error"

    try:
        if INSIGHTS_BACKGROUND_DELAY_SECONDS > 0:
            time.sleep(INSIGHTS_BACKGROUND_DELAY_SECONDS)

        messages = build_insights_prompt(context=context, last_utterance=text)
        model_request_started_at_ms = obs_now_ms()
        insights_result = openai_chat(messages, max_tokens=INSIGHTS_MAX_OUTPUT_TOKENS)
        content = insights_result["content"]
        insights_raw = safe_parse_json(content)
        insights_norm = normalize_insights_payload(insights_raw)
        if not insights_raw:
            print(f"⚠️ [{meeting_id}] insights parse returned empty JSON")

        if not (topic_shift or decisionish):
            insights_norm["summary"] = ""
            insights_norm["key_points"] = []

        if decisionish and not insights_norm.get("decisions"):
            insights_norm["decisions"] = [text]

        original_terms = insights_norm.get("terms") or []
        removed = 0
        if isinstance(original_terms, list) and original_terms:
            filtered_terms, removed = filter_terms_with_ttl(r, meeting_id, original_terms)
            insights_norm["terms"] = filtered_terms

        if is_effectively_empty_insights(insights_norm):
            add_ai_call_event(
                r,
                meeting_id,
                "insight",
                insights_result,
                "empty_after_filters",
                source_item_id,
                transcript_completed_at_ms,
                model_request_started_at_ms,
            )
            return

        insight_hash = hash_obj(insights_norm)
        insight_key = redis_dedup_key(meeting_id, "insight", insight_hash)
        if not redis_setnx_ttl(r, insight_key, DEDUP_TTL_SECONDS):
            add_ai_call_event(
                r,
                meeting_id,
                "insight",
                insights_result,
                "deduped",
                source_item_id,
                transcript_completed_at_ms,
                model_request_started_at_ms,
            )
            return

        with st.lock:
            duplicate_last = insight_hash == st.last_published_insight_hash

        if duplicate_last:
            add_ai_call_event(
                r,
                meeting_id,
                "insight",
                insights_result,
                "duplicate_last",
                source_item_id,
                transcript_completed_at_ms,
                model_request_started_at_ms,
            )
            return

        published_at_ms = obs_now_ms()
        payload = {
            "meeting_id": meeting_id,
            "ts": now_ts(),
            "ts_ms": published_at_ms,
            "source_item_id": source_item_id,
            "kind": "insight",
            "insights": insights_norm,
            "timings": {
                "transcript_completed_at_ms": transcript_completed_at_ms,
                "model_request_started_at_ms": model_request_started_at_ms,
                "model_response_received_at_ms": model_request_started_at_ms + int(insights_result.get("latency_ms") or 0),
                "published_at_ms": published_at_ms,
            },
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
        add_ai_call_event(
            r,
            meeting_id,
            "insight",
            insights_result,
            "published",
            source_item_id,
            transcript_completed_at_ms,
            model_request_started_at_ms,
            published_at_ms,
        )

        with st.lock:
            st.last_published_insight_hash = insight_hash

    except Exception as e:
        add_ai_call_event(
            r,
            meeting_id,
            "insight",
            insights_result,
            output_status,
            source_item_id,
            transcript_completed_at_ms,
            model_request_started_at_ms,
            error=str(e),
        )
        print(f"❌ [{meeting_id}] insights error: {e}")
    finally:
        with st.lock:
            st.pending_insights = max(0, st.pending_insights - 1)


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
    print(
        f"⚙️ model={MODEL} ttl={DEDUP_TTL_SECONDS}s "
        f"qa_workers={QA_WORKERS} insight_workers={INSIGHT_WORKERS}"
    )

    qa_executor = ThreadPoolExecutor(max_workers=max(1, QA_WORKERS), thread_name_prefix="qa")
    insight_executor = ThreadPoolExecutor(max_workers=max(1, INSIGHT_WORKERS), thread_name_prefix="insight")

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
        if not text:
            continue

        st.recent_turns.append((time.time(), text))
        st.history.append(text)

        recent = list(st.history)[-CONTEXT_TAKE_LAST:]
        context = "\n".join(f"- {t}" for t in recent)

        # -----------------------
        # 1) Q&A
        # -----------------------
        question_text = assemble_question_candidate(st.recent_turns)
        if question_text and QUESTION_FORCE_MODEL:
            transcript_completed_at_ms = event_ts_ms(evt)
            ctx_tokens = tokenize(context)
            now = time.time()
            q_fingerprint = hashlib.sha1(
                canonical_question(question_text).encode("utf-8", errors="replace")
            ).hexdigest()
            qa_key = redis_dedup_key(meeting_id, "qa_question", q_fingerprint)
            with st.lock:
                if (now - st.last_qa_ts) < QA_MIN_INTERVAL_SECONDS:
                    st.last_context_tokens = ctx_tokens
                    continue
                if not redis_setnx_ttl(r, qa_key, QA_TTL_SECONDS):
                    st.last_context_tokens = ctx_tokens
                    continue
                st.last_qa_ts = now
                st.pending_qa += 1
                st.last_context_tokens = ctx_tokens

            qa_executor.submit(
                process_qa_task,
                meeting_id,
                evt.get("item_id"),
                question_text,
                context,
                transcript_completed_at_ms,
                st,
                True,
            )

            # For question turns, enqueue immediate QA and skip a second model call for insights.
            continue

        if len(text) < MIN_UTTERANCE_CHARS:
            continue

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

        decisionish = has_decision_marker(text)
        should_call_insights = False
        if has_new_acronym:
            should_call_insights = True
        elif decisionish:
            should_call_insights = True
        elif similarity < CONTEXT_SIMILARITY_THRESHOLD and new_tokens >= MIN_NEW_TOKENS:
            should_call_insights = True

        if not should_call_insights:
            with st.lock:
                st.last_context_tokens = ctx_tokens
            continue

        # -----------------------
        # 3) insights
        # -----------------------
        transcript_completed_at_ms = event_ts_ms(evt)
        topic_shift = similarity < SUMMARY_TOPIC_SHIFT_THRESHOLD
        with st.lock:
            st.last_context_tokens = ctx_tokens
            if st.pending_insights >= INSIGHTS_PENDING_LIMIT_PER_MEETING:
                continue
            st.pending_insights += 1

        insight_executor.submit(
            process_insight_task,
            meeting_id,
            evt.get("item_id"),
            text,
            context,
            transcript_completed_at_ms,
            similarity,
            new_tokens,
            has_new_acronym,
            topic_shift,
            decisionish,
            st,
        )


if __name__ == "__main__":
    main()
