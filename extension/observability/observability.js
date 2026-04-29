(function attachContextObservability(global) {
  const DEFAULT_FLUSH_INTERVAL_MS = 30000;

  function roundMetric(value, digits = 3) {
    const factor = 10 ** digits;
    return Math.round((Number(value) || 0) * factor) / factor;
  }

  function percentile(values, p) {
    if (!values || values.length === 0) return 0;
    const sorted = values.slice().sort((a, b) => a - b);
    const idx = Math.max(0, Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * p)));
    return sorted[idx];
  }

  function createState(vadConfig) {
    return {
      startedAtEpochMs: Date.now(),
      lastFlushAtMs: 0,
      flushInFlight: false,
      vad: {
        framesProcessed: 0,
        framesSent: 0,
        framesDropped: 0,
        secondsProcessed: 0,
        secondsSent: 0,
        secondsDropped: 0,
        bytesSent: 0,
        rmsSum: 0,
        peakMax: 0,
        speechDetections: 0,
        bypassedFrames: 0,
        gatingActive: false,
        bypassReason: 'not_started',
        calibrated: false,
        calibrationStartedAtMs: null,
        calibrationCompletedAtMs: null,
        calibrationFrames: 0,
        noiseRmsSamples: [],
        noisePeakSamples: [],
        noiseRmsFloor: 0,
        noisePeakFloor: 0,
        effectiveRmsThreshold: vadConfig.rmsThreshold,
        effectivePeakThreshold: vadConfig.peakThreshold,
      },
      ui: {
        eventsRendered: 0,
        serverToUiLatencySumMs: 0,
        transcriptToUiLatencySumMs: 0,
        uiRenderLatencySumMs: 0,
      },
    };
  }

  function resetVadRuntimeState(state, vadConfig, bypassReason) {
    const vad = state.vad;
    vad.bypassedFrames = 0;
    vad.gatingActive = false;
    vad.bypassReason = bypassReason || 'calibrating_noise';
    vad.calibrated = false;
    vad.calibrationStartedAtMs = null;
    vad.calibrationCompletedAtMs = null;
    vad.calibrationFrames = 0;
    vad.noiseRmsSamples = [];
    vad.noisePeakSamples = [];
    vad.noiseRmsFloor = 0;
    vad.noisePeakFloor = 0;
    vad.effectiveRmsThreshold = vadConfig.rmsThreshold;
    vad.effectivePeakThreshold = vadConfig.peakThreshold;
  }

  function finalizeVadCalibration(state, vadConfig, nowMs) {
    const vad = state.vad;
    const noiseRmsFloor = percentile(vad.noiseRmsSamples, 0.2);
    const noisePeakFloor = percentile(vad.noisePeakSamples, 0.2);

    vad.noiseRmsFloor = noiseRmsFloor;
    vad.noisePeakFloor = noisePeakFloor;
    vad.effectiveRmsThreshold = Math.max(
      vadConfig.rmsThreshold,
      noiseRmsFloor * vadConfig.dynamicRmsMultiplier + vadConfig.rmsNoiseMargin
    );
    vad.effectivePeakThreshold = Math.min(
      0.95,
      Math.max(vadConfig.peakThreshold, noisePeakFloor * vadConfig.dynamicPeakMultiplier + vadConfig.peakNoiseMargin)
    );
    vad.calibrated = true;
    vad.calibrationCompletedAtMs = nowMs;
    vad.gatingActive = true;
    vad.bypassReason = null;

    console.log(
      `[VAD] calibrated rms_floor=${noiseRmsFloor.toFixed(4)} peak_floor=${noisePeakFloor.toFixed(4)} ` +
        `rms_threshold=${vad.effectiveRmsThreshold.toFixed(4)} peak_threshold=${vad.effectivePeakThreshold.toFixed(4)}`
    );
  }

  function updateVadCalibration(state, vadConfig, metrics, nowMs) {
    const vad = state.vad;
    if (vad.calibrated) return true;

    if (vad.calibrationStartedAtMs === null) {
      vad.calibrationStartedAtMs = nowMs;
    }

    vad.calibrationFrames += 1;
    vad.noiseRmsSamples.push(metrics.rms || 0);
    vad.noisePeakSamples.push(metrics.peak || 0);

    if ((nowMs - vad.calibrationStartedAtMs) >= vadConfig.calibrationMs) {
      finalizeVadCalibration(state, vadConfig, nowMs);
      return true;
    }

    vad.gatingActive = false;
    vad.bypassReason = 'calibrating_noise';
    return false;
  }

  function recordVadFrame(state, metrics, sent, durationSeconds, speechDetected, bypassReason = null) {
    const vad = state.vad;
    vad.framesProcessed += 1;
    vad.secondsProcessed += durationSeconds;
    vad.rmsSum += metrics.rms || 0;
    if ((metrics.peak || 0) > vad.peakMax) {
      vad.peakMax = metrics.peak || 0;
    }
    if (speechDetected) {
      vad.speechDetections += 1;
    }
    if (bypassReason && bypassReason !== 'calibrating_noise') {
      vad.bypassedFrames += 1;
    }

    if (sent) {
      vad.framesSent += 1;
      vad.secondsSent += durationSeconds;
    } else {
      vad.framesDropped += 1;
      vad.secondsDropped += durationSeconds;
    }
  }

  function buildClientSnapshot(state, vadConfig) {
    const vad = state.vad;
    const ui = state.ui;
    const durationSeconds = (Date.now() - state.startedAtEpochMs) / 1000;
    const avgRms = vad.framesProcessed > 0 ? vad.rmsSum / vad.framesProcessed : 0;
    const serverToUiAvg = ui.eventsRendered > 0 ? ui.serverToUiLatencySumMs / ui.eventsRendered : null;
    const transcriptToUiAvg = ui.eventsRendered > 0 ? ui.transcriptToUiLatencySumMs / ui.eventsRendered : null;
    const uiRenderAvg = ui.eventsRendered > 0 ? ui.uiRenderLatencySumMs / ui.eventsRendered : null;

    return {
      client_duration_seconds: roundMetric(durationSeconds),
      vad: {
        enabled: vadConfig.enabled,
        gating_active: Boolean(vad.gatingActive),
        bypass_reason: vad.bypassReason,
        bypass_without_mic: vadConfig.bypassWithoutMic,
        rms_threshold: vadConfig.rmsThreshold,
        peak_threshold: vadConfig.peakThreshold,
        effective_rms_threshold: roundMetric(vad.effectiveRmsThreshold, 6),
        effective_peak_threshold: roundMetric(vad.effectivePeakThreshold, 6),
        hangover_ms: vadConfig.hangoverMs,
        calibration_ms: vadConfig.calibrationMs,
        calibration_frames: vad.calibrationFrames,
        calibrated: Boolean(vad.calibrated),
        noise_rms_floor: roundMetric(vad.noiseRmsFloor, 6),
        noise_peak_floor: roundMetric(vad.noisePeakFloor, 6),
        frames_processed: vad.framesProcessed,
        frames_sent: vad.framesSent,
        frames_dropped: vad.framesDropped,
        frames_bypassed: vad.bypassedFrames,
        seconds_processed: roundMetric(vad.secondsProcessed),
        seconds_sent: roundMetric(vad.secondsSent),
        seconds_dropped: roundMetric(vad.secondsDropped),
        bytes_sent: vad.bytesSent,
        avg_rms: roundMetric(avgRms, 6),
        peak_max: roundMetric(vad.peakMax, 6),
        speech_detections: vad.speechDetections,
      },
      ui: {
        events_rendered: ui.eventsRendered,
        server_to_ui_avg_ms: serverToUiAvg === null ? null : roundMetric(serverToUiAvg, 2),
        transcript_to_ui_avg_ms: transcriptToUiAvg === null ? null : roundMetric(transcriptToUiAvg, 2),
        ui_render_avg_ms: uiRenderAvg === null ? null : roundMetric(uiRenderAvg, 2),
      },
    };
  }

  function maybeFlushSnapshot(state, meeting, publish, vadConfig, reason = 'periodic', intervalMs = DEFAULT_FLUSH_INTERVAL_MS) {
    const now = Date.now();
    if (!meeting || state.flushInFlight) return;
    if (now - state.lastFlushAtMs < intervalMs) return;

    state.lastFlushAtMs = now;
    state.flushInFlight = true;
    publish('client_snapshot', buildClientSnapshot(state, vadConfig), { reason }).finally(() => {
      state.flushInFlight = false;
    });
  }

  async function flushFinalSnapshot(state, meeting, publish, vadConfig, reason = 'final') {
    if (!meeting) return;
    await publish('client_final_snapshot', buildClientSnapshot(state, vadConfig), { reason });
  }

  function getEventTimestampMs(data) {
    if (!data || typeof data !== 'object') return null;
    if (data.timings && typeof data.timings.published_at_ms === 'number') {
      return data.timings.published_at_ms;
    }
    if (typeof data.ts_ms === 'number') {
      return data.ts_ms;
    }
    if (typeof data.ts === 'number') {
      return data.ts * 1000;
    }
    return null;
  }

  function getTranscriptCompletedTimestampMs(data) {
    if (!data || typeof data !== 'object' || !data.timings) return null;
    if (typeof data.timings.transcript_completed_at_ms === 'number') {
      return data.timings.transcript_completed_at_ms;
    }
    if (typeof data.timings.transcript_event_received_at_ms === 'number') {
      return data.timings.transcript_event_received_at_ms;
    }
    return null;
  }

  function recordUiLatency(state, data, handledKind, receivedAtEpochMs, renderedAtEpochMs, renderDurationMs, publish) {
    if (!handledKind) return;

    const serverEventAtMs = getEventTimestampMs(data);
    const transcriptCompletedAtMs = getTranscriptCompletedTimestampMs(data);
    const serverToUiMs = serverEventAtMs ? receivedAtEpochMs - serverEventAtMs : null;
    const transcriptToUiMs = transcriptCompletedAtMs ? renderedAtEpochMs - transcriptCompletedAtMs : null;

    state.ui.eventsRendered += 1;
    if (serverToUiMs !== null && serverToUiMs >= 0) {
      state.ui.serverToUiLatencySumMs += serverToUiMs;
    }
    if (transcriptToUiMs !== null && transcriptToUiMs >= 0) {
      state.ui.transcriptToUiLatencySumMs += transcriptToUiMs;
    }
    state.ui.uiRenderLatencySumMs += renderDurationMs;

    publish(
      'ui_event_latency',
      {
        event_kind: handledKind,
        server_to_ui_ms: serverToUiMs === null ? null : roundMetric(serverToUiMs, 2),
        transcript_to_ui_ms: transcriptToUiMs === null ? null : roundMetric(transcriptToUiMs, 2),
        ui_render_ms: roundMetric(renderDurationMs, 2),
      },
      {
        source_item_id: data.item_id || data.source_item_id || null,
      }
    );
  }

  global.ContextObservability = {
    DEFAULT_FLUSH_INTERVAL_MS,
    createState,
    resetVadRuntimeState,
    updateVadCalibration,
    recordVadFrame,
    maybeFlushSnapshot,
    flushFinalSnapshot,
    recordUiLatency,
  };
})(window);
