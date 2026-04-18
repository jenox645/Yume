// ============================================================================
// BACKGROUND SERVICE WORKER (Manifest V3) - Yume v0.0.9
// Handles server health, transcription, translation, romanization proxy
// Translation & romanization are SEPARATE calls (never combined)
// ============================================================================

console.log('[Background] Service worker initializing...');

// ============================================================================
// TRANSLATION CACHE — avoids re-translating identical text (chorus, repeated lines)
// ============================================================================

const translationCache = new Map();
const TRANSLATION_CACHE_MAX = 500;
const TRANSLATION_CACHE_TTL = 30 * 60 * 1000; // 30 min — forces re-translate after model switch

const romanizationCache = new Map();
const ROMANIZATION_CACHE_MAX = 300;
const ROMANIZATION_CACHE_TTL = 30 * 60 * 1000; // 30 min — matches translation cache

function _getCachedTranslation(text) {
  const hit = translationCache.get(text);
  if (hit) {
    // TTL check — stale entries evicted (user may have switched models)
    if (hit.ts && Date.now() - hit.ts > TRANSLATION_CACHE_TTL) {
      translationCache.delete(text);
      return null;
    }
    // Move to end of Map for true LRU (Map preserves insertion order)
    translationCache.delete(text);
    translationCache.set(text, hit);
    return hit.value;
  }
  return null;
}

function _setCachedTranslation(text, value) {
  if (translationCache.size >= TRANSLATION_CACHE_MAX) {
    const oldestKey = translationCache.keys().next().value;
    translationCache.delete(oldestKey);
  }
  translationCache.set(text, { value, ts: Date.now() });
}


// Fetch with AbortController timeout (prevents hung LLM calls blocking pipeline)
function _fetchWithTimeout(url, options, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

// ============================================================================
// SECURITY: API token for authenticated server requests
// Discovered from /health endpoint, stored in session storage.
// Prevents DNS rebinding and CSRF attacks against localhost server.
// ============================================================================
let apiToken = null;

async function _getApiToken(whisperUrl) {
  if (apiToken) return apiToken;
  // Try session storage first (survives service worker restart)
  try {
    const stored = await chrome.storage.session.get(['apiToken']);
    if (stored.apiToken) { apiToken = stored.apiToken; return apiToken; }
  } catch (e) { console.warn('[Yume]', e.message || e); }
  // Discover from health endpoint (exempt from token check)
  try {
    const resp = await fetch(`${whisperUrl}/health`);
    if (resp.ok) {
      const data = await resp.json();
      if (data.api_token) {
        apiToken = data.api_token;
        try { await chrome.storage.session.set({ apiToken }); } catch (e) { console.warn('[Yume]', e.message || e); }
        console.log('[Background] API token discovered from server');
      }
    }
  } catch (e) { console.warn('[Yume]', e.message || e); }
  return apiToken;
}

// Authenticated fetch to whisper server (auto-includes API token).
// If the server returns 403 (stale token after server restart), we clear
// the cached token, re-discover via /health, and retry ONCE.
async function _whisperFetch(url, options = {}, timeoutMs = 30000) {
  const { settings } = await chrome.storage.local.get(['settings']);
  const whisperUrl = _whisperUrl(settings);
  const token = await _getApiToken(whisperUrl);
  const headers = { ...options.headers };
  if (token) headers['X-API-Token'] = token;
  const resp = await _fetchWithTimeout(url, { ...options, headers }, timeoutMs);

  // 403 = server rejected our token (likely restarted with a new one)
  if (resp.status === 403) {
    console.warn('[Background] 403 — stale API token, re-discovering...');
    // Clear cached token so _getApiToken re-fetches from /health
    apiToken = null;
    try { await chrome.storage.session.remove(['apiToken']); } catch (_e) { /* ok */ }
    const newToken = await _getApiToken(whisperUrl);
    if (newToken && newToken !== token) {
      console.log('[Background] New API token acquired, retrying request');
      const retryHeaders = { ...options.headers };
      retryHeaders['X-API-Token'] = newToken;
      return _fetchWithTimeout(url, { ...options, headers: retryHeaders }, timeoutMs);
    }
  }

  return resp;
}

// ============================================================================
// INSTALLATION — only set defaults on fresh install; merge on update
// ============================================================================

const DEFAULT_SETTINGS = {
  whisperUrl: 'http://localhost:5001',
  whisperPort: 5001,
  translationUrl: 'http://localhost:5000',
  translationPort: 5000,
  chunkDuration: 30,
  showOriginal: true,
  showEnglish: true,
  showRomaji: false,
  showChunkCounter: true,
  sourceLanguage: 'ja',
  targetLanguage: 'English',
  autoStart: false,
  debugMode: true
};

// Central URL resolution — avoids hardcoded localhost:port scattered across functions
function _whisperUrl(settings) { return settings?.whisperUrl || DEFAULT_SETTINGS.whisperUrl; }
function _translationUrl(settings) { return settings?.translationUrl || DEFAULT_SETTINGS.translationUrl; }

chrome.runtime.onInstalled.addListener((details) => {
  console.log('[Background] Extension installed/updated:', details.reason);
  if (details.reason === 'install') {
    // Fresh install — set all defaults
    chrome.storage.local.set({ settings: { ...DEFAULT_SETTINGS }, installTime: Date.now(), version: '0.0.9' });
  } else if (details.reason === 'update') {
    // Update — merge new defaults into existing settings (preserves user changes)
    chrome.storage.local.get(['settings'], (result) => {
      const existing = result.settings || {};
      const merged = { ...DEFAULT_SETTINGS, ...existing };
      chrome.storage.local.set({ settings: merged, version: '0.0.9' });
      console.log('[Background] Settings preserved across update');
    });
  }
  setTimeout(() => discoverSettingsFromWhisper(), 3000);
});

// ============================================================================
// AUTO-DISCOVER SETTINGS FROM WHISPER SERVER
// ============================================================================

async function discoverSettingsFromWhisper() {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 3000);
    // Health returns 503 during loading but still includes api_token
    const response = await fetch(`${whisperUrl}/health`, { signal: controller.signal });
    let data;
    try { data = await response.json(); } catch (_e) { return; }
    // Discover API token (available even during model loading)
    if (data.api_token) {
      apiToken = data.api_token;
      try { await chrome.storage.session.set({ apiToken }); } catch (e) { console.warn('[Yume]', e.message || e); }
      console.log('[Background] API token discovered on startup');
      // Re-call with token to get full health (translation_url etc) — only if ready
      if (response.ok) {
        try {
          const fullResp = await fetch(`${whisperUrl}/health`, {
            headers: { 'X-API-Token': apiToken }
          });
          if (fullResp.ok) {
            const full = await fullResp.json();
            if (full.translation_url && full.translation_port) {
              const updated = {
                ...settings,
                translationUrl: full.translation_url,
                translationPort: full.translation_port,
              };
              // Auto-discover prompts from CLI config (if set)
              if (full.translation_prompt !== undefined) updated.translationPrompt = full.translation_prompt;
              if (full.romanization_prompt !== undefined) updated.romanizationPrompt = full.romanization_prompt;
              await chrome.storage.local.set({ settings: updated });
              console.log('[Background] Auto-discovered translation URL:', full.translation_url);
              if (full.translation_prompt) console.log('[Background] Auto-discovered custom translation prompt');
              if (full.romanization_prompt) console.log('[Background] Auto-discovered custom romanization prompt');
            }
          }
        } catch (e) { console.warn('[Yume]', e.message || e); }
      }
    }
  } catch (_e) {
    console.log('[Background] Auto-discover skipped (whisper not up yet)');
  }
}

