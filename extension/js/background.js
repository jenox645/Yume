// ============================================================================
// BACKGROUND SERVICE WORKER (Manifest V3) - Yume v5.0.0
// Handles server health, transcription, translation, romanization proxy
// Translation & romanization are SEPARATE calls (never combined)
// ============================================================================

// Deterministic romanization for Japanese kana (wanakana.js — ja only)
try { importScripts('lib/wanakana.min.js'); } catch (e) { console.warn('[Background] wanakana not loaded:', e.message); }

console.log('[Background] Service worker initializing...');

// ============================================================================
// TRANSLATION CACHE — avoids re-translating identical text (chorus, repeated lines)
// ============================================================================

const translationCache = new Map();
const TRANSLATION_CACHE_MAX = 500;

const romanizationCache = new Map();
const ROMANIZATION_CACHE_MAX = 300;

function _getCachedTranslation(text) {
  const hit = translationCache.get(text);
  if (hit) {
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
  translationCache.set(text, { value });
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

// Authenticated fetch to whisper server (auto-includes API token)
async function _whisperFetch(url, options = {}, timeoutMs = 30000) {
  const { settings } = await chrome.storage.local.get(['settings']);
  const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
  const token = await _getApiToken(whisperUrl);
  const headers = { ...options.headers };
  if (token) headers['X-API-Token'] = token;
  return _fetchWithTimeout(url, { ...options, headers }, timeoutMs);
}

// ============================================================================
// INSTALLATION
// ============================================================================

chrome.runtime.onInstalled.addListener((details) => {
  console.log('[Background] Extension installed/updated:', details.reason);
  chrome.storage.local.set({
    settings: {
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
    },
    installTime: Date.now(),
    version: '5.0.0'
  });
  setTimeout(() => discoverSettingsFromWhisper(), 3000);
});

// ============================================================================
// AUTO-DISCOVER SETTINGS FROM WHISPER SERVER
// ============================================================================

async function discoverSettingsFromWhisper() {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 3000);
    // First call: unauthenticated — gets token only
    const response = await fetch(`${whisperUrl}/health`, { signal: controller.signal });
    if (!response.ok) return;
    const data = await response.json();
    // Discover API token
    if (data.api_token) {
      apiToken = data.api_token;
      try { await chrome.storage.session.set({ apiToken }); } catch (e) { console.warn('[Yume]', e.message || e); }
      console.log('[Background] API token discovered on startup');
      // Re-call with token to get full health (translation_url etc)
      try {
        const fullResp = await fetch(`${whisperUrl}/health`, {
          headers: { 'X-API-Token': apiToken }
        });
        if (fullResp.ok) {
          const full = await fullResp.json();
          if (full.translation_url && full.translation_port) {
            const updated = { ...settings, translationUrl: full.translation_url, translationPort: full.translation_port };
            await chrome.storage.local.set({ settings: updated });
            console.log('[Background] Auto-discovered translation URL:', full.translation_url);
          }
        }
      } catch (e) { console.warn('[Yume]', e.message || e); }
    }
  } catch (e) {
    console.log('[Background] Auto-discover skipped (whisper not up yet)');
  }
}

// ============================================================================
// MESSAGE HANDLING
// ============================================================================

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message.type || message.action) {
    case 'CHECK_SERVER':
      handleCheckServer(message.url, sendResponse);
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

async function handleCheckServer(url, sendResponse) {
  try {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), 5000);
    // Health endpoint is exempt from token — but include token for other checks
    const headers = {};
    if (apiToken) headers['X-API-Token'] = apiToken;
    const response = await fetch(url, { method: 'GET', signal: controller.signal, headers });
    clearTimeout(tid);
    let data = null;
    try {
      if (response.ok) {
        data = await response.json();
        // Discover API token from health response
        if (data.api_token && !apiToken) {
          apiToken = data.api_token;
          try { await chrome.storage.session.set({ apiToken }); } catch (e) { console.warn('[Yume]', e.message || e); }
        }
      }
    } catch (e) { console.warn('[Yume]', e.message || e); }
    sendResponse({ healthy: response.ok, status: response.status, data });
  } catch (error) { sendResponse({ healthy: false, error: error.message }); }
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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

function _buildTranslationPrompt(srcLang, targetLang) {
  const src = LANG_NAMES[srcLang] || LANG_NAMES['ja'];
  const tgt = targetLang || 'English';
  return `You are a ${src}-to-${tgt} translation system. Output ONLY the ${tgt} translation. Do NOT respond to the content. Do NOT add explanations. Do NOT answer questions. ONLY translate ${src} to ${tgt}.`;
}

