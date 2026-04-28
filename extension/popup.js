// Popup script for Context AI Plugin
// Handles UI interactions and displays real-time data
// API e storage são feitos direto na popup para evitar "message port closed" do service worker

const API_BASE = 'http://localhost:8000';

// Client-side VAD (voice activity detection) to avoid sending long silence segments.
const VAD_ENABLED = true;
const VAD_RMS_THRESHOLD = 0.008;
const VAD_PEAK_THRESHOLD = 0.03;
const VAD_HANGOVER_MS = 1200;
const VAD_BYPASS_WITHOUT_MIC = false;
const VAD_CALIBRATION_MS = 2000;
const VAD_DYNAMIC_RMS_MULTIPLIER = 1.8;
const VAD_DYNAMIC_PEAK_MULTIPLIER = 1.8;
const VAD_RMS_NOISE_MARGIN = 0.003;
const VAD_PEAK_NOISE_MARGIN = 0.01;
const VAD_DEBUG_LOG_EVERY_MS = 10000;
const OBSERVABILITY_FLUSH_INTERVAL_MS = 30000;

let currentMeeting = null;
let audioSocket = null;
let eventSocket = null;
let micStream = null;
let tabStream = null;
let audioContext = null;
let audioProcessor = null;
let mixedSource = null;
let tokenRefreshInterval = null;
let vadLastSpeechTs = 0;
let vadLastDebugTs = 0;
let observabilityState = createObservabilityState();

const STORAGE_KEYS = ['meeting_id', 'token', 'event_url', 'audio_url'];

function createObservabilityState() {
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
      effectiveRmsThreshold: VAD_RMS_THRESHOLD,
      effectivePeakThreshold: VAD_PEAK_THRESHOLD,
    },
    ui: {
      eventsRendered: 0,
      serverToUiLatencySumMs: 0,
      transcriptToUiLatencySumMs: 0,
      uiRenderLatencySumMs: 0,
    },
  };
}

function resetObservabilityState() {
  observabilityState = createObservabilityState();
}

// DOM elements
const startBtn = document.getElementById('start-btn');
const testBtn = document.getElementById('test-btn');
const testResult = document.getElementById('test-result');
const stopBtn = document.getElementById('stop-btn');
const refreshBtn = document.getElementById('refresh-btn');
const statusIndicator = document.getElementById('status-indicator');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const meetingInfo = document.getElementById('meeting-info');
const meetingIdDisplay = document.getElementById('meeting-id-display');
const tokenExpiry = document.getElementById('token-expiry');
const transcriptContainer = document.getElementById('transcript-container');
const insightsContainer = document.getElementById('insights-container');
const qaContainer = document.getElementById('qa-container');
const errorMessage = document.getElementById('error-message');

// Initialize popup
document.addEventListener('DOMContentLoaded', async () => {
  await loadStoredMeeting();
  setupEventListeners();
});

// Load stored meeting data (direto do storage, sem background)
async function loadStoredMeeting() {
  const data = await getStoredMeeting();
  if (data && data.meeting_id) {
    currentMeeting = data;
    updateUI('connected');
    await checkMeetingStatus();
  }
}

function getStoredMeeting() {
  return new Promise((resolve) => {
    chrome.storage.local.get(STORAGE_KEYS, (result) => resolve(result));
  });
}

function setStoredMeeting(data) {
  return new Promise((resolve) => {
    chrome.storage.local.set(data, resolve);
  });
}

function clearStoredMeeting() {
  return new Promise((resolve) => {
    chrome.storage.local.remove(STORAGE_KEYS, resolve);
  });
}

// Chamadas à API direto da popup (evita message port closed)
async function apiCreateMeeting() {
  try {
    const res = await fetch(API_BASE + '/meetings', { method: 'POST' });
    if (!res.ok) throw new Error(res.statusText);
    return await res.json();
  } catch (e) {
    console.error('apiCreateMeeting:', e);
    return null;
  }
}

async function apiGetMeetingStatus(meetingId, token) {
  try {
    const res = await fetch(`${API_BASE}/meetings/${meetingId}?token=${encodeURIComponent(token)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

async function apiRefreshToken(meetingId, token) {
  try {
    const res = await fetch(`${API_BASE}/meetings/${meetingId}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    return await res.json();
  } catch (e) {
    console.error('apiRefreshToken:', e);
    return null;
  }
}

async function apiCloseMeeting(meetingId) {
  try {
    const res = await fetch(`${API_BASE}/meetings/${meetingId}/close`, { method: 'POST' });
    if (!res.ok) throw new Error(res.statusText);
    return await res.json();
  } catch (e) {
    console.error('apiCloseMeeting:', e);
    throw e;
  }
}

async function apiRecordObservability(kind, metrics = {}, metadata = {}) {
  if (!currentMeeting || !currentMeeting.meeting_id || !currentMeeting.token) return null;

  try {
    const res = await fetch(`${API_BASE}/meetings/${currentMeeting.meeting_id}/observability`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: currentMeeting.token,
        kind,
        metrics,
        metadata,
      }),
      keepalive: true,
    });
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    console.warn('apiRecordObservability:', e);
    return null;
  }
}