// ============================================================================
// MESSAGE HANDLING
// ============================================================================

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message.type || message.action) {
    case 'CHECK_SERVER':
      handleCheckServer(message.url, sendResponse, message.isWhisper === true);
      return true;
    case 'GET_SETTINGS':
      handleGetSettings(sendResponse);
      return true;
    case 'SAVE_SETTINGS':
      handleSaveSettings(message.settings, sendResponse);
      return true;
    case 'TRANSCRIBE':
      handleTranscribe(message.audio, message.language, sendResponse);
      return true;
    case 'TRANSCRIBE_URL':
      handleTranscribeUrl(message, sendResponse);
      return true;
    case 'PREPARE_VIDEO':
      handlePrepareVideo(message.url, message.video_id, sendResponse);
      return true;
    case 'PREPARE_DIRECT':
      handlePrepareDirect(message.stream_url, message.video_id, sendResponse);
      return true;
    case 'TRANSLATE':
      handleTranslate(message.text, message.targetLang, message.sourceLang, sendResponse);
      return true;
    case 'ROMANIZE':
      handleRomanize(message.text, message.sourceLang, sendResponse);
      return true;
    case 'ROMANIZE_BATCH':
      handleRomanizeBatch(message.texts, message.sourceLang, sendResponse);
      return true;
    case 'TRANSLATE_BATCH':
      handleTranslateBatch(message.text, message.count, sendResponse);
      return true;
    case 'LIST_MODELS':
      handleListModels(sendResponse);
      return true;
    case 'CLEAR_SERVER_CACHE':
      handleClearServerCache(sendResponse);
      return true;
    case 'CLEAR_TRANSLATION_CACHE':
      translationCache.clear();
      romanizationCache.clear();
      console.log('[Background] Translation + romanization caches cleared');
      sendResponse({ success: true });
      return true;
    case 'UPDATE_BLACKLIST':
      handleUpdateBlacklist(message.blacklist, message.whisperUrl, sendResponse);
      return true;
    case 'FETCH_PATTERNS':
      handleFetchPatterns(sendResponse);
      return true;
    case 'LIST_FONTS':
      handleListFonts(sendResponse);
      return true;
    case 'PING':
      sendResponse({ pong: true, timestamp: Date.now() });
      return false;
    default:
      sendResponse({ error: 'Unknown message type' });
      return false;
  }
});

