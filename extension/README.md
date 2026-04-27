# Context AI Plugin - Browser Extension

Browser extension (Chrome/Edge) for the Context AI Plugin realtime meeting copilot.

## Installation

### Development (unpacked mode)

1. Open Chrome/Edge.
2. Go to `chrome://extensions/` (or `edge://extensions/`).
3. Enable Developer Mode.
4. Click `Load unpacked`.
5. Select the `extension/` folder from this repository.

## Configuration

By default, the extension connects to `http://localhost:8000`.

## Usage

1. Open the extension popup.
2. Click `Iniciar Reuniao`.
3. Allow microphone access.
4. Keep the meeting tab active with audio enabled.
5. Follow transcript, insights, and QA sections in realtime.
6. Use `Renovar Token` for long meetings.
7. Use `Encerrar Reuniao` to close and cleanup.

## Structure

- `manifest.json`: extension config (Manifest V3)
- `background.js`: minimal service worker for side panel behavior
- `popup.html`: popup UI
- `popup.js`: popup logic, audio capture, WebSockets
- `content.js`: content script placeholder
- `styles.css`: popup styles

## Notes

- Tries `tabCapture` first, then fallback capture methods.
- Requests microphone and mixes mic + tab audio.
- Uses client-side VAD to reduce silence upload (`VAD_*` constants in `popup.js`), including initial noise calibration and explicit bypass reporting.
- Sends VAD/UI latency snapshots to the backend observability endpoint.
- Reconnects WebSockets automatically on transient failures.
- Renews token automatically when remaining TTL is below 1 hour.
- Stores current meeting data in `chrome.storage.local`.
- Closing a meeting generates JSONL, metrics JSON, and Markdown report files under `observability/meetings/{meeting_id}/`.
