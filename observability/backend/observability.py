from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime
from typing import Any, Optional

import redis

from backend.configs import OBSERVABILITY_ROOT, TRANSCRIPTION_COST_PER_MINUTE_USD

_word_re = re.compile(r"\S+")
_decision_markers = (
    "fica decidido",
    "ficou decidido",
    "decidimos",
    "decidido que",
    "vamos manter",
    "vamos adotar",
    "vamos seguir com",
    "sem adicionar banco",
    "a decisão",
    "decisão",
    "combinado",
    "alinhado",
    "agreed",
    "decided",
)

_decision_noise_patterns = (
    "vamos revisar",
    "pessoal vamos revisar",
    "deisao",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def now_ts() -> int:
    return int(time.time())


def decode_if_bytes(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def filename_timestamp(dt: Optional[datetime] = None) -> str:
    # Windows does not allow ":" in file names, so use underscores for time too.
    return (dt or datetime.now()).strftime("%Y_%m_%d_%H_%M_%S")


def events_stream_key(meeting_id: str) -> str:
    return f"events:{meeting_id}"


def counters_key(meeting_id: str) -> str:
    return f"observability:{meeting_id}:counters"


def add_event_to_stream(
    r: redis.Redis,
    meeting_id: str,
    payload: dict[str, Any],
    maxlen: int = 5000,
) -> None:
    try:
        r.xadd(
            events_stream_key(meeting_id),
            {"payload": json.dumps(payload, ensure_ascii=False)},
            maxlen=maxlen,
            approximate=True,
        )
    except Exception:
        pass


def add_observability_event(
    r: redis.Redis,
    meeting_id: str,
    event_type: str,
    metrics: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    source: str = "backend",
    maxlen: int = 5000,
) -> dict[str, Any]:
    payload = {
        "meeting_id": meeting_id,
        "ts": now_ts(),
        "ts_ms": now_ms(),
        "kind": f"observability.{event_type}",
        "source": source,
        "metrics": metrics or {},
        "metadata": metadata or {},
    }
    add_event_to_stream(r, meeting_id, payload, maxlen=maxlen)
    return payload


def increment_counter(r: redis.Redis, meeting_id: str, field: str, value: int = 1) -> None:
    try:
        r.hincrby(counters_key(meeting_id), field, value)
    except Exception:
        pass


def increment_float_counter(r: redis.Redis, meeting_id: str, field: str, value: float) -> None:
    try:
        r.hincrbyfloat(counters_key(meeting_id), field, value)
    except Exception:
        pass


def _parse_number(value: Any) -> Any:
    value = decode_if_bytes(value)
    if value is None:
        return 0
    text = str(value)
    try:
        if "." in text:
            return float(text)
        return int(text)
    except Exception:
        try:
            return float(text)
        except Exception:
            return value


def get_counters(r: redis.Redis, meeting_id: str) -> dict[str, Any]:
    try:
        raw = r.hgetall(counters_key(meeting_id))
    except Exception:
        return {}
    return {str(decode_if_bytes(k)): _parse_number(v) for k, v in raw.items()}


def read_meeting_events(r: redis.Redis, meeting_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        entries = r.xrange(events_stream_key(meeting_id), min="-", max="+")
    except Exception:
        return events

    for entry_id, fields in entries:
        payload_raw = None
        if isinstance(fields, dict):
            payload_raw = fields.get(b"payload") or fields.get("payload")
        payload_raw = decode_if_bytes(payload_raw)
        if not payload_raw:
            continue

        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {"kind": "unparsed", "raw": payload_raw}

        payload["_redis_id"] = decode_if_bytes(entry_id)
        events.append(payload)

    return events


def _event_ms(event: dict[str, Any]) -> Optional[int]:
    for key in ("ts_ms", "published_at_ms"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return int(value)

    timings = event.get("timings")
    if isinstance(timings, dict):
        for key in (
            "published_at_ms",
            "transcript_completed_at_ms",
            "transcript_event_received_at_ms",
            "model_response_received_at_ms",
        ):
            value = timings.get(key)
            if isinstance(value, (int, float)):
                return int(value)

    ts = event.get("ts")
    if isinstance(ts, (int, float)):
        return int(ts * 1000)

    redis_id = event.get("_redis_id")
    if isinstance(redis_id, str) and "-" in redis_id:
        try:
            return int(redis_id.split("-", 1)[0])
        except Exception:
            return None

    return None


def _iso_from_ms(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000).isoformat(timespec="seconds")


def _word_count(text: str) -> int:
    return len(_word_re.findall(text or ""))


def _clean_text(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text or "")
    return " ".join(text.strip().split())


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )


def _looks_like_decision(text: str) -> bool:
    low = (text or "").lower()
    low_ascii = _strip_accents(low)
    if any(pattern in low_ascii for pattern in _decision_noise_patterns):
        return False
    if low_ascii in ("sem adicionar banco de dados", "sem adicionar banco de dados ne"):
        return False
    return any(marker in low for marker in _decision_markers) or any(
        _strip_accents(marker) in low_ascii for marker in _decision_markers
    )


def _clip_decision_tail(text: str) -> str:
    text = _clean_text(text)
    if not text:
        return ""

    stop_patterns = (
        r"\s+e\s+a\s+pr[oó]xima\s+etapa\b",
        r"\s+e\s+como\s+pr[oó]xima\s+a[cç][aã]o\b",
        r"\s+como\s+pr[oó]xima\s+a[cç][aã]o\b",
        r"\s+e\s+precisamos\b",
        r"\s+precisamos\b",
    )
    for pattern in stop_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.start() > 12:
            text = text[: match.start()]
            break

    sentence_match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    if sentence_match:
        text = sentence_match.group(1)

    return _clean_text(text).rstrip(" ,;:")


def _extract_decision_clause(text: Any) -> str:
    original = _clean_text(text)
    if not original or not _looks_like_decision(original):
        return ""

    patterns = (
        (r"\b(?:fica|ficou)\s+decidido\s+que\s*,?\s*(.+)", ""),
        (r"\bdecidimos\s+(.+)", "Decidimos "),
        (r"\b(vamos\s+(?:manter|adotar|seguir\s+com)\b.+)", ""),
        (r"\b(a\s+decis[aã]o\s+(?:e|é|foi)\b.+)", ""),
    )
    for pattern, prefix in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE)
        if not match:
            continue
        clause = _clip_decision_tail(match.group(1))
        clause = re.sub(r"^(?:por enquanto,?\s*)+", "", clause, flags=re.IGNORECASE)
        if not clause:
            continue
        if prefix and not clause.lower().startswith(prefix.strip().lower()):
            clause = prefix + clause
        if _looks_like_decision(clause):
            return clause[:1].upper() + clause[1:]

    fallback = _clip_decision_tail(original)
    return fallback[:1].upper() + fallback[1:] if fallback else ""


def _append_unique(items: list[str], seen: set[str], value: Any) -> None:
    text = _clean_text(value)
    if not text:
        return
    key = text.lower()
    if key in seen:
        return
    for existing in items:
        existing_key = existing.lower()
        if len(key) > 10 and (key in existing_key or existing_key in key):
            return
    seen.add(key)
    items.append(text)


def _collect_decisions(transcript_events: list[dict[str, Any]], insight_events: list[dict[str, Any]]) -> list[str]:
    decisions: list[str] = []
    seen: set[str] = set()

    for event in insight_events:
        insights = event.get("insights") if isinstance(event.get("insights"), dict) else {}
        for decision in insights.get("decisions") or []:
            clause = _extract_decision_clause(decision)
            if clause:
                _append_unique(decisions, seen, clause)

    for event in transcript_events:
        text = _clean_text(event.get("text") or event.get("transcript") or "")
        clause = _extract_decision_clause(text)
        if clause:
            _append_unique(decisions, seen, clause)

    return decisions


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _percent(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100, 2)


def _summarize(values: list[float]) -> dict[str, Any]:
    clean = sorted(float(v) for v in values if v is not None and v >= 0)
    if not clean:
        return {"count": 0}

    def percentile(p: float) -> float:
        if len(clean) == 1:
            return clean[0]
        idx = round((len(clean) - 1) * p)
        return clean[idx]

    return {
        "count": len(clean),
        "min_ms": round(clean[0], 2),
        "avg_ms": round(sum(clean) / len(clean), 2),
        "p50_ms": round(percentile(0.50), 2),
        "p95_ms": round(percentile(0.95), 2),
        "max_ms": round(clean[-1], 2),
    }


def _latest_observability_metrics(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    target_kind = f"observability.{event_type}"
    latest: Optional[dict[str, Any]] = None
    latest_ms = -1
    for event in events:
        if event.get("kind") != target_kind:
            continue
        event_ms = _event_ms(event) or 0
        if event_ms >= latest_ms:
            latest_ms = event_ms
            metrics = event.get("metrics")
            latest = metrics if isinstance(metrics, dict) else {}
    return latest or {}


def build_metrics(meeting_id: str, events: list[dict[str, Any]], counters: dict[str, Any]) -> dict[str, Any]:
    closed_at_ms = now_ms()
    event_times = [value for value in (_event_ms(event) for event in events) if value is not None]
    started_at_ms = min(event_times) if event_times else closed_at_ms

    transcript_events = [e for e in events if e.get("kind") == "transcript.completed"]
    insight_events = [e for e in events if e.get("kind") == "insight"]
    qa_events = [e for e in events if e.get("kind") == "qa"]
    ai_call_events = [e for e in events if e.get("kind") == "observability.ai_call"]
    ui_latency_events = [e for e in events if e.get("kind") == "observability.ui_event_latency"]

    transcript_texts = [(e.get("text") or e.get("transcript") or "").strip() for e in transcript_events]
    transcript_words = sum(_word_count(text) for text in transcript_texts)

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cached_tokens = 0
    reasoning_tokens = 0
    ai_cost_usd = 0.0
    ai_calls_by_purpose: dict[str, dict[str, Any]] = {}
    useful_ai_calls = 0
    empty_ai_calls = 0
    fallback_ai_calls = 0

    model_latencies: list[float] = []
    transcript_to_publish_latencies: list[float] = []
    model_latencies_by_purpose: dict[str, list[float]] = {}
    transcript_to_publish_latencies_by_purpose: dict[str, list[float]] = {}

    for event in ai_call_events:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        purpose = str(metrics.get("purpose") or "unknown")
        usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
        prompt_tokens += _safe_int(usage.get("prompt_tokens"))
        completion_tokens += _safe_int(usage.get("completion_tokens"))
        total_tokens += _safe_int(usage.get("total_tokens"))

        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens += _safe_int(prompt_details.get("cached_tokens"))

        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning_tokens += _safe_int(completion_details.get("reasoning_tokens"))

        estimated_cost = _safe_float(metrics.get("estimated_cost_usd"))
        ai_cost_usd += estimated_cost

        purpose_bucket = ai_calls_by_purpose.setdefault(
            purpose,
            {"count": 0, "estimated_cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        purpose_bucket["count"] += 1
        purpose_bucket["estimated_cost_usd"] += estimated_cost
        purpose_bucket["prompt_tokens"] += _safe_int(usage.get("prompt_tokens"))
        purpose_bucket["completion_tokens"] += _safe_int(usage.get("completion_tokens"))
        purpose_bucket["total_tokens"] += _safe_int(usage.get("total_tokens"))

        status = str(metrics.get("output_status") or "")
        if status == "published":
            useful_ai_calls += 1
        if status == "fallback_published":
            empty_ai_calls += 1
            fallback_ai_calls += 1
        if status in ("empty", "empty_after_filters", "parse_empty"):
            empty_ai_calls += 1

        if metrics.get("model_latency_ms") is not None:
            value = _safe_float(metrics.get("model_latency_ms"))
            model_latencies.append(value)
            model_latencies_by_purpose.setdefault(purpose, []).append(value)
        if metrics.get("transcript_to_publish_ms") is not None:
            value = _safe_float(metrics.get("transcript_to_publish_ms"))
            transcript_to_publish_latencies.append(value)
            transcript_to_publish_latencies_by_purpose.setdefault(purpose, []).append(value)

    for bucket in ai_calls_by_purpose.values():
        bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"], 8)

    client_snapshot = _latest_observability_metrics(events, "client_final_snapshot") or _latest_observability_metrics(
        events, "client_snapshot"
    )
    vad = client_snapshot.get("vad") if isinstance(client_snapshot.get("vad"), dict) else {}

    audio_bytes_forwarded = _safe_float(counters.get("audio_bytes_forwarded_total"))
    audio_bytes_received = _safe_float(counters.get("audio_bytes_received_total"))
    audio_seconds_forwarded = _safe_float(counters.get("audio_seconds_forwarded_estimated"))
    audio_seconds_received = audio_bytes_received / 2 / 24000 if audio_bytes_received else 0.0

    transcription_cost_usd = (audio_seconds_forwarded / 60) * TRANSCRIPTION_COST_PER_MINUTE_USD
    total_cost_usd = transcription_cost_usd + ai_cost_usd

    transcript_latencies = []
    for event in transcript_events:
        timings = event.get("timings") if isinstance(event.get("timings"), dict) else {}
        value = timings.get("estimated_last_audio_to_transcript_ms")
        if value is not None:
            transcript_latencies.append(_safe_float(value))

    ui_server_to_ui_latencies = []
    ui_transcript_to_ui_latencies = []
    ui_ai_output_to_ui_latencies = []
    ui_render_latencies = []
    ui_latencies_by_event_kind: dict[str, dict[str, list[float]]] = {}
    for event in ui_latency_events:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        event_kind = str(metrics.get("event_kind") or "unknown")
        bucket = ui_latencies_by_event_kind.setdefault(
            event_kind,
            {"server_to_ui": [], "transcript_to_ui": [], "ui_render": []},
        )
        if metrics.get("server_to_ui_ms") is not None:
            value = _safe_float(metrics.get("server_to_ui_ms"))
            ui_server_to_ui_latencies.append(value)
            bucket["server_to_ui"].append(value)
        if metrics.get("transcript_to_ui_ms") is not None:
            value = _safe_float(metrics.get("transcript_to_ui_ms"))
            ui_transcript_to_ui_latencies.append(value)
            bucket["transcript_to_ui"].append(value)
            if event_kind in ("qa", "insight"):
                ui_ai_output_to_ui_latencies.append(value)
        if metrics.get("ui_render_ms") is not None:
            value = _safe_float(metrics.get("ui_render_ms"))
            ui_render_latencies.append(value)
            bucket["ui_render"].append(value)

    decisions = _collect_decisions(transcript_events, insight_events)

    return {
        "meeting_id": meeting_id,
        "generated_at_ms": closed_at_ms,
        "generated_at": _iso_from_ms(closed_at_ms),
        "started_at_ms": started_at_ms,
        "started_at": _iso_from_ms(started_at_ms),
        "duration_seconds_estimated": round(max(0, closed_at_ms - started_at_ms) / 1000, 3),
        "events_total": len(events),
        "cost": {
            "estimated_total_usd": round(total_cost_usd, 8),
            "estimated_transcription_usd": round(transcription_cost_usd, 8),
            "estimated_ai_usd": round(ai_cost_usd, 8),
            "transcription_cost_per_minute_usd": TRANSCRIPTION_COST_PER_MINUTE_USD,
            "ai_by_purpose": ai_calls_by_purpose,
        },
        "audio": {
            "chunks_received_total": _safe_int(counters.get("audio_chunks_received_total")),
            "bytes_received_total": _safe_int(audio_bytes_received),
            "seconds_received_estimated": round(audio_seconds_received, 3),
            "chunks_forwarded_total": _safe_int(counters.get("audio_chunks_forwarded_total")),
            "bytes_forwarded_total": _safe_int(audio_bytes_forwarded),
            "seconds_forwarded_estimated": round(audio_seconds_forwarded, 3),
        },
        "vad": {
            **vad,
            "enabled": bool(vad.get("enabled")) if vad else False,
            "gating_active": bool(vad.get("gating_active")) if vad else False,
            "bypass_reason": vad.get("bypass_reason") if vad else "not_reported",
            "drop_rate_percent": _percent(_safe_float(vad.get("frames_dropped")), _safe_float(vad.get("frames_processed"))),
            "audio_savings_percent": _percent(
                _safe_float(vad.get("seconds_dropped")),
                _safe_float(vad.get("seconds_processed")),
            ),
        },
        "tokens": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
        },
        "decisions": decisions,
        "latency": {
            "last_audio_to_transcript": _summarize(transcript_latencies),
            "model_call": _summarize(model_latencies),
            "model_call_by_purpose": {
                purpose: _summarize(values) for purpose, values in sorted(model_latencies_by_purpose.items())
            },
            "transcript_to_ai_publish": _summarize(transcript_to_publish_latencies),
            "transcript_to_ai_publish_by_purpose": {
                purpose: _summarize(values)
                for purpose, values in sorted(transcript_to_publish_latencies_by_purpose.items())
            },
            "server_to_ui": _summarize(ui_server_to_ui_latencies),
            "transcript_to_ui": _summarize(ui_transcript_to_ui_latencies),
            "ai_output_to_ui": _summarize(ui_ai_output_to_ui_latencies),
            "ui_render": _summarize(ui_render_latencies),
            "ui_by_event_kind": {
                kind: {
                    metric_name: _summarize(values)
                    for metric_name, values in metric_buckets.items()
                }
                for kind, metric_buckets in sorted(ui_latencies_by_event_kind.items())
            },
        },
        "quality": {
            "transcript_completed_count": len(transcript_events),
            "transcript_words_total": transcript_words,
            "insights_published_count": len(insight_events),
            "qa_published_count": len(qa_events),
            "ai_calls_total": len(ai_call_events),
            "ai_calls_published": useful_ai_calls,
            "ai_calls_empty": empty_ai_calls,
            "ai_calls_fallback": fallback_ai_calls,
            "useful_output_rate_percent": _percent(useful_ai_calls, len(ai_call_events)),
            "decisions_count": len(decisions),
            "terms_published_count": sum(
                len((e.get("insights") or {}).get("terms") or []) for e in insight_events if isinstance(e.get("insights"), dict)
            ),
            "actions_published_count": sum(
                len((e.get("insights") or {}).get("actions") or [])
                for e in insight_events
                if isinstance(e.get("insights"), dict)
            ),
        },
    }


def _format_seconds(seconds: Any) -> str:
    value = _safe_float(seconds)
    minutes = int(value // 60)
    remainder = int(value % 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _fmt_money(value: Any) -> str:
    return f"${_safe_float(value):.6f}"


def _fmt_latency(summary: dict[str, Any]) -> str:
    if not summary or not summary.get("count"):
        return "n/a"
    return f"avg {summary.get('avg_ms')}ms | p50 {summary.get('p50_ms')}ms | p95 {summary.get('p95_ms')}ms"


def _fmt_bool(value: Any) -> str:
    return "yes" if bool(value) else "no"


def build_markdown_report(meeting_id: str, events: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    transcript_events = [e for e in events if e.get("kind") == "transcript.completed"]
    insight_events = [e for e in events if e.get("kind") == "insight"]
    qa_events = [e for e in events if e.get("kind") == "qa"]

    lines = [
        f"# Meeting Report - {meeting_id}",
        "",
        f"- Generated at: {metrics.get('generated_at')}",
        f"- Estimated duration: {_format_seconds(metrics.get('duration_seconds_estimated'))}",
        f"- Events captured: {metrics.get('events_total')}",
        f"- Estimated total cost: {_fmt_money((metrics.get('cost') or {}).get('estimated_total_usd'))}",
        "",
        "## Cost",
        "",
        f"- Transcription: {_fmt_money((metrics.get('cost') or {}).get('estimated_transcription_usd'))}",
        f"- AI calls: {_fmt_money((metrics.get('cost') or {}).get('estimated_ai_usd'))}",
        "",
        "## Audio and VAD",
        "",
    ]

    audio = metrics.get("audio") or {}
    vad = metrics.get("vad") or {}
    lines.extend(
        [
            f"- Audio received: {_format_seconds(audio.get('seconds_received_estimated'))} ({audio.get('chunks_received_total', 0)} chunks)",
            f"- Audio forwarded to transcription: {_format_seconds(audio.get('seconds_forwarded_estimated'))}",
            f"- VAD enabled: {_fmt_bool(vad.get('enabled'))}",
            f"- VAD gating active: {_fmt_bool(vad.get('gating_active'))}",
            f"- VAD bypass reason: {vad.get('bypass_reason') or 'none'}",
            f"- VAD calibrated: {_fmt_bool(vad.get('calibrated'))}",
            f"- VAD thresholds: rms {vad.get('effective_rms_threshold', vad.get('rms_threshold', 'n/a'))} | peak {vad.get('effective_peak_threshold', vad.get('peak_threshold', 'n/a'))}",
            f"- VAD noise floor: rms {vad.get('noise_rms_floor', 'n/a')} | peak {vad.get('noise_peak_floor', 'n/a')}",
            f"- VAD frames processed: {vad.get('frames_processed', 0)}",
            f"- VAD frames sent: {vad.get('frames_sent', 0)}",
            f"- VAD frames dropped: {vad.get('frames_dropped', 0)}",
            f"- VAD frames bypassed: {vad.get('frames_bypassed', 0)}",
            f"- VAD estimated savings: {vad.get('audio_savings_percent', 0)}%",
            "",
            "## Tokens",
            "",
        ]
    )

    tokens = metrics.get("tokens") or {}
    lines.extend(
        [
            f"- Prompt tokens: {tokens.get('prompt_tokens', 0)}",
            f"- Completion tokens: {tokens.get('completion_tokens', 0)}",
            f"- Total tokens: {tokens.get('total_tokens', 0)}",
            f"- Cached tokens: {tokens.get('cached_tokens', 0)}",
            f"- Reasoning tokens: {tokens.get('reasoning_tokens', 0)}",
            "",
            "## Latency",
            "",
        ]
    )

    latency = metrics.get("latency") or {}
    transcript_ui_latency = (
        ((latency.get("ui_by_event_kind") or {}).get("transcript.completed") or {}).get("transcript_to_ui") or {}
    )
    lines.extend(
        [
            f"- Last audio to transcript: {_fmt_latency(latency.get('last_audio_to_transcript') or {})}",
            f"- Model call: {_fmt_latency(latency.get('model_call') or {})}",
            f"- Transcript to AI publish: {_fmt_latency(latency.get('transcript_to_ai_publish') or {})}",
            f"- Server to UI: {_fmt_latency(latency.get('server_to_ui') or {})}",
            f"- Transcript UI latency: {_fmt_latency(transcript_ui_latency)}",
            f"- AI output to UI latency: {_fmt_latency(latency.get('ai_output_to_ui') or {})}",
            f"- UI render: {_fmt_latency(latency.get('ui_render') or {})}",
            "",
            "### Latency by Type",
            "",
        ]
    )

    for purpose, summary in (latency.get("model_call_by_purpose") or {}).items():
        lines.append(f"- Model call ({purpose}): {_fmt_latency(summary or {})}")
    for purpose, summary in (latency.get("transcript_to_ai_publish_by_purpose") or {}).items():
        lines.append(f"- Transcript to AI publish ({purpose}): {_fmt_latency(summary or {})}")
    for event_kind, buckets in (latency.get("ui_by_event_kind") or {}).items():
        if not isinstance(buckets, dict):
            continue
        lines.append(
            f"- UI ({event_kind}): server {_fmt_latency(buckets.get('server_to_ui') or {})} | "
            f"transcript {_fmt_latency(buckets.get('transcript_to_ui') or {})} | "
            f"render {_fmt_latency(buckets.get('ui_render') or {})}"
        )

    lines.extend(["", "## Quality", ""])

    quality = metrics.get("quality") or {}
    lines.extend(
        [
            f"- Transcript chunks: {quality.get('transcript_completed_count', 0)}",
            f"- Transcript words: {quality.get('transcript_words_total', 0)}",
            f"- Insights published: {quality.get('insights_published_count', 0)}",
            f"- Q&A published: {quality.get('qa_published_count', 0)}",
            f"- Decisions captured: {quality.get('decisions_count', 0)}",
            f"- AI calls: {quality.get('ai_calls_total', 0)}",
            f"- AI fallback outputs: {quality.get('ai_calls_fallback', 0)}",
            f"- Useful output rate: {quality.get('useful_output_rate_percent', 0)}%",
            "",
            "## Transcript",
            "",
        ]
    )

    if transcript_events:
        for event in transcript_events:
            text = (event.get("text") or event.get("transcript") or "").strip()
            if text:
                lines.append(f"- {text}")
    else:
        lines.append("_No transcript captured._")

    decisions = metrics.get("decisions") or _collect_decisions(transcript_events, insight_events)
    lines.extend(["", "## Decisions", ""])
    if decisions:
        for decision in decisions:
            lines.append(f"- {decision}")
    else:
        lines.append("_No decisions captured._")

    lines.extend(["", "## Insights", ""])
    if insight_events:
        seen_terms: set[str] = set()
        for event in insight_events:
            insights = event.get("insights") if isinstance(event.get("insights"), dict) else {}
            summary = (insights.get("summary") or "").strip()
            if summary:
                lines.append(f"- Summary: {summary}")
            for point in insights.get("key_points") or []:
                lines.append(f"- Key point: {point}")
            for term in insights.get("terms") or []:
                if isinstance(term, dict):
                    term_name = _clean_text(term.get("term"))
                    if not term_name:
                        continue
                    term_key = term_name.lower()
                    if term_key in seen_terms:
                        continue
                    seen_terms.add(term_key)
                    lines.append(f"- Term: {term_name} - {term.get('explain')}")
            for action in insights.get("actions") or []:
                lines.append(f"- Action: {action}")
    else:
        lines.append("_No insights captured._")

    lines.extend(["", "## Q&A", ""])
    if qa_events:
        for event in qa_events:
            qa = event.get("qa") if isinstance(event.get("qa"), dict) else {}
            question = (qa.get("question") or "").strip()
            answer = (qa.get("answer") or qa.get("answer_short") or "").strip()
            if question:
                lines.append(f"- Q: {question}")
            if answer:
                lines.append(f"  A: {answer}")
    else:
        lines.append("_No Q&A captured._")

    lines.append("")
    return "\n".join(lines)


def write_meeting_artifacts(
    meeting_id: str,
    events: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, str]:
    ts = filename_timestamp()
    meeting_dir = OBSERVABILITY_ROOT / f"data_{ts}" / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    events_path = meeting_dir / f"events_{ts}.jsonl"
    metrics_path = meeting_dir / f"metrics_{ts}.json"
    report_path = meeting_dir / f"report_{ts}.md"

    with events_path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            f.write("\n")

    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(build_markdown_report(meeting_id, events, metrics), encoding="utf-8")

    return {
        "events_path": str(events_path),
        "metrics_path": str(metrics_path),
        "report_path": str(report_path),
    }


def generate_meeting_artifacts(r: redis.Redis, meeting_id: str) -> dict[str, Any]:
    events = read_meeting_events(r, meeting_id)
    counters = get_counters(r, meeting_id)
    metrics = build_metrics(meeting_id, events, counters)
    paths = write_meeting_artifacts(meeting_id, events, metrics)
    return {
        **paths,
        "metrics": {
            "estimated_total_cost_usd": metrics.get("cost", {}).get("estimated_total_usd", 0),
            "events_total": metrics.get("events_total", 0),
            "transcript_completed_count": metrics.get("quality", {}).get("transcript_completed_count", 0),
        },
    }