// ============================================================================
// SERVER HEALTH CHECKS
// ============================================================================

async function handleCheckServer(url, sendResponse, isWhisperServer = false) {
  try {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), 5000);
    const headers = {};
    // Only attach Whisper API token when checking Whisper server (caller specifies)
    if (apiToken && isWhisperServer) {
      headers['X-API-Token'] = apiToken;
    }
    const response = await fetch(url, { method: 'GET', signal: controller.signal, headers });
    clearTimeout(tid);
    let data = null;
    try {
      const text = await response.text();
      // Try JSON parse — fall back to treating any 200 response as healthy
      try { data = JSON.parse(text); } catch (_e) { data = { raw: text.substring(0, 200) }; }
      // Discover API token from Whisper health response
      if (data.api_token && !apiToken) {
        apiToken = data.api_token;
        try { await chrome.storage.session.set({ apiToken }); } catch (e) { console.warn('[Yume]', e.message || e); }
      }
    } catch (e) { console.warn('[Yume]', e.message || e); }
    const loading = data?.status === 'loading';
    sendResponse({ healthy: response.ok, loading, status: response.status, data });
  } catch (error) {
    // HTTP timed out — try a quick TCP connect to see if the port is at least open.
    // This catches the case where the LLM server is busy with inference (10-20s)
    // and can't respond to HTTP, but IS running. Without this, the popup shows
    // red dot during every translation, which is misleading.
    if (!isWhisperServer) {
      try {
        const urlObj = new URL(url);
        const tcpUrl = `${urlObj.protocol}//${urlObj.host}`;
        const tcpController = new AbortController();
        const tcpTimer = setTimeout(() => tcpController.abort(), 2000);
        const tcpResp = await fetch(tcpUrl, { method: 'HEAD', signal: tcpController.signal });
        clearTimeout(tcpTimer);
        // Any response (even 404) means the server is running, just busy
        sendResponse({ healthy: true, loading: false, status: tcpResp.status, data: { busy: true } });
        return;
      } catch (_tcpErr) {
        // TCP also failed — server is genuinely down
      }
    }
    sendResponse({ healthy: false, loading: false, error: error.message });
  }
}

// ============================================================================
// SETTINGS
// ============================================================================

function handleGetSettings(sendResponse) {
  chrome.storage.local.get(['settings'], (result) => {
    sendResponse({ settings: result.settings || {} });
  });
}

function handleSaveSettings(settings, sendResponse) {
  chrome.storage.local.set({ settings }, () => sendResponse({ success: true }));
}

// ============================================================================
// TRANSCRIPTION PROXY
// ============================================================================

async function handleTranscribe(audioBase64, language, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/transcribe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio: audioBase64, language: language || 'ja' })
    });
    if (!response.ok) throw new Error(`Whisper error: ${response.status}`);
    const result = await response.json();
    sendResponse({ success: true, result });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

async function handleTranscribeUrl(msg, sendResponse) {
  _startKeepAlive();
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const body = {
      url: msg.url,
      video_id: msg.video_id,
      chunk_index: msg.chunk_index,
      chunk_duration: msg.chunk_duration || 30,
      step_size: msg.step_size || 25,
    };
    const lang = msg.language || 'ja';
    if (lang !== 'auto') body.language = lang;

    const response = await _whisperFetch(`${whisperUrl}/transcribe_url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }, 120000);
    if (!response.ok) throw new Error(`Whisper error: ${response.status}`);
    const result = await response.json();
    sendResponse({ success: true, result });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
  finally { _stopKeepAlive(); }
}

async function handlePrepareVideo(url, videoId, sendResponse) {
  _startKeepAlive();
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, video_id: videoId })
    }, 300000);
    if (!response.ok) {
      // Read the actual error message from the server
      let errMsg = `Prepare failed: ${response.status}`;
      try {
        const errData = await response.json();
        if (errData.error) errMsg = errData.error;
      } catch (e) { console.warn('[Yume]', e.message || e); }
      throw new Error(errMsg);
    }
    const result = await response.json();
    sendResponse({ success: true, ...result });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
  finally { _stopKeepAlive(); }
}

async function handlePrepareDirect(streamUrl, videoId, sendResponse) {
  _startKeepAlive();
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/prepare_direct`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stream_url: streamUrl, video_id: videoId })
    }, 300000);
    if (!response.ok) {
      let errMsg = `Direct prepare failed: ${response.status}`;
      try { const d = await response.json(); if (d.error) errMsg = d.error; } catch (e) { console.warn('[Yume]', e.message || e); }
      throw new Error(errMsg);
    }
    const result = await response.json();
    sendResponse({ success: true, ...result });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
  finally { _stopKeepAlive(); }
}

// ============================================================================
// LANGUAGE MAPS
// ============================================================================

