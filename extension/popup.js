// Popup script for Context AI Plugin
// Handles UI interactions and displays real-time data
// API e storage são feitos direto na popup para evitar "message port closed" do service worker

const API_BASE = 'http://localhost:8000';

// Client-side VAD (voice activity detection) to avoid sending long silence segments.
const VAD_ENABLED = true;
const VAD_RMS_THRESHOLD = 0.008;
const VAD_PEAK_THRESHOLD = 0.03;
const VAD_HANGOVER_MS = 700;
const VAD_DEBUG_LOG_EVERY_MS = 10000;

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

const STORAGE_KEYS = ['meeting_id', 'token', 'ws_url', 'audio_url'];

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

// Stop meeting
async function stopMeeting() {
  try {
    if (!currentMeeting) return;
    
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
    currentMeeting.ws_url = data.ws_url;
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

function shouldSendAudioFrame(frame, nowMs) {
  // When microphone is unavailable, tab audio can be lower/unstable.
  // In this mode, avoid client-side gating to prevent cutting speech.
  if (!VAD_ENABLED || !micStream) return true;

  const metrics = getFrameEnergyMetrics(frame);
  const speechDetected = metrics.rms >= VAD_RMS_THRESHOLD || metrics.peak >= VAD_PEAK_THRESHOLD;

  if (speechDetected) {
    vadLastSpeechTs = nowMs;
  }

  const inHangover = (nowMs - vadLastSpeechTs) <= VAD_HANGOVER_MS;
  const shouldSend = speechDetected || inHangover;

  if (shouldSend && (nowMs - vadLastDebugTs) >= VAD_DEBUG_LOG_EVERY_MS) {
    console.log(
      `[VAD] sending audio rms=${metrics.rms.toFixed(4)} peak=${metrics.peak.toFixed(4)}`
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
  vadLastSpeechTs = 0;
  vadLastDebugTs = 0;

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
    if (!shouldSendAudioFrame(inputData, nowMs)) return;
    const resampled = resampleTo24k(inputData, sourceRate);
    const pcm16 = new Int16Array(resampled.length);
    for (let i = 0; i < resampled.length; i++) {
      const s = Math.max(-1, Math.min(1, resampled[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    audioSocket.send(pcm16.buffer);
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

  vadLastSpeechTs = 0;
  vadLastDebugTs = 0;
}

// Connect event WebSocket
async function connectEventSocket() {
  try {
    if (eventSocket && eventSocket.readyState === WebSocket.OPEN) {
      eventSocket.close();
    }
    
    const wsUrl = new URL(currentMeeting.ws_url);
    eventSocket = new WebSocket(wsUrl.href);
    
    eventSocket.onopen = () => {
      console.log('Event WebSocket connected');
    };
    
    eventSocket.onmessage = (event) => {
      try {
        if (!event || !event.data) return;
        const data = JSON.parse(event.data);
        handleEvent(data);
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

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Handle incoming events
function handleEvent(data) {
  if (!data || typeof data !== 'object') return;
  const kind = data.kind || data.type;
  
  if (kind === 'transcript.completed') {
    const text = (data.text || data.transcript || '').trim();
    if (text) addTranscript(text);
  } else if (kind === 'insight') {
    addInsight(data);
  } else if (kind === 'qa') {
    addQA(data);
  }
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