// Setup event listeners
function setupEventListeners() {
  testBtn.addEventListener('click', testConnection);
  startBtn.addEventListener('click', startMeeting);
  stopBtn.addEventListener('click', stopMeeting);
  refreshBtn.addEventListener('click', refreshToken);
}

// Testar se a API está acessível (fetch direto da popup)
async function testConnection() {
  testResult.classList.remove('hidden');
  testResult.textContent = 'Testando...';
  testResult.style.color = '';
  try {
    const res = await fetch(API_BASE + '/health');
    const text = await res.text();
    if (res.ok) {
      testResult.textContent = '✓ API OK';
      testResult.style.color = 'green';
    } else {
      testResult.textContent = '✗ Falha: ' + res.status + ' ' + text;
      testResult.style.color = 'red';
    }
  } catch (e) {
    testResult.textContent = '✗ Erro: ' + formatError(e);
    testResult.style.color = 'red';
  }
  setTimeout(() => testResult.classList.add('hidden'), 5000);
}

// Start meeting: cria reunião, solicita microfone + áudio da aba e conecta WS.
async function startMeeting() {
  try {
    showError('');
    updateUI('connecting');

    // 1) Criar reunião na API (fetch direto da popup)
    const data = await apiCreateMeeting();
    if (!data) {
      showError('Backend inacessível. Verifique se está rodando (localhost:8000) e clique em "Testar conexão com a API".');
      updateUI('disconnected');
      return;
    }

    currentMeeting = data;
    resetObservabilityState();
    await setStoredMeeting(data);

    // 2) Solicitar microfone e capturar áudio da aba ativa (ex.: Google Meet/Teams)
    const captureResult = await startCombinedAudioCapture();
    if (!captureResult.ok) {
      console.error('Audio capture failed:', captureResult.message);
      await apiCloseMeeting(currentMeeting.meeting_id).catch(() => {});
      await clearStoredMeeting();
      currentMeeting = null;
      updateUI('disconnected');
      showError(captureResult.message);
      return;
    }

    await connectEventSocket();
    startTokenRefreshMonitoring();
    updateUI('connected');
    if (captureResult.warning) {
      showError(`Aviso: ${captureResult.warning}. Seguindo apenas com o áudio da aba.`);
    }
  } catch (error) {
    console.error('Error starting meeting:', error);
    showError('Erro ao iniciar reunião: ' + formatError(error));
    updateUI('disconnected');
  }
}

function captureActiveTabAudio() {
  return new Promise((resolve, reject) => {
    if (!chrome.tabCapture || !chrome.tabCapture.capture) {
      reject(new Error('tabCapture indisponível nesta versão do navegador'));
      return;
    }

    chrome.tabCapture.capture({ audio: true, video: false }, (stream) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!stream) {
        reject(new Error('Fluxo de áudio da aba não disponível'));
        return;
      }
      resolve(stream);
    });
  });
}

function getActiveTab() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!tabs || tabs.length === 0) {
        reject(new Error('Nenhuma aba ativa encontrada'));
        return;
      }
      resolve(tabs[0]);
    });
  });
}

function getTabMediaStreamId(tabId) {
  return new Promise((resolve, reject) => {
    if (!chrome.tabCapture || !chrome.tabCapture.getMediaStreamId) {
      reject(new Error('tabCapture.getMediaStreamId indisponível'));
      return;
    }

    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!streamId) {
        reject(new Error('Não foi possível obter streamId da aba'));
        return;
      }
      resolve(streamId);
    });
  });
}

async function captureTabAudioByMediaStreamId() {
  const activeTab = await getActiveTab();
  const streamId = await getTabMediaStreamId(activeTab.id);

  const constraints = {
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    },
    video: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
        maxWidth: 1,
        maxHeight: 1,
        maxFrameRate: 1,
      },
    },
  };

  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  const audioTracks = stream.getAudioTracks();
  if (!audioTracks || audioTracks.length === 0) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error('StreamId gerado, mas sem áudio da aba');
  }
  stream.getVideoTracks().forEach((track) => track.stop());
  return new MediaStream(audioTracks);
}