const LANG_NAMES = {
  ja: 'Japanese', zh: 'Chinese', ko: 'Korean', en: 'English',
  fr: 'French', de: 'German', es: 'Spanish', pt: 'Portuguese',
  ru: 'Russian', it: 'Italian', ar: 'Arabic', auto: 'the detected language'
};

// Per-language romanization systems — each has its own short assertive prompt
const ROMANIZATION_SYSTEMS = {
  'ja': {
    name: 'romaji',
    prompt: 'You are a Japanese romaji converter. Output ONLY the romaji reading of the Japanese input. Do NOT translate. Do NOT add explanations. ONLY romaji.'
  },
  'zh': {
    name: 'pinyin',
    prompt: 'You are a Chinese pinyin converter. Output ONLY the pinyin with tone marks (ā á ǎ à) of the Chinese input. Do NOT translate. Do NOT add explanations. ONLY pinyin.'
  },
  'ko': {
    name: 'romanization',
    prompt: 'You are a Korean romanization converter using Revised Romanization. Output ONLY the romanized Korean input. Do NOT translate. Do NOT add explanations. ONLY romanization.'
  },
  'ru': {
    name: 'transliteration',
    prompt: 'You are a Russian to Latin transliteration converter. Output ONLY the Latin transliteration of the Russian input. Do NOT translate. Do NOT add explanations. ONLY transliteration.'
  },
  'ar': {
    name: 'transliteration',
    prompt: 'You are an Arabic to Latin transliteration converter. Output ONLY the Latin transliteration of the Arabic input. Do NOT translate. Do NOT add explanations. ONLY transliteration.'
  },
};

// ============================================================================
// TRANSLATION (short assertive prompt — NEVER includes romanization)
// ============================================================================

function _buildTranslationPrompt(srcLang, targetLang, customPrompt) {
  const src = LANG_NAMES[srcLang] || LANG_NAMES['ja'];
  const tgt = targetLang || 'English';
  // Use custom prompt from settings if available (set via CLI → Settings → Translation prompt)
  if (customPrompt) {
    return customPrompt.replace(/\{src\}/g, src).replace(/\{tgt\}/g, tgt);
  }
  return `You are a ${src}-to-${tgt} translation system. Output ONLY the ${tgt} translation. Do NOT respond to the content. Do NOT add explanations. Do NOT answer questions. ONLY translate ${src} to ${tgt}.`;
}

const _CLEAN_TEXT_MAX_LENGTH = 2000;

// Context snippet: last translated sentence carried into the next batch prompt.
// NOTE: module-level — cleared on service worker unload (acceptable; next batch cold-starts
// gracefully). Not keyed per-tab: concurrent multi-tab use may see cross-tab context bleed,
// which is a minor coherence issue. A chrome.storage.session[tabId] fix would resolve both
// but requires making _buildBatchPrompt async — deferred.
let _lastTranslatedSentence = '';

