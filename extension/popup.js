// Yume - Popup Script
// Settings UI + views of the server's state (status, blacklist, library, stats).
// Everything server-side goes through background.js ({type: 'API'}), which holds
// the API token.

// ============================================================================
// HELPERS
// ============================================================================

function api(request) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'API', request }, (resp) => {
      resolve(chrome.runtime.lastError ? { ok: false, status: 0, data: { error: chrome.runtime.lastError.message } } : resp);
    });
  });
}

function yume(cmd) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'YUME', cmd }, (resp) => {
      resolve(chrome.runtime.lastError || !resp ? { ok: false, unavailable: true } : resp);
    });
  });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function sendToTab(message) {
  const tab = await activeTab();
  if (!tab?.id) throw new Error('No active tab');
  return chrome.tabs.sendMessage(tab.id, message);
}

// Escapes quotes too — results are also interpolated into attribute values.
function _escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function showToast(message, type = 'success') {
  document.querySelectorAll('.yume-toast').forEach(t => t.remove());
  const toast = document.createElement('div');
  toast.className = `yume-toast ${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 1500);
}

function flash(el, text, kind = '', ms = 4000) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status-message ' + kind;
  if (ms) setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, ms);
}

function downloadText(content, filename, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ============================================================================
// CONSTANTS
// ============================================================================

const ROMA_LABELS = {
  ja: { name: 'Romaji', hint: '(Romaji)' },
  zh: { name: 'Pinyin', hint: '(Pinyin)' },
  ko: { name: 'Romanization', hint: '(Romanization)' },
  ru: { name: 'Transliteration', hint: '(Latin)' },
  ar: { name: 'Transliteration', hint: '(Latin, via the LLM)' },
};

const LATIN_TARGETS = [
  'English', 'French', 'Spanish', 'German', 'Portuguese',
  'Italian', 'Indonesian', 'Vietnamese', 'Turkish', 'Dutch', 'Polish'
];

const CJK_FONTS = {
  ja: ['Noto Sans JP', 'Noto Serif JP', 'M PLUS Rounded 1c', 'Kosugi Maru',
       'Zen Maru Gothic', 'Yu Gothic', 'Meiryo', 'MS Gothic', 'Hiragino Sans'],
  zh: ['Noto Sans SC', 'Noto Sans TC', 'Microsoft YaHei', 'PingFang SC',
       'SimHei', 'STHeiti', 'Source Han Sans CN'],
  ko: ['Noto Sans KR', 'Malgun Gothic', 'Apple SD Gothic Neo', 'NanumGothic'],
  ar: ['Noto Naskh Arabic', 'Amiri', 'Scheherazade New', 'Tahoma', 'Arial'],
};
const GENERIC_FONTS = ['Arial', 'Georgia', 'Verdana', 'Segoe UI', 'Consolas', 'Times New Roman', 'Courier New', 'Helvetica'];

const DEFAULTS = {
  whisperUrl: 'http://localhost:5001', whisperPort: 5001,
  showOriginal: true, showEnglish: true, showRomaji: false,
  showChunkCounter: true, showConfidence: false,
  sourceLanguage: 'ja', targetLanguage: 'English',
  windowTitle: 'Yume', windowBgColor: '#08080c', windowOpacity: 88, windowShadow: true,
  originalFont: '', romajiFont: '', translationFont: '',
  originalFontSize: 19, originalColor: '#ffffff',
  romajiFontSize: 13, romajiColor: '#ffc88c',
  translationFontSize: 14, translationColor: '#b4beff',
  timingOffset: 0,
  glassEnabled: false, glassBlur: 18, glassRadius: 20,
};

// Settings that moved to the server (translation runs there now) or died with
// the old pipeline. Removed from storage once so they cannot confuse anything.
const OBSOLETE_SETTINGS = [
  'chunkDuration', 'translationUrl', 'translationPort', 'translationPrompt', 'romanizationPrompt',
  'sessionRestoreMinutes', 'libraryRetentionDays', 'libraryMaxVideos', 'autoStart', 'debugMode', 'showJapanese',
];

// ============================================================================
// STARTUP
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  $('yumeVersion').textContent = 'v' + chrome.runtime.getManifest().version;
  restoreCollapsibleState();
  setupCollapsibleSections();
  populateFontDropdowns();
  setupEventListeners();
  migrateStorage().catch(e => console.warn('[Yume] migration:', e.message));
  loadSettings().catch(e => console.error('[Yume] loadSettings failed:', e));
  checkServers().catch(e => console.error('[Yume] checkServers failed:', e));
  checkSubtitleStatus().catch(e => console.warn('[Yume]', e.message));
  showShortcutHint();
});

// One-time cleanup of data the old in-browser pipeline kept locally.
async function migrateStorage() {
  const all = await chrome.storage.local.get(null);
  const settings = all.settings || {};
  if (OBSOLETE_SETTINGS.some(k => k in settings)) {
    for (const k of OBSOLETE_SETTINGS) delete settings[k];
    await chrome.storage.local.set({ settings });
  }
  // The blacklist used to live in the browser and be pushed to the server only
  // on demand (overwriting whatever the CLI had added). Merge it into the server.
  const local = all.hallucinationBlacklist || [];
  if (local.length) {
    let ok = true;
    for (const text of local) {
      const r = await api({ method: 'POST', path: '/blacklist/add', body: { text } });
      if (!r.ok) { ok = false; break; }
    }
    if (ok) await chrome.storage.local.remove('hallucinationBlacklist');
  }
  // The old per-browser subtitle library (yumeLib_*) is superseded by the server cache
  const libKeys = Object.keys(all).filter(k => k.startsWith('yumeLib_'));
  if (libKeys.length) await chrome.storage.local.remove(libKeys);
}

function showShortcutHint() {
  const el = document.getElementById('shortcutHint');
  if (!el || !chrome.commands?.getAll) return;
  chrome.commands.getAll((cmds) => {
    const cmd = (cmds || []).find(c => c.name === 'toggle-subtitles');
    el.textContent = cmd?.shortcut
      ? `Keyboard shortcut: ${cmd.shortcut} (works on the video page)`
      : 'Tip: set a toggle shortcut at chrome://extensions/shortcuts';
  });
}

// ============================================================================
// COLLAPSIBLE SECTIONS
// ============================================================================

function loadSectionData(sid) {
  const warn = (e) => console.warn('[Yume]', e.message);
  if (sid === 'model') loadModels().catch(warn);
  else if (sid === 'diagnostics') fetchDiagnostics().catch(warn);
  else if (sid === 'hallucination') loadBlacklist().catch(warn);
  else if (sid === 'stats') startStatsPolling();
  else if (sid === 'whisper-model') fetchStats().catch(warn);
  else if (sid === 'history') loadHistory().catch(warn);
}

function restoreCollapsibleState() {
  chrome.storage.local.get(['expandedSections'], (result) => {
    const expanded = result.expandedSections || [];
    document.querySelectorAll('.yume-section.collapsible').forEach(section => {
      const id = section.getAttribute('data-section');
      if (id && expanded.includes(id)) {
        section.classList.remove('collapsed');
        const arrow = section.querySelector('.toggle-arrow');
        if (arrow) arrow.textContent = '▼';
        loadSectionData(id);
      }
    });
  });
}

function setupCollapsibleSections() {
  document.querySelectorAll('.section-toggle').forEach(label => {
    label.addEventListener('click', () => {
      const section = label.closest('.yume-section');
      section.classList.toggle('collapsed');
      const arrow = label.querySelector('.toggle-arrow');
      if (arrow) arrow.textContent = section.classList.contains('collapsed') ? '▶' : '▼';
      const expanded = [...document.querySelectorAll('.yume-section.collapsible:not(.collapsed)')]
        .map(s => s.getAttribute('data-section')).filter(Boolean);
      chrome.storage.local.set({ expandedSections: expanded });
      const sid = section.getAttribute('data-section') || '';
      if (!section.classList.contains('collapsed')) loadSectionData(sid);
      else if (sid === 'stats') stopStatsPolling();
    });
  });
}

// ============================================================================
// FONTS
// ============================================================================

function detectAvailableFonts(fontList) {
  const ctx = document.createElement('canvas').getContext('2d');
  if (!ctx) return fontList;
  const testStr = 'abcdefghij漢字テスト가나다라';
  ctx.font = '72px monospace';
  const baseWidth = ctx.measureText(testStr).width;
  return fontList.filter((font) => {
    ctx.font = `72px "${font}", monospace`;
    return ctx.measureText(testStr).width !== baseWidth;
  });
}

function populateFontDropdowns() {
  const src = document.getElementById('sourceLanguage')?.value || 'ja';
  const langFonts = CJK_FONTS[src] || CJK_FONTS.ja;
  const available = detectAvailableFonts([...new Set([...langFonts, ...Object.values(CJK_FONTS).flat(), ...GENERIC_FONTS])]);
  const addGroup = (select, label, fonts) => {
    if (!fonts.length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    for (const f of fonts) {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = f;
      group.appendChild(opt);
    }
    select.appendChild(group);
  };
  for (const id of ['originalFont', 'romajiFont', 'translationFont']) {
    const select = document.getElementById(id);
    if (!select) continue;
    const prev = select.value;
    select.innerHTML = '';
    const def = document.createElement('option');
    def.value = ''; def.textContent = 'System Default';
    select.appendChild(def);
    for (const f of BUNDLED_FONTS) {
      const opt = document.createElement('option');
      opt.value = f.name; opt.textContent = `${f.name} (bundled)`;
      select.appendChild(opt);
    }
    addGroup(select, `${src.toUpperCase()} Fonts`, available.filter(f => langFonts.includes(f)));
    addGroup(select, 'Other Fonts', available.filter(f => !langFonts.includes(f)));
    if (prev) select.value = prev;
  }
}

// ============================================================================
// TOGGLE SUBTITLES
// ============================================================================

function _isScriptablePage(url) {
  return /^https?:/.test(url || '') && !(url || '').startsWith('https://chromewebstore.google.com');
}

async function toggleSubtitles() {
  const button = document.getElementById('toggleSubtitles');
  const statusMsg = document.getElementById('statusMessage');
  button.disabled = true;
  flash(statusMsg, 'Working...', '', 0);
  try {
    const tab = await activeTab();
    if (!tab) throw new Error('No active tab found');
    if (!_isScriptablePage(tab.url)) {
      throw new Error("Yume can't run on this page — open a video page (like YouTube) and try again.");
    }
    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { action: 'TOGGLE_SUBTITLES' });
    } catch (e) {
      if (/Receiving end does not exist|Could not establish connection/.test(e.message)) {
        throw new Error('Yume is not loaded on this tab yet — reload the page (F5), then try again.', { cause: e });
      }
      throw e;
    }
    if (!response?.success) throw new Error(response?.error || 'Toggle failed');
    updateToggleButton(response.active);
    flash(statusMsg, response.active ? 'Starting — progress shows in the subtitle window' : 'Subtitles disabled', response.active ? '' : 'success', 3000);
  } catch (error) {
    flash(statusMsg, error.message, 'error', 0);
  } finally { button.disabled = false; }
}

function updateToggleButton(isActive) {
  const button = document.getElementById('toggleSubtitles');
  button.classList.toggle('active', isActive);
  button.setAttribute('aria-checked', String(isActive));
  document.getElementById('toggleText').textContent = isActive ? 'Disable' : 'Enable';
}

async function checkSubtitleStatus() {
  const statusMsg = document.getElementById('statusMessage');
  const tab = await activeTab();
  if (!tab) return;
  if (!_isScriptablePage(tab.url)) {
    flash(statusMsg, "Yume can't run on this page — open a video page (like YouTube).", '', 0);
    return;
  }
  const response = await chrome.tabs.sendMessage(tab.id, { action: 'GET_STATUS' });
  if (response?.success) {
    updateToggleButton(response.active);
    if (!response.active && response.hasVideo === false) flash(statusMsg, 'No video detected on this page yet.', '', 0);
  }
}

// ============================================================================
// SERVER STATUS
// ============================================================================

async function checkServers() {
  const whisperEl = document.getElementById('whisperStatus');
  const translationEl = document.getElementById('translationStatus');
  const whisperInfo = document.getElementById('whisperInfo');
  const translationInfo = document.getElementById('translationInfo');
  if (!whisperEl.className.includes('connected')) whisperEl.className = 'status-indicator checking';

  const h = await api({ path: '/health', timeout: 5000 });
  const st = h.data?.status;
  whisperEl.className = 'status-indicator ' + (st === 'ready' ? 'connected' : st === 'loading' ? 'checking' : 'disconnected');
  whisperInfo.textContent = st === 'error' ? 'Model failed to load — see whisper_server.log'
    : st === 'loading' ? 'Loading model...'
    : st === 'ready' ? `${h.data.device || '?'} | ${h.data.model || '?'}`
    : (h.data?.error || 'Not reachable');

  if (st) {
    // The server talks to the translation LLM; ask it whether that works.
    const t = await api({ path: '/translation/health', timeout: 8000 });
    translationEl.className = 'status-indicator ' + (t.data?.up ? 'connected' : 'disconnected');
    translationInfo.textContent = t.data?.address
      ? `${t.data.backend} @ ${t.data.address}${t.data.up ? '' : ' — not reachable'}` : 'unknown';
  } else {
    translationEl.className = 'status-indicator disconnected';
    translationInfo.textContent = 'via Whisper server (offline)';
  }
  // Start/Stop through the native helper (one-click start). Each request
  // launches a Python process: ask when the server is down (to offer Start
  // and show its progress), otherwise once per popup to know about Stop.
  if (!st || !_svcStatus) _svcStatus = await yume('status');
  const svc = _svcStatus;
  const startBtn = document.getElementById('serviceStart');
  const stopBtn = document.getElementById('serviceStop');
  const starting = svc.ok && svc.state === 'starting';
  startBtn.style.display = svc.ok && !st && !starting ? '' : 'none';
  stopBtn.style.display = svc.ok && svc.managed && svc.state !== 'stopped' ? '' : 'none';
  if (starting && !st) whisperInfo.textContent = svc.message || 'Starting...';

  const hint = document.getElementById('serverHint');
  const down = !st || !translationEl.classList.contains('connected');
  if (!st && starting) {
    hint.textContent = 'Yume is starting — the first start can take a few minutes.';
  } else if (!st && svc.ok) {
    hint.textContent = (svc.last_error ? `Last start failed: ${svc.last_error}. ` : '') +
      'Press Enable on a video (or Start Yume) — it starts by itself.';
  } else if (!st) {
    hint.textContent = 'Server offline? Start Yume first (double-click START_YUME), then click Refresh. ' +
      'Tip: "python pocket_yume.py autostart on" lets Enable start it for you.';
  } else {
    hint.textContent = 'Translation server not reachable — check it in the Yume CLI (Settings → Translation settings).';
  }
  hint.style.display = down ? '' : 'none';
}

let _svcStatus = null;

async function serviceCommand(cmd) {
  const btn = document.getElementById(cmd === 'start' ? 'serviceStart' : 'serviceStop');
  btn.disabled = true;
  try {
    const r = await yume(cmd);
    if (!r.ok) showToast(r.error || `Could not ${cmd} Yume`, 'error');
    else if (cmd === 'start') showToast('Starting Yume...');
    else showToast('Yume stopped');
  } finally {
    btn.disabled = false;
    _svcStatus = null;
    await checkServers();
  }
}

let _statusInterval = null;
document.addEventListener('visibilitychange', () => {
  clearInterval(_statusInterval);
  if (!document.hidden) _statusInterval = setInterval(checkServers, 12000);
});
_statusInterval = setInterval(checkServers, 12000);

// ============================================================================
// SETTINGS
// ============================================================================

const $ = (id) => document.getElementById(id);

async function loadSettings() {
  const { settings: raw = {} } = await chrome.storage.local.get(['settings']);
  const s = { ...DEFAULTS, ...raw };
  $('showOriginal').checked = s.showOriginal !== false;
  $('showEnglish').checked = s.showEnglish !== false;
  $('showRomaji').checked = s.showRomaji === true;
  $('showChunkCounter').checked = s.showChunkCounter !== false;
  $('showConfidence').checked = s.showConfidence === true;
  $('sourceLanguage').value = s.sourceLanguage;
  $('targetLanguage').value = s.targetLanguage;
  $('windowTitle').value = s.windowTitle;
  $('windowBgColor').value = s.windowBgColor;
  $('windowOpacity').value = s.windowOpacity;
  $('opacityVal').textContent = s.windowOpacity + '%';
  $('windowShadow').checked = s.windowShadow !== false;
  $('originalFont').value = s.originalFont;
  $('romajiFont').value = s.romajiFont;
  $('translationFont').value = s.translationFont;
  $('originalFontSize').value = s.originalFontSize;
  $('originalColor').value = s.originalColor;
  $('romajiFontSize').value = s.romajiFontSize;
  $('romajiColor').value = s.romajiColor;
  $('translationFontSize').value = s.translationFontSize;
  $('translationColor').value = s.translationColor;
  $('whisperPort').value = s.whisperPort;
  $('timingOffset').value = s.timingOffset;
  $('timingOffsetVal').value = (s.timingOffset / 10).toFixed(1);
  $('glassEnabled').checked = s.glassEnabled === true;
  $('glassBlur').value = s.glassBlur;
  $('glassBlurVal').textContent = s.glassBlur + 'px';
  $('glassRadius').value = s.glassRadius;
  $('glassRadiusVal').textContent = s.glassRadius + 'px';
  $('glassControls').style.display = s.glassEnabled ? 'block' : 'none';
  updateRomajiLabel();
  updateTranslationFontVisibility();
}

async function saveSettings(notify = true) {
  const { settings: existing = {} } = await chrome.storage.local.get(['settings']);
  const int = (id, d) => parseInt($(id).value, 10) || d;
  const radius = parseInt($('glassRadius').value, 10);
  const settings = {
    ...existing,
    showOriginal: $('showOriginal').checked,
    showEnglish: $('showEnglish').checked,
    showRomaji: $('showRomaji').checked,
    showChunkCounter: $('showChunkCounter').checked,
    showConfidence: $('showConfidence').checked,
    sourceLanguage: $('sourceLanguage').value,
    targetLanguage: $('targetLanguage').value,
    windowTitle: $('windowTitle').value || 'Yume',
    windowBgColor: $('windowBgColor').value,
    windowOpacity: int('windowOpacity', 88),
    windowShadow: $('windowShadow').checked,
    originalFont: $('originalFont').value,
    romajiFont: $('romajiFont').value,
    translationFont: $('translationFont').value,
    originalFontSize: int('originalFontSize', 19),
    originalColor: $('originalColor').value,
    romajiFontSize: int('romajiFontSize', 13),
    romajiColor: $('romajiColor').value,
    translationFontSize: int('translationFontSize', 14),
    translationColor: $('translationColor').value,
    timingOffset: parseInt($('timingOffset').value, 10) || 0,
    glassEnabled: $('glassEnabled').checked,
    glassBlur: int('glassBlur', 18),
    // 0 is a valid radius (square corners) — `|| default` would swallow it
    glassRadius: Number.isFinite(radius) ? radius : 20,
  };
  await chrome.storage.local.set({ settings });
  if (notify) showToast('Settings saved', 'success');
}

function updateRomajiLabel() {
  const label = ROMA_LABELS[$('sourceLanguage').value];
  const row = $('showRomaji')?.closest('.setting-card');
  if (row) row.style.display = label ? '' : 'none';
  $('romajiAppearanceSection').style.display = label ? '' : 'none';
  $('romajiHint').textContent = label?.hint || '';
  $('romajiAppearanceLabel').textContent = label?.name || 'Romanization';
}

function updateTranslationFontVisibility() {
  $('translationFontRow').style.display = LATIN_TARGETS.includes($('targetLanguage').value) ? '' : 'none';
}

function sendGlass() {
  sendToTab({
    action: 'UPDATE_GLASS', glassEnabled: $('glassEnabled').checked,
    glassBlur: parseInt($('glassBlur').value, 10), glassRadius: parseInt($('glassRadius').value, 10),
  }).catch(() => { /* no subtitle window on this tab */ });
}

async function savePorts() {
  const wPort = parseInt($('whisperPort').value, 10);
  const statusEl = $('portsStatus');
  if (!(wPort >= 1 && wPort <= 65535)) { flash(statusEl, 'Port must be 1-65535', 'error'); return; }
  const { settings = {} } = await chrome.storage.local.get(['settings']);
  await chrome.storage.local.set({ settings: { ...settings, whisperPort: wPort, whisperUrl: `http://localhost:${wPort}` } });
  flash(statusEl, 'Saved! Checking...', '', 0);
  await checkServers();
  const ok = $('whisperStatus').classList.contains('connected');
  flash(statusEl, ok ? '✓ Connected' : 'Saved, but the Whisper server is not reachable on that port', ok ? 'success' : 'error', 0);
}