async function captureTabAudioByDisplayMedia() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    throw new Error('getDisplayMedia indisponível neste navegador');
  }

  const displayStream = await navigator.mediaDevices.getDisplayMedia({
    video: true,
    audio: true,
  });

  const audioTracks = displayStream.getAudioTracks();
  if (!audioTracks || audioTracks.length === 0) {
    displayStream.getTracks().forEach((track) => track.stop());
    throw new Error('Nenhum áudio capturado da aba. Selecione uma aba e habilite "Compartilhar áudio".');
  }

  // Não precisamos do vídeo para transcrição.
  displayStream.getVideoTracks().forEach((track) => track.stop());
  return new MediaStream(audioTracks);
}

function chooseDesktopMediaStreamId() {
  return new Promise((resolve, reject) => {
    if (!chrome.desktopCapture || !chrome.desktopCapture.chooseDesktopMedia) {
      reject(new Error('desktopCapture indisponível neste navegador'));
      return;
    }

    chrome.desktopCapture.chooseDesktopMedia(['tab', 'audio'], (streamId) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!streamId) {
        reject(new Error('Compartilhamento da aba cancelado'));
        return;
      }
      resolve(streamId);
    });
  });
}

async function captureTabAudioByDesktopCapture() {
  const streamId = await chooseDesktopMediaStreamId();
  const desktopConstraints = {
    audio: {
      mandatory: {
        chromeMediaSource: 'desktop',
        chromeMediaSourceId: streamId,
      },
    },
    video: {
      mandatory: {
        chromeMediaSource: 'desktop',
        chromeMediaSourceId: streamId,
        maxWidth: 1,
        maxHeight: 1,
        maxFrameRate: 1,
      },
    },
  };

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(desktopConstraints);
  } catch (mediaDevicesError) {
    const legacyGetUserMedia = navigator.webkitGetUserMedia || navigator.getUserMedia;
    if (!legacyGetUserMedia) {
      throw mediaDevicesError;
    }

    stream = await new Promise((resolve, reject) => {
      legacyGetUserMedia.call(navigator, desktopConstraints, resolve, reject);
    });
  }

  const audioTracks = stream.getAudioTracks();
  if (!audioTracks || audioTracks.length === 0) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error('Nenhum áudio capturado da aba no compartilhamento');
  }

  // Não precisamos do vídeo para transcrição.
  stream.getVideoTracks().forEach((track) => track.stop());
  return new MediaStream(audioTracks);
}

async function captureTabAudioWithFallback() {
  const errors = [];

  try {
    return await captureActiveTabAudio();
  } catch (tabCaptureError) {
    console.warn('tabCapture indisponível/falhou:', tabCaptureError);
    errors.push('tabCapture: ' + formatError(tabCaptureError));
  }

  try {
    return await captureTabAudioByMediaStreamId();
  } catch (streamIdError) {
    console.warn('tabCapture.getMediaStreamId indisponível/falhou:', streamIdError);
    errors.push('streamId: ' + formatError(streamIdError));
  }

  try {
    return await captureTabAudioByDisplayMedia();
  } catch (displayMediaError) {
    console.warn('getDisplayMedia indisponível/falhou:', displayMediaError);
    errors.push('displayMedia: ' + formatError(displayMediaError));
  }

  try {
    return await captureTabAudioByDesktopCapture();
  } catch (desktopCaptureError) {
    console.warn('desktopCapture indisponível/falhou:', desktopCaptureError);
    errors.push('desktopCapture: ' + formatError(desktopCaptureError));
  }

  throw new Error('Falha ao capturar áudio da aba. Detalhes -> ' + errors.join(' | '));
}

async function requestMicrophoneAudio() {
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
  } catch (micError) {
    throw new Error('Microfone: ' + formatError(micError));
  }
}

async function startCombinedAudioCapture() {
  let localMicStream = null;
  let localTabStream = null;
  let micWarning = null;

  try {
    localMicStream = await requestMicrophoneAudio();
  } catch (error) {
    micWarning = error.message;
    console.warn('Microphone capture unavailable, continuing with tab audio only:', micWarning);
  }

  try {
    localTabStream = await captureTabAudioWithFallback();
  } catch (error) {
    if (localMicStream) {
      localMicStream.getTracks().forEach((track) => track.stop());
    }
    console.error('Tab audio capture error:', error);
    return {
      ok: false,
      message: 'Áudio da aba: ' + formatError(error),
    };
  }

  await startAudioCaptureWithStreams(localMicStream, localTabStream);
  return { ok: true, warning: micWarning };
}