function _cleanTranslation(text) {
  if (text.length > _CLEAN_TEXT_MAX_LENGTH) return text.trim();
  let c = text.trim();
  const matched = [];
  let t;
  t = c; c = c.replace(/^.{1,40}\s+translates?\s+to\s+/i, '');            if (c !== t) matched.push('translates-to');
  t = c; c = c.replace(/^translation:\s*/i, '');                            if (c !== t) matched.push('translation-prefix');
  t = c; c = c.replace(/^line\s*\d+:\s*/i, '');                             if (c !== t) matched.push('line-prefix');
  t = c; c = c.replace(/^(Romanization|Romaji|English)\s*[:：]\s*/i, '');   if (c !== t) matched.push('lang-label');
  t = c; c = c.replace(/^["'「」『』]+|["'「」『』]+$/g, '');                  if (c !== t) matched.push('strip-quotes');
  t = c; c = c.replace(/^.{1,30}\s+means?\s+["']?/i, '');                  if (c !== t) matched.push('means');
  if (matched.length > 0) {
    console.warn(`[CleanTranslation] patterns=[${matched.join(',')}] in="${text.substring(0, 80)}"`);
  }
  return c.trim();
}

// Clean verbose LLM romanization output to extract just the romaji/pinyin/transliteration.
// LLMs often output: "The romaji for X is: **Y** (explanation)" — we need just Y.
function _cleanRomanization(text) {
  if (!text) return '';
  if (text.length > _CLEAN_TEXT_MAX_LENGTH) return text.trim();
  let c = text.trim();

  // Strip markdown bold/italic
  c = c.replace(/\*{1,3}/g, '');

  // Try to extract from common LLM patterns:
  // "...is converted to romaji as: RESULT (explanation)" → RESULT
  // "...romaji: RESULT" → RESULT
  // "The romaji reading is: RESULT" → RESULT
  const extractPatterns = [
    /(?:romaji|pinyin|romanization|transliteration|latin)\s*(?:is|reading|version)?[:\s]+["']?([^"'\n(]+)/i,
    /(?:converted|written)\s+(?:to|as|in)\s+(?:romaji|latin|pinyin)?[:\s]*["']?([^"'\n(]+)/i,
    /^["']([^"'\n]+)["']\s*$/,  // Quoted result on its own line
  ];
  for (const pattern of extractPatterns) {
    const m = c.match(pattern);
    if (m && m[1]) {
      const extracted = m[1].trim();
      // Only use if it looks like actual romanization (mostly Latin chars)
      if (extracted.length > 0 && /^[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùü\s',.\-!?]+$/.test(extracted)) {
        c = extracted;
        break;
      }
    }
  }

  // Strip common prefixes
  c = c.replace(/^(Romaji|Pinyin|Romanization|Transliteration|Reading|Result)\s*[:：]\s*/i, '');
  // Strip parenthetical notes
  c = c.replace(/\s*\([^)]*\)\s*/g, ' ');
  // Strip "No translation needed" and similar
  c = c.replace(/no\s+(translation|explanation)\s+(needed|required|necessary)/gi, '');
  // Strip remaining quotes
  c = c.replace(/^["'「」『』]+|["'「」『』]+$/g, '');
  // Collapse whitespace
  c = c.replace(/\s+/g, ' ').trim();

  return c;
}

async function handleTranslate(text, targetLang, sourceLang, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const translationUrl = _translationUrl(settings);
    const lang = targetLang || settings?.targetLanguage || 'English';
    const srcLang = sourceLang || settings?.sourceLanguage || 'ja';

    // Check translation cache first (key includes URL so model switches invalidate)
    const cacheKey = `${srcLang}||${lang}||${translationUrl}||${text}`;
    const cached = _getCachedTranslation(cacheKey);
    if (cached) {
      sendResponse({ success: true, translation: cached });
      return;
    }

    // Short, single-purpose prompt — no romanization here
    // Custom prompt from CLI settings takes priority if set
    const systemPrompt = _buildTranslationPrompt(srcLang, lang, settings?.translationPrompt);

    const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: text }
        ],
        max_tokens: 200,
        temperature: 0.1,
        stream: false
      })
    }, 120000);  // 120s — consumer LLMs (12B Q6_K) need 15-60s per batch

    if (!response.ok) throw new Error(`Translation server error: ${response.status}`);
    const result = await response.json();
    let translation = '';
    if (result.choices?.[0]?.message?.content) {
      translation = _cleanTranslation(result.choices[0].message.content);
    }
    if (translation) _setCachedTranslation(cacheKey, translation);
    sendResponse({ success: true, translation });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

// ============================================================================
// ROMANIZATION (completely separate from translation)
// ============================================================================

async function handleRomanize(text, sourceLang, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const translationUrl = _translationUrl(settings);
    const whisperUrl = _whisperUrl(settings);
    const srcLang = sourceLang || settings?.sourceLanguage || 'ja';

    const system = ROMANIZATION_SYSTEMS[srcLang];
    if (!system) {
      sendResponse({ success: true, romanization: '' });
      return;
    }

    // Check romanization cache (with TTL)
    const cacheKey = `roma||${srcLang}||${text}`;
    const cached = romanizationCache.get(cacheKey);
    if (cached) {
      // Support both old format (plain string) and new format ({value, ts})
      const val = typeof cached === 'string' ? cached : cached.value;
      const ts = typeof cached === 'string' ? 0 : (cached.ts || 0);
      if (ts && Date.now() - ts > ROMANIZATION_CACHE_TTL) {
        romanizationCache.delete(cacheKey);
      } else {
        romanizationCache.delete(cacheKey);
        romanizationCache.set(cacheKey, cached); // move to end (LRU)
        sendResponse({ success: true, romanization: val });
        return;
      }
    }

    // Strategy 1: Deterministic romanization (ja/zh/ko — instant, no GPU, no hallucination)
    let romanization = '';
    if (srcLang === 'ja' || srcLang === 'zh' || srcLang === 'ko') {
      try {
        const detResp = await _whisperFetch(`${whisperUrl}/romanize`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, language: srcLang })
        }, 60000);
        if (detResp.ok) {
          const detData = await detResp.json();
          if (detData.romanization) {
            romanization = detData.romanization;
          }
        }
      } catch (_e) {
        // Deterministic endpoint unavailable — fall through to LLM
      }
    }

    // Strategy 2: LLM romanization (fallback for all languages, or if deterministic unavailable)
    if (!romanization) {
      // Use custom prompt from settings if available, else built-in per-language prompt
      const customRomaPrompt = settings?.romanizationPrompt;
      const systemPrompt = customRomaPrompt
        ? customRomaPrompt.replace(/\{src\}/g, LANG_NAMES[srcLang] || srcLang).replace(/\{sys\}/g, system.name)
        : system.prompt;

      const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: text }
          ],
          max_tokens: 200,
          temperature: 0.1,
          stream: false
        })
      }, 120000);

      if (!response.ok) throw new Error(`Romanization error: ${response.status}`);
      const result = await response.json();
      if (result.choices?.[0]?.message?.content) {
        romanization = _cleanRomanization(result.choices[0].message.content);
      }
    }

    // Cache the result (with timestamp for TTL)
    if (romanization) {
      if (romanizationCache.size >= ROMANIZATION_CACHE_MAX) {
        romanizationCache.delete(romanizationCache.keys().next().value);
      }
      romanizationCache.set(cacheKey, { value: romanization, ts: Date.now() });
    }

    sendResponse({ success: true, romanization });
  } catch (error) { sendResponse({ success: false, error: error.message, romanization: '' }); }
}