async function setCustomStreamUrl() {
  const url = ($('customStreamUrl').value || '').trim();
  const statusEl = $('streamUrlStatus');
  if (!/^https?:\/\//.test(url)) { flash(statusEl, 'Enter an http(s) URL first', 'error', 3000); return; }
  await chrome.storage.local.set({ customStreamUrl: url });
  flash(statusEl, '✓ Stream URL set. Click Enable to start.', 'success', 0);
}

async function clearCustomStreamUrl() {
  $('customStreamUrl').value = '';
  await chrome.storage.local.remove('customStreamUrl');
  flash($('streamUrlStatus'), 'Cleared', '', 2000);
}

// ============================================================================
// TRANSLATION MODEL
// ============================================================================

async function loadModels() {
  const infoEl = $('modelInfo');
  infoEl.innerHTML = '<span class="model-label">Loading...</span>';
  const r = await api({ path: '/translation/models', timeout: 10000 });
  if (!r.ok) {
    infoEl.innerHTML = `<span class="model-label" style="color:var(--text-dim)">${_escapeHtml(r.data?.error || 'Server not running')}</span>`;
    return;
  }
  const d = r.data;
  let html = `<span class="model-label">Backend: <b>${_escapeHtml(d.backend || '?')}</b></span>`;
  if ((d.models || []).length) {
    html += '<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">Loaded:</div>';
    for (const m of d.models) html += `<div style="font-size:12px;color:var(--text-primary);padding:2px 0">• ${_escapeHtml(m.name)}</div>`;
  }
  if ((d.local_ggufs || []).length) {
    html += '<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">Local GGUFs:</div>';
    for (const g of d.local_ggufs) html += `<div style="font-size:12px;color:var(--text-primary);padding:2px 0">• ${_escapeHtml(g.name)} <span style="color:var(--text-dim)">(${_escapeHtml(g.size_mb)} MB)</span></div>`;
  }
  if (d.note) html += `<div style="margin-top:6px;font-size:10px;color:var(--border-gold);font-style:italic">${_escapeHtml(d.note)}</div>`;
  infoEl.innerHTML = html;
}

// ============================================================================
// DIAGNOSTICS
// ============================================================================

let lastDiag = null;

async function fetchDiagnostics() {
  const statusEl = $('diagStatus');
  const logEl = $('diagLog');
  let response;
  try { response = await sendToTab({ action: 'GET_DIAGNOSTICS' }); } catch (_e) {
    statusEl.textContent = 'Content script not loaded'; logEl.innerHTML = ''; return;
  }
  const d = response?.diagnostics;
  if (!d) { statusEl.textContent = 'Idle — enable subtitles on a video'; logEl.innerHTML = ''; return; }
  lastDiag = { ...d, tabUrl: (await activeTab())?.url, timestamp: new Date().toISOString() };
  const p = d.progress || {};
  statusEl.textContent = [
    d.status?.toUpperCase(), `regions ${p.regions_done || 0}/${p.regions_total || '?'}`,
    `lines ${p.lines || 0}`, p.translated !== undefined ? `translated ${p.translated}` : '', d.error,
  ].filter(Boolean).join(' | ');
  if (!d.events.length) { logEl.innerHTML = '<div style="color:var(--text-dim);font-style:italic">No entries yet</div>'; return; }
  logEl.innerHTML = d.events.map((e) => {
    const time = new Date(e.t * 1000).toLocaleTimeString('en-GB', { hour12: false });
    return `<div class="diag-entry"><span class="diag-time">${_escapeHtml(time)}</span>` +
      `<span class="diag-badge ${_escapeHtml(e.level)}">${_escapeHtml(e.level)}</span>` +
      `<span class="diag-detail">${_escapeHtml(e.msg)}</span></div>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;
}

async function downloadDiagnostics() {
  await fetchDiagnostics();
  if (!lastDiag) { showToast('No diagnostics', 'error'); return; }
  downloadText(JSON.stringify(lastDiag, null, 2), `yume-diag-${new Date().toISOString().replace(/[:.]/g, '-')}.json`, 'application/json');
}

// ============================================================================
// HALLUCINATION BLACKLIST (lives on the server)
// ============================================================================

let blacklistVisible = false;

// The blacklist is applied by the server; ask the page to fetch the change now
function refreshTab() {
  sendToTab({ action: 'REFRESH_SUBTITLES' }).catch(() => { /* no subtitles on this tab */ });
}

async function loadBlacklist() {
  const r = await api({ path: '/blacklist' });
  const list = r.ok ? (r.data.blacklist || []) : [];
  $('blacklistCount').textContent = r.ok ? `${list.length} item${list.length !== 1 ? 's' : ''}` : 'server offline';
  const container = $('blacklistContainer');
  if (!list.length) { container.innerHTML = '<div class="blacklist-empty">No items yet</div>'; return; }
  container.innerHTML = list.map((item, i) => `
    <div class="blacklist-item">
      <span class="blacklist-text">${_escapeHtml(item)}</span>
      <button class="blacklist-remove" data-index="${i}" title="Remove">×</button>
    </div>`).join('');
  container.querySelectorAll('.blacklist-remove').forEach(btn => btn.addEventListener('click', async () => {
    await api({ method: 'POST', path: '/blacklist/remove', body: { text: list[+btn.dataset.index] } });
    refreshTab();
    loadBlacklist();
  }));
}

async function reportHallucination() {
  const previewEl = $('hallucinationPreview');
  const statusEl = $('blacklistStatus');
  let response;
  try { response = await sendToTab({ action: 'GET_CURRENT_SUBTITLE' }); } catch (_e) {
    flash(statusEl, 'Content script not loaded', 'error', 3000); return;
  }
  const text = (response?.subtitle?.original || '').trim();
  if (!text) { flash(statusEl, 'No subtitle currently displayed', 'error', 3000); return; }
  previewEl.style.display = 'block';
  previewEl.innerHTML = `
    <div class="preview-label">Captured:</div>
    <div class="preview-text">${_escapeHtml(text)}</div>
    <div style="display:flex;gap:6px;margin-top:6px">
      <button class="yume-button primary preview-confirm" style="flex:1;padding:4px 8px;font-size:11px">Add to Blacklist</button>
      <button class="yume-button secondary preview-cancel" style="flex:1;padding:4px 8px;font-size:11px">Cancel</button>
    </div>`;
  previewEl.querySelector('.preview-confirm').addEventListener('click', async () => {
    previewEl.style.display = 'none';
    const r = await api({ method: 'POST', path: '/blacklist/add', body: { text } });
    flash(statusEl, r.ok ? `✓ Blocked (${r.data.count} items) — applied everywhere` : (r.data?.error || 'Failed'), r.ok ? 'success' : 'error');
    refreshTab();
    loadBlacklist();
  });
  previewEl.querySelector('.preview-cancel').addEventListener('click', () => { previewEl.style.display = 'none'; });
}

async function clearBlacklist() {
  const r = await api({ method: 'POST', path: '/blacklist/update', body: { blacklist: [] } });
  showToast(r.ok ? 'Blacklist cleared' : (r.data?.error || 'Failed'), r.ok ? 'success' : 'error');
  refreshTab();
  loadBlacklist();
}

// ============================================================================
// STATS + WHISPER MODEL
// ============================================================================

let statsInterval = null;

async function fetchStats() {
  const el = $('statsContent');
  const r = await api({ path: '/stats', timeout: 8000 });
  if (!r.ok) {
    el.textContent = 'Stats unavailable: ' + (r.data?.error || `HTTP ${r.status}`);
    const { settings = {} } = await chrome.storage.local.get(['settings']);
    if (settings.whisperModel) $('currentWhisperModel').textContent = `${settings.whisperModel.split(/[/\\]/).pop()} (offline)`;
    return;
  }
  const s = r.data;
  const n = (v) => _escapeHtml(v ?? '?');  // numbers too: nothing reaches innerHTML unescaped
  let html = '';
  if (s.gpu) {
    const pct = Math.round((s.gpu.vram_used_mb / s.gpu.vram_total_mb) * 100) || 0;
    const color = pct > 90 ? 'var(--accent-red)' : pct > 70 ? 'var(--border-gold)' : 'var(--accent-green)';
    html += `<div style="margin-bottom:8px"><b>${_escapeHtml(s.gpu.gpu_name)}</b><br>`;
    html += '<div style="display:flex;align-items:center;gap:8px;margin:4px 0">';
    html += '<div style="flex:1;height:8px;background:rgba(255,255,255,0.1);border-radius:4px;overflow:hidden">';
    html += `<div style="width:${pct}%;height:100%;background:${color};border-radius:4px"></div></div>`;
    html += `<span style="font-size:11px;color:${color}">${n(s.gpu.vram_used_mb)}/${n(s.gpu.vram_total_mb)} MB</span></div>`;
    html += `GPU: ${n(s.gpu.gpu_util_pct)}% &nbsp;|&nbsp; ${n(s.gpu.gpu_temp_c)}°C</div>`;
  }
  html += `<b>Whisper:</b> ${_escapeHtml(s.model)} (${_escapeHtml(s.device)}/${_escapeHtml(s.compute_type)})<br>`;
  html += `Sections transcribed: <b>${n(s.regions_transcribed)}</b> &nbsp;|&nbsp; Lines: <b>${n(s.segments_produced)}</b><br>`;
  html += `Avg Whisper time: <b>${n(s.avg_whisper_time)}s</b> &nbsp;|&nbsp; Last: ${n(s.last_region_whisper_time)}s<br>`;
  html += `Audio processed: <b>${n(Math.round(s.total_audio_seconds))}s</b> &nbsp;|&nbsp; Lines translated: ${n(s.lines_translated)}<br>`;
  html += `Hallucinations blocked: <b>${n(s.hallucinations_filtered)}</b> &nbsp;|&nbsp; Blacklist: ${n(s.blacklist_size)}<br>`;
  html += `Uptime: ${_escapeHtml(s.uptime_human)} &nbsp;|&nbsp; Saved videos: ${n(s.library_size)} &nbsp;|&nbsp; Active jobs: ${n(s.active)}`;
  el.innerHTML = html;

  const isCustomPath = /[/\\]/.test(s.model || '');
  $('currentWhisperModel').textContent = s.model_display_name || (isCustomPath ? s.model.split(/[/\\]/).pop() : s.model);
  const sel = $('whisperModelSelect');
  if (isCustomPath && ![...sel.options].some(o => o.value === s.model)) {
    const opt = document.createElement('option');
    opt.value = s.model;
    opt.textContent = `${s.model_display_name || s.model.split(/[/\\]/).pop()} (custom)`;
    sel.insertBefore(opt, sel.firstChild);
  }
  sel.value = s.model;
  const { settings = {} } = await chrome.storage.local.get(['settings']);
  if (settings.whisperModel !== s.model) await chrome.storage.local.set({ settings: { ...settings, whisperModel: s.model } });
}

function startStatsPolling() {
  if (statsInterval) return;
  fetchStats().catch(e => console.warn('[Yume]', e.message));
  statsInterval = setInterval(() => fetchStats().catch(e => console.warn('[Yume]', e.message)), 5000);
}
function stopStatsPolling() { clearInterval(statsInterval); statsInterval = null; }

async function switchWhisperModel() {
  const model = $('whisperModelSelect').value;
  const statusEl = $('modelSwitchStatus');
  flash(statusEl, `Loading ${model}... (first use downloads it)`, '', 0);
  const r = await api({ method: 'POST', path: '/model/switch', body: { model }, timeout: 600000 });
  if (r.ok) {
    flash(statusEl, r.data.status === 'already_loaded' ? `Already using ${model}` : `✓ Switched to ${r.data.model}`, 'success', 5000);
    $('currentWhisperModel').textContent = r.data.model;
  } else {
    flash(statusEl, r.data?.error || 'Switch failed', 'error', 6000);
  }
}

// ============================================================================
// EXPORT + HISTORY (server library)
// ============================================================================

async function exportSubtitles(format) {
  const statusEl = $('exportStatus');
  flash(statusEl, 'Exporting...', '', 0);
  let r;
  try { r = await sendToTab({ action: format === 'vtt' ? 'EXPORT_VTT' : 'EXPORT_SRT' }); } catch (e) {
    flash(statusEl, e.message, 'error'); return;
  }
  if (!r?.success) { flash(statusEl, r?.error || 'Export failed', 'error'); return; }
  if (!r.count) { flash(statusEl, 'No subtitles to export yet', 'error'); return; }
  downloadText(r.content, `yume-subtitles-${new Date().toISOString().slice(0, 16).replace(/[T:]/g, '-')}.${format}`,
    format === 'vtt' ? 'text/vtt;charset=utf-8' : 'application/x-subrip;charset=utf-8');
  const p = r.progress || {};
  const pct = p.regions_total ? Math.round((p.regions_done / p.regions_total) * 100) : 0;
  flash(statusEl, `✓ Exported ${r.count} subtitles (${pct}% of the video processed)`, 'success', 5000);
}

async function loadHistory() {
  const listEl = $('historyList');
  const r = await api({ path: '/library' });
  if (!r.ok) { listEl.innerHTML = `<div class="blacklist-empty">${_escapeHtml(r.data?.error || 'Server offline')}</div>`; return; }
  const entries = r.data.videos || [];
  if (!entries.length) {
    listEl.innerHTML = '<div class="blacklist-empty">No saved videos yet — transcribed videos appear here.</div>';
    return;
  }
  const { settings = {} } = await chrome.storage.local.get(['settings']);
  listEl.innerHTML = entries.map((e, i) => {
    const date = e.last_used ? new Date(e.last_used * 1000).toLocaleDateString() : '';
    const label = (e.title || e.video_key).slice(0, 60);
    const pct = e.regions_total ? Math.round((e.regions_done / e.regions_total) * 100) : 0;
    return `
      <div class="blacklist-item">
        <span class="blacklist-text" title="${_escapeHtml(e.url || '')}">${_escapeHtml(label)}
          <span style="color:var(--text-dim);font-size:10px;display:block">${_escapeHtml(date)} · ${_escapeHtml(e.language)} · ${_escapeHtml(e.model)} · ${pct}%</span>
        </span>
        <button class="yume-button secondary hist-srt" data-i="${i}" style="padding:2px 8px;font-size:10px;margin-top:0">SRT</button>
        <button class="blacklist-remove hist-del" data-i="${i}" title="Remove">×</button>
      </div>`;
  }).join('');
  listEl.querySelectorAll('.hist-srt').forEach(btn => btn.addEventListener('click', async () => {
    const e = entries[+btn.dataset.i];
    const target = settings.showEnglish === false ? '' : (settings.targetLanguage || 'English');
    const x = await api({ path: '/library/export', query: { video_key: e.video_key, language: e.language, model: e.model, target, format: 'srt' } });
    if (!x.ok || !x.data.count) { showToast(x.data?.error || 'No subtitles in this entry', 'error'); return; }
    downloadText(x.data.content, `yume-${(e.title || 'subtitles').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60)}.srt`, 'application/x-subrip;charset=utf-8');
  }));
  listEl.querySelectorAll('.hist-del').forEach(btn => btn.addEventListener('click', async () => {
    await api({ method: 'POST', path: '/library/delete', body: { video_key: entries[+btn.dataset.i].video_key } });
    loadHistory();
  }));
}

let _clearArmed = null;
async function clearCache() {
  const btn = $('clearHistory');
  if (!_clearArmed) {
    // confirm() is not available in Firefox popups — ask with a second click
    const label = btn.textContent;
    btn.textContent = 'Click again to delete everything';
    _clearArmed = setTimeout(() => { btn.textContent = label; _clearArmed = null; }, 4000);
    btn.dataset.label = label;
    return;
  }
  clearTimeout(_clearArmed);
  _clearArmed = null;
  btn.textContent = btn.dataset.label || btn.textContent;
  const r = await api({ method: 'POST', path: '/cache/clear' });
  showToast(r.ok ? 'Cache cleared' : (r.data?.error || 'Failed'), r.ok ? 'success' : 'error');
  loadHistory();
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

function setupEventListeners() {
  const on = (id, ev, fn) => $(id)?.addEventListener(ev, (e) => {
    const r = fn(e);
    if (r?.catch) r.catch(err => console.error('[Yume]', err));
  });

  on('toggleSubtitles', 'click', toggleSubtitles);
  on('refreshStatus', 'click', checkServers);
  on('serviceStart', 'click', () => serviceCommand('start'));
  on('serviceStop', 'click', () => serviceCommand('stop'));
  on('refreshDiag', 'click', fetchDiagnostics);
  on('downloadDiag', 'click', downloadDiagnostics);
  on('refreshModels', 'click', loadModels);
  on('savePorts', 'click', savePorts);
  on('setStreamUrl', 'click', setCustomStreamUrl);
  on('clearStreamUrl', 'click', clearCustomStreamUrl);

  for (const id of ['showOriginal', 'showEnglish', 'showRomaji', 'showChunkCounter', 'showConfidence', 'windowShadow',
    'originalFont', 'romajiFont', 'translationFont',
    'windowTitle', 'originalFontSize', 'romajiFontSize', 'translationFontSize']) {
    on(id, 'change', () => saveSettings(true));
  }
  on('sourceLanguage', 'change', () => { updateRomajiLabel(); populateFontDropdowns(); return saveSettings(true); });
  on('targetLanguage', 'change', () => { updateTranslationFontVisibility(); return saveSettings(true); });

  on('windowOpacity', 'input', () => { $('opacityVal').textContent = $('windowOpacity').value + '%'; return saveSettings(false); });
  for (const id of ['windowBgColor', 'originalColor', 'romajiColor', 'translationColor']) on(id, 'input', () => saveSettings(false));

  on('timingOffset', 'input', () => { $('timingOffsetVal').value = ($('timingOffset').value / 10).toFixed(1); return saveSettings(false); });
  on('timingOffsetVal', 'input', () => {
    const ticks = Math.round(Math.max(-30, Math.min(30, (parseFloat($('timingOffsetVal').value) || 0) * 10)));
    $('timingOffset').value = ticks;
    return saveSettings(false);
  });

  on('glassEnabled', 'change', () => { $('glassControls').style.display = $('glassEnabled').checked ? 'block' : 'none'; sendGlass(); return saveSettings(false); });
  on('glassBlur', 'input', () => { $('glassBlurVal').textContent = $('glassBlur').value + 'px'; sendGlass(); });
  on('glassBlur', 'change', () => saveSettings(false));
  on('glassRadius', 'input', () => { $('glassRadiusVal').textContent = $('glassRadius').value + 'px'; sendGlass(); });
  on('glassRadius', 'change', () => saveSettings(false));

  on('reportHallucination', 'click', reportHallucination);
  on('clearBlacklist', 'click', clearBlacklist);
  on('toggleBlacklistView', 'click', () => {
    blacklistVisible = !blacklistVisible;
    $('blacklistContainer').style.display = blacklistVisible ? '' : 'none';
    $('toggleBlacklistView').textContent = blacklistVisible ? '▲' : '▼';
    if (blacklistVisible) return loadBlacklist();
  });

  on('refreshStats', 'click', fetchStats);
  on('switchWhisperModel', 'click', switchWhisperModel);
  on('exportSrt', 'click', () => exportSubtitles('srt'));
  on('exportVtt', 'click', () => exportSubtitles('vtt'));
  on('refreshHistory', 'click', loadHistory);
  on('clearHistory', 'click', clearCache);

  chrome.storage.local.get(['customStreamUrl'], (r) => { if (r.customStreamUrl) $('customStreamUrl').value = r.customStreamUrl; });
}
