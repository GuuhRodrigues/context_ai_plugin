// Background service worker for Context AI Plugin
// Manages meeting lifecycle and WebSocket connections

const API_BASE = 'http://localhost:8000'; // TODO: make configurable

// Storage keys
const STORAGE_MEETING_ID = 'meeting_id';
const STORAGE_TOKEN = 'token';
const STORAGE_WS_URL = 'ws_url';
const STORAGE_AUDIO_URL = 'audio_url';
const STORAGE_API_BASE = 'api_base';

// Initialize: load API base from storage or use default
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get([STORAGE_API_BASE], (result) => {
    if (!result[STORAGE_API_BASE]) {
      chrome.storage.local.set({ [STORAGE_API_BASE]: API_BASE });
    }
  });

  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((error) => {
      console.warn('Failed to set side panel behavior:', error);
    });
  }
});

// Create a new meeting
async function createMeeting() {
  try {
    const result = await chrome.storage.local.get([STORAGE_API_BASE]);
    const apiBase = result[STORAGE_API_BASE] || API_BASE;
    
    const response = await fetch(`${apiBase}/meetings`, {
      method: 'POST',
    });
    
    if (!response.ok) {
      throw new Error(`Failed to create meeting: ${response.statusText}`);
    }
    
    const data = await response.json();
    
    // Save meeting data
    await chrome.storage.local.set({
      [STORAGE_MEETING_ID]: data.meeting_id,
      [STORAGE_TOKEN]: data.token,
      [STORAGE_WS_URL]: data.ws_url,
      [STORAGE_AUDIO_URL]: data.audio_url,
    });
    
    return data;
  } catch (error) {
    console.error('Error creating meeting:', error);
    throw error;
  }
}

// Get meeting status
async function getMeetingStatus(meetingId, token) {
  try {
    const result = await chrome.storage.local.get([STORAGE_API_BASE]);
    const apiBase = result[STORAGE_API_BASE] || API_BASE;
    
    const response = await fetch(`${apiBase}/meetings/${meetingId}?token=${token}`);
    
    if (!response.ok) {
      if (response.status === 401) {
        return { error: 'Invalid or expired token' };
      }
      throw new Error(`Failed to get status: ${response.statusText}`);
    }
    
    return await response.json();
  } catch (error) {
    console.error('Error getting meeting status:', error);
    throw error;
  }
}

// Refresh meeting token
async function refreshMeetingToken(meetingId, currentToken) {
  try {
    const result = await chrome.storage.local.get([STORAGE_API_BASE]);
    const apiBase = result[STORAGE_API_BASE] || API_BASE;
    
    const response = await fetch(`${apiBase}/meetings/${meetingId}/refresh`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ token: currentToken }),
    });
    
    if (!response.ok) {
      if (response.status === 401) {
        return { error: 'Invalid or expired token, or meeting closed' };
      }
      throw new Error(`Failed to refresh token: ${response.statusText}`);
    }
    
    const data = await response.json();
    
    // Update stored meeting data
    await chrome.storage.local.set({
      [STORAGE_TOKEN]: data.token,
      [STORAGE_WS_URL]: data.ws_url,
      [STORAGE_AUDIO_URL]: data.audio_url,
    });
    
    return data;
  } catch (error) {
    console.error('Error refreshing token:', error);
    throw error;
  }
}

// Close meeting
async function closeMeeting(meetingId) {
  try {
    const result = await chrome.storage.local.get([STORAGE_API_BASE]);
    const apiBase = result[STORAGE_API_BASE] || API_BASE;
    
    const response = await fetch(`${apiBase}/meetings/${meetingId}/close`, {
      method: 'POST',
    });
    
    if (!response.ok) {
      throw new Error(`Failed to close meeting: ${response.statusText}`);
    }
    
    // Clear stored meeting data
    await chrome.storage.local.remove([
      STORAGE_MEETING_ID,
      STORAGE_TOKEN,
      STORAGE_WS_URL,
      STORAGE_AUDIO_URL,
    ]);
    
    return await response.json();
  } catch (error) {
    console.error('Error closing meeting:', error);
    throw error;
  }
}

// Message handler from popup/content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'createMeeting') {
    createMeeting()
      .then((data) => sendResponse({ success: true, data }))
      .catch((error) => sendResponse({ success: false, error: error.message }));
    return true; // async response
  }
  
  if (request.action === 'getMeetingStatus') {
    getMeetingStatus(request.meetingId, request.token)
      .then((data) => sendResponse({ success: true, data }))
      .catch((error) => sendResponse({ success: false, error: error.message }));
    return true;
  }
  
  if (request.action === 'refreshToken') {
    refreshMeetingToken(request.meetingId, request.token)
      .then((data) => sendResponse({ success: true, data }))
      .catch((error) => sendResponse({ success: false, error: error.message }));
    return true;
  }
  
  if (request.action === 'closeMeeting') {
    closeMeeting(request.meetingId)
      .then((data) => sendResponse({ success: true, data }))
      .catch((error) => sendResponse({ success: false, error: error.message }));
    return true;
  }
  
  if (request.action === 'getStoredMeeting') {
    chrome.storage.local.get([
      STORAGE_MEETING_ID,
      STORAGE_TOKEN,
      STORAGE_WS_URL,
      STORAGE_AUDIO_URL,
    ], (result) => {
      sendResponse({ success: true, data: result });
    });
    return true;
  }

  if (request.action === 'testConnection') {
    chrome.storage.local.get([STORAGE_API_BASE], async (result) => {
      const apiBase = result[STORAGE_API_BASE] || API_BASE;
      try {
        const res = await fetch(apiBase + '/health');
        const ok = res.ok;
        const text = await res.text();
        sendResponse({ success: ok, data: ok ? text : res.status + ' ' + text });
      } catch (e) {
        sendResponse({ success: false, error: e.message });
      }
    });
    return true;
  }
});