// ============================================================================
// BATCH ROMANIZATION — single round trip for N segments (ja/zh/ko deterministic)
// Falls back to sequential LLM calls for unsupported languages (ru/ar)
// ============================================================================

async function handleRomanizeBatch(texts, sourceLang, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const translationUrl = _translationUrl(settings);
    const srcLang = sourceLang || settings?.sourceLanguage || 'ja';

    if (!texts || texts.length === 0) {
      sendResponse({ success: true, romanizations: [] });
      return;
    }

    // Check cache for each text
    const results = new Array(texts.length).fill('');
    const uncachedTexts = [];
    const uncachedIndices = [];

    for (let i = 0; i < texts.length; i++) {
      const cacheKey = `roma||${srcLang}||${texts[i]}`;
      const cached = romanizationCache.get(cacheKey);
      if (cached) {
        const val = typeof cached === 'string' ? cached : cached.value;
        const ts = typeof cached === 'string' ? 0 : (cached.ts || 0);
        if (ts && Date.now() - ts > ROMANIZATION_CACHE_TTL) {
          romanizationCache.delete(cacheKey);
          uncachedTexts.push(texts[i]);
          uncachedIndices.push(i);
        } else {
          romanizationCache.delete(cacheKey);
          romanizationCache.set(cacheKey, cached);
          results[i] = val;
        }
      } else {
        uncachedTexts.push(texts[i]);
        uncachedIndices.push(i);
      }
    }

    if (uncachedTexts.length === 0) {
      sendResponse({ success: true, romanizations: results });
      return;
    }

    // Strategy 1: Batch deterministic (ja/zh/ko — single HTTP call)
    if (srcLang === 'ja' || srcLang === 'zh' || srcLang === 'ko') {
      try {
        const detResp = await _whisperFetch(`${whisperUrl}/romanize_batch`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ texts: uncachedTexts, language: srcLang })
        }, 60000);
        if (detResp.ok) {
          const detData = await detResp.json();
          if (detData.romanizations && detData.romanizations.length === uncachedTexts.length) {
            for (let i = 0; i < uncachedIndices.length; i++) {
              const roma = (detData.romanizations[i] || '').trim();
              results[uncachedIndices[i]] = roma;
              if (roma) {
                const cacheKey = `roma||${srcLang}||${uncachedTexts[i]}`;
                if (romanizationCache.size >= ROMANIZATION_CACHE_MAX) {
                  romanizationCache.delete(romanizationCache.keys().next().value);
                }
                romanizationCache.set(cacheKey, { value: roma, ts: Date.now() });
              }
            }
            sendResponse({ success: true, romanizations: results });
            return;
          }
        }
      } catch (e) {
        console.warn('[Background] Batch romanization fallback to sequential:', e.message);
      }
    }

    // Strategy 2: Sequential LLM romanization (ru/ar, or fallback)
    const system = ROMANIZATION_SYSTEMS[srcLang];
    if (!system) {
      sendResponse({ success: true, romanizations: results });
      return;
    }
    // Use custom prompt from settings if available
    const customRomaPrompt = settings?.romanizationPrompt;
    const romaSystemPrompt = customRomaPrompt
      ? customRomaPrompt.replace(/\{src\}/g, LANG_NAMES[srcLang] || srcLang).replace(/\{sys\}/g, system.name)
      : system.prompt;

    // BATCH LLM romanization — single API call instead of N sequential calls.
    // Uses the same [N] numbered format as batch translation.
    const batchRomaText = uncachedTexts.map((t, i) => `[${i + 1}] ${t}`).join('\n');
    const batchRomaPrompt = `${romaSystemPrompt}\nYou receive numbered lines and output ONLY the ${system.name} with matching [N] numbers. One result per line. No explanations.`;

    try {
      const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: "system", content: batchRomaPrompt },
            { role: "user", content: batchRomaText }
          ],
          max_tokens: Math.min(2048, Math.max(500, 200 * uncachedTexts.length)),
          temperature: 0.1, stream: false
        })
      }, 120000);

      if (response.ok) {
        const result = await response.json();
        const raw = (result.choices?.[0]?.message?.content || '').trim();
        const parsedRoma = _parseBatchResponse(raw, uncachedTexts.length);

        for (let i = 0; i < uncachedIndices.length; i++) {
          let roma = _cleanRomanization(parsedRoma[i] || '');
          results[uncachedIndices[i]] = roma;
          if (roma) {
            const cacheKey = `roma||${srcLang}||${uncachedTexts[i]}`;
            if (romanizationCache.size >= ROMANIZATION_CACHE_MAX) {
              romanizationCache.delete(romanizationCache.keys().next().value);
            }
            romanizationCache.set(cacheKey, { value: roma, ts: Date.now() });
          }
        }
      }
    } catch (e) {
      console.warn('[Background] Batch LLM romanization failed:', e.message);
      // Fallback: individual calls only if batch completely failed
      for (let i = 0; i < uncachedTexts.length; i++) {
        try {
          const resp = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              messages: [
                { role: "system", content: romaSystemPrompt },
                { role: "user", content: uncachedTexts[i] }
              ],
              max_tokens: 200, temperature: 0.1, stream: false
            })
          }, 30000);
          if (resp.ok) {
            const r = await resp.json();
            let roma = _cleanRomanization(r.choices?.[0]?.message?.content || '');
            results[uncachedIndices[i]] = roma;
            if (roma) {
              const cacheKey = `roma||${srcLang}||${uncachedTexts[i]}`;
              if (romanizationCache.size >= ROMANIZATION_CACHE_MAX) {
                romanizationCache.delete(romanizationCache.keys().next().value);
              }
              romanizationCache.set(cacheKey, { value: roma, ts: Date.now() });
            }
          }
        } catch (_e2) { /* skip */ }
      }
    }

    sendResponse({ success: true, romanizations: results });
  } catch (error) { sendResponse({ success: false, error: error.message, romanizations: [] }); }
}

