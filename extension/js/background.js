// ============================================================================
// BACKGROUND (MV3 service worker / Firefox event page)
//
// A thin authenticated proxy to the local Yume server. The server runs the whole
// subtitle pipeline (download, Whisper, translation, romanization, caching);
// content scripts and the popup reach it through here because only extension
// pages may call it (CORS) and only this worker holds the API token.
// ============================================================================

const DEFAULT_SETTINGS = {
  whisperUrl: 'http://localhost:5001',
  whisperPort: 5001,
  showOriginal: true,
  showEnglish: true,
  showRomaji: false,
  showChunkCounter: true,
  sourceLanguage: 'ja',
  targetLanguage: 'English',
};

// Central URL resolution — never hardcode localhost:5001 elsewhere
function _whisperUrl(settings) { return settings?.whisperUrl || DEFAULT_SETTINGS.whisperUrl; }

async function _settings() {
  const { settings } = await chrome.storage.local.get(['settings']);
  return settings || {};
}

chrome.runtime.onInstalled.addListener((details) => {
  chrome.storage.local.get(['settings'], (result) => {
    // Fresh install: defaults. Update: keep the user's values, add new keys.
    const merged = { ...DEFAULT_SETTINGS, ...(result.settings || {}) };
    chrome.storage.local.set({ settings: merged, version: chrome.runtime.getManifest().version });
  });
  console.log('[Background] Installed/updated:', details.reason);
});

// ============================================================================
// API TOKEN — discovered from /health (which only hands it to extension
// origins), kept in session storage so it survives worker restarts.
// ============================================================================

let apiToken = null;

async function _getApiToken(base, refresh = false) {
  if (apiToken && !refresh) return apiToken;
  if (!refresh) {
    try {
      const stored = await chrome.storage.session.get(['apiToken']);
      if (stored.apiToken) { apiToken = stored.apiToken; return apiToken; }
    } catch (_e) { /* session storage unavailable — rediscover */ }
  }
  try {
    const resp = await _fetchWithTimeout(`${base}/health`, {}, 3000);
    const data = await resp.json();
    if (data.api_token) {
      apiToken = data.api_token;
      try { await chrome.storage.session.set({ apiToken }); } catch (_e) { /* best effort */ }
    }
  } catch (_e) { /* server down */ }
  return apiToken;
}

function _fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// Server paths the extension may call. Anything else is refused, so a content
// script (running on arbitrary pages) cannot be turned into a generic proxy.
const _ALLOWED_PATH = /^\/(health|stats|jobs(\/[a-f0-9]{32}(\/(options|export))?)?|library(\/(export|delete))?|blacklist(\/(update|add|remove))?|model\/switch|translation\/(models|health|test)|cache\/clear)$/;

// Call the server: { ok, status, data } — never throws.
async function serverRequest({ method = 'GET', path, query, body, timeout = 15000 }) {
  if (!_ALLOWED_PATH.test(path || '')) return { ok: false, status: 0, data: { error: `Path not allowed: ${path}` } };
  const base = _whisperUrl(await _settings());
  const qs = query ? '?' + new URLSearchParams(query).toString() : '';
  const send = async (token) => {
    const headers = {};
    if (token) headers['X-API-Token'] = token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    return _fetchWithTimeout(`${base}${path}${qs}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    }, timeout);
  };
  try {
    let resp = await send(await _getApiToken(base));
    if (resp.status === 403) {
      // Server restarted with a new token — rediscover once and retry
      resp = await send(await _getApiToken(base, true));
    }
    let data = null;
    try { data = await resp.json(); } catch (_e) { data = {}; }
    return { ok: resp.ok, status: resp.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { error: e.name === 'AbortError' ? 'Request timed out' : 'Yume server not reachable — is it running?' } };
  }
}

// ============================================================================
// START / STOP — through the native messaging host (yume/native_host.py),
// registered by "python pocket_yume.py autostart on". Without it the user
// starts Yume from the launcher as before.
// ============================================================================

const NATIVE_HOST = 'com.pocketyume.yume';
const _YUME_COMMANDS = new Set(['status', 'start', 'stop']);

// { ok, state, message, last_error, managed } | { ok: false, unavailable: true, error }
function yumeCommand(cmd) {
  if (!_YUME_COMMANDS.has(cmd)) return Promise.resolve({ ok: false, error: `Unknown command: ${cmd}` });
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd }, (resp) => {
        const err = chrome.runtime.lastError;
        if (err || !resp) {
          // Not registered / Python missing / host crashed: fall back to the launcher
          resolve({ ok: false, unavailable: true, error: err?.message || 'No reply from the Yume helper' });
        } else {
          if (cmd === 'start' || cmd === 'stop') {
            apiToken = null;  // the next server run mints a new token
            chrome.storage.session.remove('apiToken').catch(() => {});
          }
          resolve(resp);
        }
      });
    } catch (e) {
      resolve({ ok: false, unavailable: true, error: e.message });
    }
  });
}

// ============================================================================
// MESSAGES
// ============================================================================

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;  // only our own scripts
  if (message.type === 'API') {
    const req = { ...message.request };
    // Jobs carry the tab title as translation context (song name, artist, topic)
    if (req.path === '/jobs' && req.method === 'POST' && sender.tab?.title) {
      req.body = { ...req.body, title: req.body?.title || sender.tab.title };
    }
    serverRequest(req).then(sendResponse);
    return true;
  }
  if (message.type === 'YUME') {
    yumeCommand(message.cmd).then(sendResponse);
    return true;
  }
  if (message.type === 'PING') {
    sendResponse({ pong: true });
    return false;
  }
  sendResponse({ ok: false, data: { error: 'Unknown message type' } });
  return false;
});

// ============================================================================
// KEYBOARD SHORTCUT (Alt+Y → toggle subtitles on the active tab)
// ============================================================================

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'toggle-subtitles') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    chrome.tabs.sendMessage(tab.id, { action: 'TOGGLE_SUBTITLES' }).catch(() => {
      /* no content script on this tab (browser page) */
    });
  }
});
