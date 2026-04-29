// Background service worker for Context AI Plugin.
// Meeting lifecycle, API calls, and storage are handled directly in popup.js.

function enableSidePanelOnActionClick() {
  if (!chrome.sidePanel || !chrome.sidePanel.setPanelBehavior) {
    return;
  }

  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((error) => {
    console.warn('Failed to set side panel behavior:', error);
  });
}

chrome.runtime.onInstalled.addListener(() => {
  enableSidePanelOnActionClick();
});

chrome.runtime.onStartup.addListener(() => {
  enableSidePanelOnActionClick();
});