// ============================================================================
// BATCH TRANSLATION
// ============================================================================

function _buildBatchPrompt(srcLang, targetLang, customPrompt, priorContext) {
  const src = LANG_NAMES[srcLang] || LANG_NAMES['ja'];
  const tgt = targetLang || 'English';
  const base = customPrompt
    ? customPrompt.replace(/\{src\}/g, src).replace(/\{tgt\}/g, tgt)
    : `Translate ${src} to ${tgt}.`;
  // ~320 chars ≈ 80 tokens — skip oversized context snippets
  const ctx = (priorContext && priorContext.length <= 320) ? `Prior context: ${priorContext}\n` : '';
  return `${ctx}${base} Output ONLY numbered lines matching [N] input. Nothing else.`;
}

async function handleTranslateBatch(batchText, count, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const translationUrl = _translationUrl(settings);
    const srcLang = settings?.sourceLanguage || 'ja';
    const tgtLang = settings?.targetLanguage || 'English';

    // Parse input lines and check cache for each
    const inputLines = batchText.split('\n').map(l => l.trim()).filter(l => l);
    const results = new Array(count).fill('');
    const uncachedLines = [];
    const uncachedIndices = [];

    for (let i = 0; i < inputLines.length; i++) {
      const m = inputLines[i].match(/^\[(\d+)\]\s*(.+)/);
      if (!m) continue;
      const idx = parseInt(m[1]) - 1;
      const text = m[2].trim();
      const cacheKey = `${srcLang}||${tgtLang}||${translationUrl}||${text}`;
      const cached = _getCachedTranslation(cacheKey);
      if (cached) {
        results[idx] = cached;
      } else {
        uncachedLines.push(`[${uncachedLines.length + 1}] ${text}`);
        uncachedIndices.push({ originalIdx: idx, text });
      }
    }

    // If everything was cached, return immediately
    if (uncachedLines.length === 0) {
      sendResponse({ success: true, translations: results });
      return;
    }

    // Send only uncached lines to LLM
    const batchMessages = [
      { role: "system", content: _buildBatchPrompt(srcLang, tgtLang, settings?.translationPrompt, _lastTranslatedSentence) },
      { role: "user", content: uncachedLines.join('\n') },
      // Forced assistant prefix: steers model to continue from [1] instead of generating preamble.
      // Remove this message if the backend rejects partial assistant turns.
      { role: "assistant", content: "[1]" }
    ];
    const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: batchMessages,
        max_tokens: Math.min(2048, Math.max(500, 150 * uncachedLines.length)),
        temperature: 0.1,
        stream: false
      })
    }, 120000);  // 120s for consumer LLM batch
    if (!response.ok) throw new Error(`Translation error: ${response.status}`);
    const result = await response.json();
    // Restore the forced [1] prefix the model continued from
    let raw = '[1]' + (result.choices?.[0]?.message?.content || '');
    const newTranslations = _parseBatchResponse(raw.trim(), uncachedLines.length);

    // Map results back and populate cache
    for (let i = 0; i < uncachedIndices.length; i++) {
      const { originalIdx, text } = uncachedIndices[i];
      const translation = (newTranslations[i] || '').trim();
      results[originalIdx] = translation;
      if (translation) {
        _setCachedTranslation(`${srcLang}||${tgtLang}||${translationUrl}||${text}`, translation);
      }
    }

    // Save last translated sentence for next batch's prior-context snippet
    const lastNonEmpty = results.filter(Boolean).pop();
    if (lastNonEmpty) {
      // Extract the last sentence fragment (split on . ! ? or use full string if short)
      const sentences = lastNonEmpty.split(/(?<=[.!?])\s+/);
      _lastTranslatedSentence = sentences[sentences.length - 1].trim();
    }

    sendResponse({ success: true, translations: results });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