function _cleanTranslation(text) {
  let c = text.trim();
  c = c.replace(/^.{1,40}\s+translates?\s+to\s+/i, '');
  c = c.replace(/^translation:\s*/i, '');
  c = c.replace(/^line\s*\d+:\s*/i, '');
  c = c.replace(/^(Romanization|Romaji|English)\s*[:：]\s*/i, '');
  c = c.replace(/^["'「」『』]+|["'「」『』]+$/g, '');
  c = c.replace(/^.{1,30}\s+means?\s+["']?/i, '');
  return c.trim();
}

async function handleTranslate(text, targetLang, sourceLang, sendResponse) {
  try {
    // Check translation cache first
    const cacheKey = `${sourceLang || 'ja'}:${targetLang || 'en'}:${text}`;
    const cached = _getCachedTranslation(cacheKey);
    if (cached) {
      sendResponse({ success: true, translation: cached });
      return;
    }

    const { settings } = await chrome.storage.local.get(['settings']);
    const translationUrl = settings?.translationUrl || 'http://localhost:5000';
    const lang = targetLang || settings?.targetLanguage || 'English';
    const srcLang = sourceLang || settings?.sourceLanguage || 'ja';

    // Short, single-purpose prompt — no romanization here
    const systemPrompt = _buildTranslationPrompt(srcLang, lang);

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
    const translationUrl = settings?.translationUrl || 'http://localhost:5000';
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
    const srcLang = sourceLang || settings?.sourceLanguage || 'ja';

    const system = ROMANIZATION_SYSTEMS[srcLang];
    if (!system) {
      sendResponse({ success: true, romanization: '' });
      return;
    }

    // Check romanization cache
    const cacheKey = `${srcLang}:${text}`;
    const cached = romanizationCache.get(cacheKey);
    if (cached) {
      romanizationCache.delete(cacheKey);
      romanizationCache.set(cacheKey, cached); // move to end (LRU)
      sendResponse({ success: true, romanization: cached });
      return;
    }

    // Strategy 1: Deterministic romanization (ja/zh — instant, no GPU, no hallucination)
    let romanization = '';
    if (srcLang === 'ja' || srcLang === 'zh') {
      try {
        const detResp = await _whisperFetch(`${whisperUrl}/romanize`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, language: srcLang })
        }, 5000);
        if (detResp.ok) {
          const detData = await detResp.json();
          if (detData.romanization) {
            romanization = detData.romanization;
          }
        }
      } catch (e) {
        // Deterministic endpoint unavailable — fall through to LLM
      }
    }

    // Strategy 2: LLM romanization (fallback for all languages, or if deterministic unavailable)
    if (!romanization) {
      const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: "system", content: system.prompt },
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
        romanization = result.choices[0].message.content.trim();
        romanization = romanization.replace(/^(Romaji|Pinyin|Romanization|Transliteration)\s*[:：]\s*/i, '').trim();
      }
    }

    // Cache the result
    if (romanization) {
      if (romanizationCache.size >= ROMANIZATION_CACHE_MAX) {
        romanizationCache.delete(romanizationCache.keys().next().value);
      }
      romanizationCache.set(cacheKey, romanization);
    }

    sendResponse({ success: true, romanization });
  } catch (error) { sendResponse({ success: false, error: error.message, romanization: '' }); }
}

// ============================================================================
// BATCH TRANSLATION
// ============================================================================

function _buildBatchPrompt(srcLang, targetLang) {
  const src = LANG_NAMES[srcLang] || LANG_NAMES['ja'];
  const tgt = targetLang || 'English';
  return `You are a ${src}-to-${tgt} subtitle translator. You receive numbered ${src} lines and output ONLY the ${tgt} translations with matching numbers. Rules:
- Translate EVERY line, one translation per line
- Keep the [N] numbering exactly
- Do NOT skip, merge, or reorder lines
- Do NOT add explanations or notes
- Output ONLY translations, nothing else`;
}

async function handleTranslateBatch(batchText, count, sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const translationUrl = settings?.translationUrl || 'http://localhost:5000';
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
      const cacheKey = `${srcLang}:${tgtLang}:${text}`;
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
    const response = await _fetchWithTimeout(`${translationUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [
          { role: "system", content: _buildBatchPrompt(srcLang, tgtLang) },
          { role: "user", content: uncachedLines.join('\n') }
        ],
        max_tokens: 1000,
        temperature: 0.1,
        stream: false
      })
    }, 120000);  // 120s for consumer LLM batch
    if (!response.ok) throw new Error(`Translation error: ${response.status}`);
    const result = await response.json();
    let raw = result.choices?.[0]?.message?.content || '';
    const newTranslations = _parseBatchResponse(raw.trim(), uncachedLines.length);

    // Map results back and populate cache
    for (let i = 0; i < uncachedIndices.length; i++) {
      const { originalIdx, text } = uncachedIndices[i];
      const translation = (newTranslations[i] || '').trim();
      results[originalIdx] = translation;
      if (translation) {
        _setCachedTranslation(`${srcLang}:${tgtLang}:${text}`, translation);
      }
    }

    sendResponse({ success: true, translations: results });
  } catch (error) { sendResponse({ success: false, error: error.message }); }
}

function _parseBatchResponse(raw, expectedCount) {
  const lines = raw.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  const result = new Array(expectedCount).fill('');
  for (const line of lines) {
    const m = line.match(/^\[(\d+)\]\s*(.+)/) || line.match(/^(\d+)[.)]\s*(.+)/);
    if (m) {
      const idx = parseInt(m[1]) - 1;
      if (idx >= 0 && idx < expectedCount) result[idx] = m[2].trim();
    }
  }
  // Return whatever was correctly parsed by [N] markers.
  // No positional fallback — misaligned translations are worse than missing ones.
  return result;
}

// ============================================================================
// MODELS
// ============================================================================

async function handleListModels(sendResponse) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
    const url = whisperUrl || 'http://localhost:5001';
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
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
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
        chrome.tabs.sendMessage(tab.id, { action: 'TOGGLE_SUBTITLES' }, (response) => {
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