function formatError(error) {
  if (!error) return 'erro desconhecido';
  if (error.name && error.message) return `${error.name}: ${error.message}`;
  if (error.message) return error.message;
  return String(error);
}

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

function getVadBypassReason() {
  if (!VAD_ENABLED) return 'disabled';
  if (!micStream && VAD_BYPASS_WITHOUT_MIC) return 'no_microphone';
  return null;
}

function resetVadRuntimeState() {
  vadLastSpeechTs = 0;
  vadLastDebugTs = 0;
  const vad = observabilityState.vad;
  vad.bypassedFrames = 0;
  vad.gatingActive = false;
  vad.bypassReason = getVadBypassReason() || 'calibrating_noise';
  vad.calibrated = false;
  vad.calibrationStartedAtMs = null;
  vad.calibrationCompletedAtMs = null;
  vad.calibrationFrames = 0;
  vad.noiseRmsSamples = [];
  vad.noisePeakSamples = [];
  vad.noiseRmsFloor = 0;
  vad.noisePeakFloor = 0;
  vad.effectiveRmsThreshold = VAD_RMS_THRESHOLD;
  vad.effectivePeakThreshold = VAD_PEAK_THRESHOLD;
}

function finalizeVadCalibration(nowMs) {
  const vad = observabilityState.vad;
  const noiseRmsFloor = percentile(vad.noiseRmsSamples, 0.2);
  const noisePeakFloor = percentile(vad.noisePeakSamples, 0.2);

  vad.noiseRmsFloor = noiseRmsFloor;
  vad.noisePeakFloor = noisePeakFloor;
  vad.effectiveRmsThreshold = Math.max(
    VAD_RMS_THRESHOLD,
    noiseRmsFloor * VAD_DYNAMIC_RMS_MULTIPLIER + VAD_RMS_NOISE_MARGIN
  );
  vad.effectivePeakThreshold = Math.min(
    0.95,
    Math.max(VAD_PEAK_THRESHOLD, noisePeakFloor * VAD_DYNAMIC_PEAK_MULTIPLIER + VAD_PEAK_NOISE_MARGIN)
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

function updateVadCalibration(metrics, nowMs) {
  const vad = observabilityState.vad;
  if (vad.calibrated) return true;

  if (vad.calibrationStartedAtMs === null) {
    vad.calibrationStartedAtMs = nowMs;
  }

  vad.calibrationFrames += 1;
  vad.noiseRmsSamples.push(metrics.rms || 0);
  vad.noisePeakSamples.push(metrics.peak || 0);

  if ((nowMs - vad.calibrationStartedAtMs) >= VAD_CALIBRATION_MS) {
    finalizeVadCalibration(nowMs);
    return true;
  }

  vad.gatingActive = false;
  vad.bypassReason = 'calibrating_noise';
  return false;
}

function recordVadFrame(metrics, sent, durationSeconds, speechDetected, bypassReason = null) {
  const vad = observabilityState.vad;
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

function buildClientObservabilitySnapshot() {
  const vad = observabilityState.vad;
  const ui = observabilityState.ui;
  const durationSeconds = (Date.now() - observabilityState.startedAtEpochMs) / 1000;
  const avgRms = vad.framesProcessed > 0 ? vad.rmsSum / vad.framesProcessed : 0;
  const serverToUiAvg = ui.eventsRendered > 0 ? ui.serverToUiLatencySumMs / ui.eventsRendered : null;
  const transcriptToUiAvg = ui.eventsRendered > 0 ? ui.transcriptToUiLatencySumMs / ui.eventsRendered : null;
  const uiRenderAvg = ui.eventsRendered > 0 ? ui.uiRenderLatencySumMs / ui.eventsRendered : null;

  return {
    client_duration_seconds: roundMetric(durationSeconds),
    vad: {
      enabled: VAD_ENABLED,
      gating_active: Boolean(vad.gatingActive),
      bypass_reason: vad.bypassReason,
      bypass_without_mic: VAD_BYPASS_WITHOUT_MIC,
      rms_threshold: VAD_RMS_THRESHOLD,
      peak_threshold: VAD_PEAK_THRESHOLD,
      effective_rms_threshold: roundMetric(vad.effectiveRmsThreshold, 6),
      effective_peak_threshold: roundMetric(vad.effectivePeakThreshold, 6),
      hangover_ms: VAD_HANGOVER_MS,
      calibration_ms: VAD_CALIBRATION_MS,
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

function maybeFlushObservabilitySnapshot(reason = 'periodic') {
  const now = Date.now();
  if (!currentMeeting || observabilityState.flushInFlight) return;
  if (now - observabilityState.lastFlushAtMs < OBSERVABILITY_FLUSH_INTERVAL_MS) return;

  observabilityState.lastFlushAtMs = now;
  observabilityState.flushInFlight = true;
  apiRecordObservability('client_snapshot', buildClientObservabilitySnapshot(), { reason })
    .finally(() => {
      observabilityState.flushInFlight = false;
    });
}

async function flushFinalObservabilitySnapshot(reason = 'final') {
  if (!currentMeeting) return;
  await apiRecordObservability('client_final_snapshot', buildClientObservabilitySnapshot(), { reason });
}

// Stop meeting
async function stopMeeting() {
  try {
    if (!currentMeeting) return;

    await flushFinalObservabilitySnapshot('before_close');

    // Stop audio capture
    stopAudioCapture();

    // Close WebSockets
    closeEventSocket();

    // Stop token refresh monitoring
    stopTokenRefreshMonitoring();

    // Close meeting on server
    await apiCloseMeeting(currentMeeting.meeting_id);
    await clearStoredMeeting();

    // Clear UI
    currentMeeting = null;
    clearTranscripts();
    clearInsights();
    clearQA();
    updateUI('disconnected');
    resetObservabilityState();

  } catch (error) {
    console.error('Error stopping meeting:', error);
    showError('Erro ao encerrar reunião: ' + formatError(error));
  }
}

// Refresh token
async function refreshToken() {
  try {
    if (!currentMeeting) return;

    const data = await apiRefreshToken(currentMeeting.meeting_id, currentMeeting.token);
    if (!data) {
      throw new Error('Falha ao renovar token');
    }

    currentMeeting.token = data.token;
    currentMeeting.event_url = data.event_url;
    currentMeeting.audio_url = data.audio_url;
    await setStoredMeeting(currentMeeting);

    reconnectAudioSocket();
    await connectEventSocket();

    showError('Token renovado com sucesso!');

  } catch (error) {
    console.error('Error refreshing token:', error);
    showError('Erro ao renovar token: ' + formatError(error));
  }
}

// Check meeting status
async function checkMeetingStatus() {
  if (!currentMeeting) return;

  try {
    const data = await apiGetMeetingStatus(currentMeeting.meeting_id, currentMeeting.token);
    if (data && data.status === 'open') {
      updateTokenExpiry(data.expires_in_seconds);
    } else {
      currentMeeting = null;
      await clearStoredMeeting();
      updateUI('disconnected');
    }
  } catch (error) {
    console.error('Error checking status:', error);
  }
}

// Start token refresh monitoring
function startTokenRefreshMonitoring() {
  tokenRefreshInterval = setInterval(async () => {
    if (!currentMeeting) return;

    const data = await apiGetMeetingStatus(currentMeeting.meeting_id, currentMeeting.token);
    if (data && data.status === 'open') {
      const expiresIn = data.expires_in_seconds;
      updateTokenExpiry(expiresIn);
      if (expiresIn < 3600) {
        await refreshToken();
      }
    }
  }, 5 * 60 * 1000);
}

// Stop token refresh monitoring
function stopTokenRefreshMonitoring() {
  if (tokenRefreshInterval) {
    clearInterval(tokenRefreshInterval);
    tokenRefreshInterval = null;
  }
}

// Resample float32 array to target sample rate (simple linear interpolation)
function resampleTo24k(inputFloat32, sourceSampleRate) {
  if (sourceSampleRate === 24000) return inputFloat32;
  const ratio = 24000 / sourceSampleRate;
  const outLength = Math.round(inputFloat32.length * ratio);
  const result = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const srcIndex = i / ratio;
    const idx = Math.floor(srcIndex);
    const frac = srcIndex - idx;
    const next = idx + 1 < inputFloat32.length ? inputFloat32[idx + 1] : inputFloat32[idx];
    result[i] = inputFloat32[idx] * (1 - frac) + next * frac;
  }
  return result;
}

function getFrameEnergyMetrics(frame) {
  let sumSq = 0;
  let peak = 0;
  for (let i = 0; i < frame.length; i++) {
    const v = frame[i];
    sumSq += v * v;
    const av = Math.abs(v);
    if (av > peak) peak = av;
  }
  const rms = frame.length > 0 ? Math.sqrt(sumSq / frame.length) : 0;
  return { rms, peak };
}

function shouldSendAudioFrame(frame, nowMs, frameDurationSeconds) {
  const metrics = getFrameEnergyMetrics(frame);
  const bypassReason = getVadBypassReason();
  const staticSpeechDetected = metrics.rms >= VAD_RMS_THRESHOLD || metrics.peak >= VAD_PEAK_THRESHOLD;

  if (bypassReason) {
    const vad = observabilityState.vad;
    vad.gatingActive = false;
    vad.bypassReason = bypassReason;
    recordVadFrame(metrics, true, frameDurationSeconds, staticSpeechDetected, bypassReason);
    return true;
  }

  if (!updateVadCalibration(metrics, nowMs)) {
    recordVadFrame(metrics, true, frameDurationSeconds, staticSpeechDetected, 'calibrating_noise');
    return true;
  }

  const vad = observabilityState.vad;
  vad.gatingActive = true;
  vad.bypassReason = null;

  const peakNeedsRms = Math.max(VAD_RMS_THRESHOLD, vad.noiseRmsFloor * 1.2);
  const speechDetected =
    metrics.rms >= vad.effectiveRmsThreshold ||
    (metrics.peak >= vad.effectivePeakThreshold && metrics.rms >= peakNeedsRms);

  if (speechDetected) {
    vadLastSpeechTs = nowMs;
  }

  const inHangover = (nowMs - vadLastSpeechTs) <= VAD_HANGOVER_MS;
  const shouldSend = speechDetected || inHangover;
  recordVadFrame(metrics, shouldSend, frameDurationSeconds, speechDetected);

  if (shouldSend && (nowMs - vadLastDebugTs) >= VAD_DEBUG_LOG_EVERY_MS) {
    console.log(
      `[VAD] sending audio rms=${metrics.rms.toFixed(4)} peak=${metrics.peak.toFixed(4)} ` +
        `thresholds=${vad.effectiveRmsThreshold.toFixed(4)}/${vad.effectivePeakThreshold.toFixed(4)}`
    );
    vadLastDebugTs = nowMs;
  }

  return shouldSend;
}

// Start audio capture using microphone + tab streams mixed together.
async function startAudioCaptureWithStreams(microphoneStream, activeTabStream) {
  if (!currentMeeting) return;
  micStream = microphoneStream;
  tabStream = activeTabStream;
  resetVadRuntimeState();

  // AudioContext pode iniciar "suspended" em alguns navegadores; ativar se necessário
  audioContext = new AudioContext();
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }

  let micSource = null;
  let tabSource = null;
  const mixGain = audioContext.createGain();
  mixGain.gain.value = 0.7;

  if (micStream) {
    micSource = audioContext.createMediaStreamSource(micStream);
    micSource.connect(mixGain);
  }

  if (tabStream) {
    tabSource = audioContext.createMediaStreamSource(tabStream);
    tabSource.connect(mixGain);
  }

  audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);
  const sourceRate = audioContext.sampleRate;

  audioProcessor.onaudioprocess = (e) => {
    if (!audioSocket || audioSocket.readyState !== WebSocket.OPEN) return;
    const inputData = e.inputBuffer.getChannelData(0);
    const nowMs = performance.now();
    const frameDurationSeconds = inputData.length / sourceRate;
    const shouldSend = shouldSendAudioFrame(inputData, nowMs, frameDurationSeconds);
    maybeFlushObservabilitySnapshot('audio');
    if (!shouldSend) return;
    const resampled = resampleTo24k(inputData, sourceRate);
    const pcm16 = new Int16Array(resampled.length);
    for (let i = 0; i < resampled.length; i++) {
      const s = Math.max(-1, Math.min(1, resampled[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    audioSocket.send(pcm16.buffer);
    observabilityState.vad.bytesSent += pcm16.byteLength;
  };

  mixGain.connect(audioProcessor);
  audioProcessor.connect(audioContext.destination);
  mixedSource = mixGain;

  const audioUrl = new URL(currentMeeting.audio_url);
  audioSocket = new WebSocket(audioUrl.href);
  audioSocket.onopen = () => console.log('Audio WebSocket connected');
  audioSocket.onerror = () => showError('Erro na conexão de áudio');
  audioSocket.onclose = () => console.log('Audio WebSocket closed');
}

// Reconnect only the audio WebSocket (e.g. after token refresh). Keeps same stream/context.
function reconnectAudioSocket() {
  if (audioSocket) {
    audioSocket.close();
    audioSocket = null;
  }
  if (!currentMeeting) return;
  const audioUrl = new URL(currentMeeting.audio_url);
  audioSocket = new WebSocket(audioUrl.href);
  audioSocket.onopen = () => console.log('Audio WebSocket reconnected');
  audioSocket.onerror = () => showError('Erro na conexão de áudio');
  audioSocket.onclose = () => console.log('Audio WebSocket closed');
}

// Stop audio capture
function stopAudioCapture() {
  if (micStream) {
    micStream.getTracks().forEach(track => track.stop());
    micStream = null;
  }

  if (tabStream) {
    tabStream.getTracks().forEach(track => track.stop());
    tabStream = null;
  }

  if (mixedSource) {
    mixedSource.disconnect();
    mixedSource = null;
  }

  if (audioProcessor) {
    audioProcessor.disconnect();
    audioProcessor = null;
  }

  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }

  if (audioSocket) {
    audioSocket.close();
    audioSocket = null;
  }

  resetVadRuntimeState();
}

// Connect event WebSocket
async function connectEventSocket() {
  try {
    if (eventSocket && eventSocket.readyState === WebSocket.OPEN) {
      eventSocket.close();
    }

    const wsUrl = new URL(currentMeeting.event_url);
    eventSocket = new WebSocket(wsUrl.href);

    eventSocket.onopen = () => {
      console.log('Event WebSocket connected');
    };

    eventSocket.onmessage = (event) => {
      try {
        if (!event || !event.data) return;
        const receivedAtEpochMs = Date.now();
        const renderStartMs = performance.now();
        const data = JSON.parse(event.data);
        const handledKind = handleEvent(data);
        const renderDurationMs = performance.now() - renderStartMs;
        recordUiLatency(data, handledKind, receivedAtEpochMs, Date.now(), renderDurationMs);
      } catch (error) {
        console.error('Error parsing event:', error);
      }
    };

    eventSocket.onerror = (error) => {
      console.error('Event WebSocket error:', error);
      showError('Erro na conexão de eventos');
    };

    eventSocket.onclose = () => {
      console.log('Event WebSocket closed');
      if (currentMeeting) {
        // Try to reconnect after 3 seconds
        setTimeout(() => {
          if (currentMeeting) {
            connectEventSocket();
          }
        }, 3000);
      }
    };

  } catch (error) {
    console.error('Error connecting event socket:', error);
    showError(`Erro ao conectar eventos: ${error.message}`);
  }
}

// Close event WebSocket
function closeEventSocket() {
  if (eventSocket) {
    eventSocket.close();
    eventSocket = null;
  }
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

function recordUiLatency(data, handledKind, receivedAtEpochMs, renderedAtEpochMs, renderDurationMs) {
  if (!handledKind) return;

  const serverEventAtMs = getEventTimestampMs(data);
  const transcriptCompletedAtMs = getTranscriptCompletedTimestampMs(data);
  const serverToUiMs = serverEventAtMs ? receivedAtEpochMs - serverEventAtMs : null;
  const transcriptToUiMs = transcriptCompletedAtMs ? renderedAtEpochMs - transcriptCompletedAtMs : null;

  observabilityState.ui.eventsRendered += 1;
  if (serverToUiMs !== null && serverToUiMs >= 0) {
    observabilityState.ui.serverToUiLatencySumMs += serverToUiMs;
  }
  if (transcriptToUiMs !== null && transcriptToUiMs >= 0) {
    observabilityState.ui.transcriptToUiLatencySumMs += transcriptToUiMs;
  }
  observabilityState.ui.uiRenderLatencySumMs += renderDurationMs;

  apiRecordObservability(
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

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Handle incoming events
function handleEvent(data) {
  if (!data || typeof data !== 'object') return null;
  const kind = data.kind || data.type;

  if (kind === 'transcript.completed') {
    const text = (data.text || data.transcript || '').trim();
    if (text) addTranscript(text);
    return 'transcript.completed';
  } else if (kind === 'insight') {
    addInsight(data);
    return 'insight';
  } else if (kind === 'qa') {
    addQA(data);
    return 'qa';
  }
  return null;
}

// UI update functions
function updateUI(state) {
  if (state === 'disconnected') {
    statusDot.className = 'status-dot disconnected';
    statusText.textContent = 'Desconectado';
    meetingInfo.classList.add('hidden');
    startBtn.classList.remove('hidden');
    stopBtn.classList.add('hidden');
    refreshBtn.classList.add('hidden');
  } else if (state === 'connecting') {
    statusDot.className = 'status-dot connecting';
    statusText.textContent = 'Conectando...';
    meetingInfo.classList.add('hidden');
    startBtn.classList.add('hidden');
    stopBtn.classList.add('hidden');
    refreshBtn.classList.add('hidden');
  } else if (state === 'connected') {
    statusDot.className = 'status-dot connected';
    statusText.textContent = 'Conectado';
    meetingInfo.classList.remove('hidden');
    meetingIdDisplay.textContent = currentMeeting.meeting_id.substring(0, 8) + '...';
    startBtn.classList.add('hidden');
    stopBtn.classList.remove('hidden');
    refreshBtn.classList.remove('hidden');
  }
}

function updateTokenExpiry(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  tokenExpiry.textContent = `${hours}h ${minutes}m`;
}

function addTranscript(text) {
  if (transcriptContainer.querySelector('.empty-state')) {
    transcriptContainer.innerHTML = '';
  }

  const p = document.createElement('p');
  p.className = 'transcript-item';
  p.textContent = text;
  transcriptContainer.appendChild(p);
  transcriptContainer.scrollTop = transcriptContainer.scrollHeight;
}

function addInsight(data) {
  if (insightsContainer.querySelector('.empty-state')) {
    insightsContainer.innerHTML = '';
  }

  const insights = data.insights || {};
  const summary = insights.summary || '';
  const keyPoints = insights.key_points || [];
  const actionItems = insights.actions || insights.action_items || [];
  const decisions = insights.decisions || [];
  const terms = insights.terms || [];

  const div = document.createElement('div');
  div.className = 'insight-item';

  let html = '';
  if (summary) {
    html += `<p><strong>Resumo:</strong> ${escapeHtml(summary)}</p>`;
  }
  if (keyPoints.length > 0) {
    html += `<p><strong>Pontos-chave:</strong> ${escapeHtml(keyPoints.join(', '))}</p>`;
  }
  if (actionItems.length > 0) {
    html += `<p><strong>Ações:</strong> ${escapeHtml(actionItems.join(', '))}</p>`;
  }
  if (decisions.length > 0) {
    html += `<p><strong>Decisões:</strong> ${escapeHtml(decisions.join(', '))}</p>`;
  }
  if (terms.length > 0) {
    const termRows = terms
      .map((t) => {
        if (typeof t === 'string') {
          const v = t.trim();
          if (!v) return '';
          return `<span class="term-name">${escapeHtml(v)}</span>`;
        }
        if (!t || typeof t !== 'object') return '';
        const term = (t.term || '').trim();
        const explain = (t.explain || '').trim();
        if (!term && !explain) return '';
        if (!term) return escapeHtml(explain);
        if (!explain) return `<span class="term-name">${escapeHtml(term)}</span>`;
        return `<span class="term-name">${escapeHtml(term)}</span>: ${escapeHtml(explain)}`;
      })
      .filter(Boolean)
      .join('<br>');
    if (termRows) {
      html += `<p><strong>Termos:</strong><br>${termRows}</p>`;
    }
  }

  if (!html) {
    html = '<p>Novo insight gerado</p>';
  }

  div.innerHTML = html;
  insightsContainer.appendChild(div);
  insightsContainer.scrollTop = insightsContainer.scrollHeight;
}

function addQA(data) {
  if (qaContainer.querySelector('.empty-state')) {
    qaContainer.innerHTML = '';
  }

  const qa = data.qa || {};
  const question = (qa.question || data.question || 'Pergunta').trim();
  const answer = (qa.answer || qa.answer_short || data.answer || data.response || '').trim();
  let confidence = '';
  if (typeof qa.confidence === 'string' && qa.confidence.trim()) {
    confidence = ` (confiança: ${qa.confidence.trim()})`;
  }

  const div = document.createElement('div');
  div.className = 'qa-item';
  div.innerHTML = `
    <strong>❓ ${escapeHtml(question)}</strong>
    <p>💡 ${escapeHtml(answer)}${confidence}</p>
  `;
  qaContainer.appendChild(div);
  qaContainer.scrollTop = qaContainer.scrollHeight;
}

function clearTranscripts() {
  transcriptContainer.innerHTML = '<p class="empty-state">Nenhuma transcrição ainda...</p>';
}

function clearInsights() {
  insightsContainer.innerHTML = '<p class="empty-state">Nenhum insight ainda...</p>';
}

function clearQA() {
  qaContainer.innerHTML = '<p class="empty-state">Nenhuma pergunta detectada ainda...</p>';
}

function showError(message) {
  if (message) {
    errorMessage.textContent = message;
    errorMessage.classList.remove('hidden');
  } else {
    errorMessage.classList.add('hidden');
  }
}

// Comunicação com o background não é mais usada para API/storage;
// tudo é feito na popup para evitar "message port closed".