function _parseBatchResponse(raw, expectedCount) {
  const lines = raw.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  const result = new Array(expectedCount).fill('');

  // Strategy 1: Parse by [N] or N) markers (preferred — unambiguous)
  let markerHits = 0;
  for (const line of lines) {
    const m = line.match(/^\[(\d+)\]\s*(.+)/) || line.match(/^(\d+)[.)]\s*(.+)/);
    if (m) {
      const idx = parseInt(m[1]) - 1;
      if (idx >= 0 && idx < expectedCount) {
        result[idx] = _cleanTranslation(m[2].trim());
        markerHits++;
      }
    }
  }

  // Strategy 2: Positional fallback — if LLM returned plain lines without numbering
  // Only use when NO markers were found (avoids mixing strategies)
  if (markerHits === 0 && lines.length > 0) {
    console.log(`[Background] Batch: no [N] markers found in ${lines.length} lines, using positional fallback`);
    for (let i = 0; i < Math.min(lines.length, expectedCount); i++) {
      const cleaned = _cleanTranslation(lines[i]);
      if (cleaned) result[i] = cleaned;
    }
  }

  return result;
}

// ============================================================================
// MODELS
// ============================================================================

async function handleListModels(sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/translation/models`, {}, 8000);
    if (!response.ok) throw new Error(`Model list failed: ${response.status}`);
    const data = await response.json();
    sendResponse({ success: true, ...data });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

// ============================================================================
// FONT LISTING (scan extension/fonts/ folder)
// ============================================================================

async function handleListFonts(sendResponse) {
  try {
    // We can't enumerate extension directory from service worker,
    // but we can try to fetch known font files
    // The popup will send font filenames found via manifest web_accessible_resources
    sendResponse({ success: true, note: 'Use manifest web_accessible_resources for font listing' });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

// ============================================================================
// CACHE
// ============================================================================

async function handleClearServerCache(sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/cache/clear`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }
    });
    if (response.ok) {
      const result = await response.json();
      sendResponse({ success: true, cleared: result.cleared });
    } else {
      sendResponse({ success: false, error: `Server returned ${response.status}` });
    }
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

// ============================================================================
// HALLUCINATION BLACKLIST
// ============================================================================

async function handleUpdateBlacklist(blacklist, whisperUrl, sendResponse) {
  try {
    const url = whisperUrl || DEFAULT_SETTINGS.whisperUrl;
    const response = await _whisperFetch(`${url}/blacklist/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ blacklist: blacklist || [] })
    });
    if (response.ok) {
      const result = await response.json();
      sendResponse({ success: true, count: result.count });
    } else {
      sendResponse({ success: false, error: `Server returned ${response.status}` });
    }
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

async function handleFetchPatterns(sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = _whisperUrl(settings);
    const response = await _whisperFetch(`${whisperUrl}/hallucination_patterns`, {}, 5000);
    if (!response.ok) throw new Error(`Pattern fetch failed: ${response.status}`);
    const data = await response.json();
    // Return full structured response so client can use all fields
    sendResponse({ success: true, data });
  } catch (error) {
    sendResponse({ success: false, error: error.message });
  }
}

// ============================================================================
// KEYBOARD SHORTCUT (Alt+Y → toggle subtitles on active tab)
// ============================================================================

chrome.commands.onCommand.addListener(async (command) => {
  if (command === 'toggle-subtitles') {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab?.id) {
        chrome.tabs.sendMessage(tab.id, { action: 'TOGGLE_SUBTITLES' }, (_response) => {
          if (chrome.runtime.lastError) {
            console.log('[Background] Shortcut: no content script on this tab');
          }
        });
      }
    } catch (e) {
      console.log('[Background] Shortcut error:', e.message);
    }
  }
});

// ============================================================================
// KEEP-ALIVE (scoped — only active during server operations)
// Per review: global keepAlive wastes resources; scope it to active operations.
// ============================================================================

let keepAliveInterval = null;
let activeOperations = 0;

function _startKeepAlive() {
  activeOperations++;
  if (!keepAliveInterval) {
    keepAliveInterval = setInterval(() => { chrome.runtime.getPlatformInfo(() => {}); }, 25000);
  }
}

function _stopKeepAlive() {
  activeOperations = Math.max(0, activeOperations - 1);
  if (activeOperations === 0 && keepAliveInterval) {
    clearInterval(keepAliveInterval);
    keepAliveInterval = null;
  }
}

console.log('[Background] Service worker ready (scoped keepAlive)');
