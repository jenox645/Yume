// Yume - Popup Script v5.4.2
console.log('[Yume Popup] Script loaded');

// ============================================================================
// API TOKEN HELPER — retrieves token stored by background.js
// ============================================================================

async function _getApiToken() {
  try {
    const data = await chrome.storage.session.get(['apiToken']);
    return data.apiToken || null;
  } catch (e) {
    return null;
  }
}

// ============================================================================
// ROMA LABELS — dynamic romanization naming per source language
// ============================================================================

const ROMA_LABELS = {
  'ja': { name: 'Romaji', hint: '(Romaji)' },
  'zh': { name: 'Pinyin', hint: '(Pinyin)' },
  'ko': { name: 'Romanization', hint: '(Romanization)' },
  'ru': { name: 'Transliteration', hint: '(Latin)' },
  'ar': { name: 'Transliteration', hint: '(Latin)' },
};

// Latin-alphabet target languages (show translation font for these)
const LATIN_TARGETS = [
  'English', 'French', 'Spanish', 'German', 'Portuguese',
  'Italian', 'Indonesian', 'Vietnamese', 'Turkish', 'Dutch', 'Polish'
];

// Bundled fonts — add .ttf/.otf files to extension/fonts/ and list them here
const BUNDLED_FONTS = [];

// System fonts — organized by language, detected at runtime
const CJK_FONTS = {
  ja: ['Noto Sans JP', 'Noto Serif JP', 'M PLUS Rounded 1c', 'Kosugi Maru',
       'Zen Maru Gothic', 'Yu Gothic', 'Meiryo', 'MS Gothic', 'Hiragino Sans'],
  zh: ['Noto Sans SC', 'Noto Sans TC', 'Microsoft YaHei', 'PingFang SC',
       'SimHei', 'STHeiti', 'Source Han Sans CN'],
  ko: ['Noto Sans KR', 'Malgun Gothic', 'Apple SD Gothic Neo', 'NanumGothic'],
  ar: ['Noto Naskh Arabic', 'Amiri', 'Scheherazade New', 'Tahoma', 'Arial'],
};
const GENERIC_FONTS = [
  'Arial', 'Georgia', 'Verdana', 'Segoe UI', 'Consolas',
  'Times New Roman', 'Courier New', 'Helvetica',
];

// Detect which fonts are actually installed on this system
function detectAvailableFonts(fontList) {
  const available = [];
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) return fontList; // can't detect, return all
  const testStr = 'abcdefghij漢字テスト가나다라';
  ctx.font = '72px monospace';
  const baseWidth = ctx.measureText(testStr).width;
  for (const font of fontList) {
    ctx.font = `72px "${font}", monospace`;
    if (ctx.measureText(testStr).width !== baseWidth) {
      available.push(font);
    }
  }
  return available;
}

// ============================================================================
// STARTUP
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  restoreCollapsibleState();
  setupCollapsibleSections();
  populateFontDropdowns();
  setupEventListeners();
  loadSettings().catch(e => console.error('[Yume] loadSettings failed:', e));
  checkServers().catch(e => console.error('[Yume] checkServers failed:', e));
  checkSubtitleStatus().catch(e => console.error('[Yume] checkSubtitleStatus failed:', e));
});

// ============================================================================
// COLLAPSIBLE SECTIONS
// ============================================================================

function restoreCollapsibleState() {
  chrome.storage.local.get(['expandedSections'], (result) => {
    const expanded = result.expandedSections || [];
    document.querySelectorAll('.yume-section.collapsible').forEach(section => {
      const id = section.getAttribute('data-section');
      if (id && expanded.includes(id)) {
        section.classList.remove('collapsed');
        const arrow = section.querySelector('.toggle-arrow');
        if (arrow) arrow.textContent = '\u25BC';
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
      if (arrow) arrow.textContent = section.classList.contains('collapsed') ? '\u25B6' : '\u25BC';

      const expanded = [];
      document.querySelectorAll('.yume-section.collapsible:not(.collapsed)').forEach(s => {
        const id = s.getAttribute('data-section');
        if (id) expanded.push(id);
      });
      chrome.storage.local.set({ expandedSections: expanded });

      const sid = section.getAttribute('data-section') || '';
      if (!section.classList.contains('collapsed')) {
        const text = label.textContent.trim();
        if (sid === 'translation-model' || text.includes('Translation Model')) loadModels().catch(e => console.warn('[Yume]', e.message));
        if (sid === 'diagnostics' || text.includes('Diagnostics')) fetchDiagnostics().catch(e => console.warn('[Yume]', e.message));
        if (sid === 'hallucination' || text.includes('Hallucination')) loadBlacklist();
        if (sid === 'server-stats' || text.includes('Server Stats')) startStatsPolling();
        if (sid === 'whisper-model' || text.includes('Whisper Model')) fetchStats().catch(e => console.warn('[Yume]', e.message));
      } else {
        const text = label.textContent.trim();
        if (sid === 'server-stats' || text.includes('Server Stats')) stopStatsPolling();
      }
    });
  });
}

// ============================================================================
// FONT DROPDOWNS
// ============================================================================

function populateFontDropdowns() {
  const src = document.getElementById('sourceLanguage')?.value || 'ja';
  const langFonts = CJK_FONTS[src] || CJK_FONTS['ja'];
  const allCandidates = [...new Set([...langFonts, ...Object.values(CJK_FONTS).flat(), ...GENERIC_FONTS])];
  const available = detectAvailableFonts(allCandidates);

  const dropdowns = ['originalFont', 'romajiFont', 'translationFont'];
  dropdowns.forEach(id => {
    const select = document.getElementById(id);
    if (!select) return;
    const prev = select.value; // preserve selection
    select.innerHTML = '';

    // System default
    const defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = 'System Default';
    select.appendChild(defOpt);

    // Bundled fonts (from extension/fonts/)
    BUNDLED_FONTS.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.name;
      opt.textContent = f.name + ' (bundled)';
      select.appendChild(opt);
    });

    // Language-specific fonts (detected as available)
    const langAvail = available.filter(f => langFonts.includes(f));
    if (langAvail.length > 0) {
      const group = document.createElement('optgroup');
      group.label = `${src.toUpperCase()} Fonts`;
      langAvail.forEach(f => {
        const opt = document.createElement('option');
        opt.value = f; opt.textContent = f;
        group.appendChild(opt);
      });
      select.appendChild(group);
    }

    // Other detected fonts
    const otherAvail = available.filter(f => !langFonts.includes(f));
    if (otherAvail.length > 0) {
      const group = document.createElement('optgroup');
      group.label = 'Other Fonts';
      otherAvail.forEach(f => {
        const opt = document.createElement('option');
        opt.value = f; opt.textContent = f;
        group.appendChild(opt);
      });
      select.appendChild(group);
    }

    // Restore previous selection if still available
    if (prev) select.value = prev;
  });

  // Also scan storage for user-added font names
  chrome.storage.local.get(['customFonts'], (result) => {
    const custom = result.customFonts || [];
    if (custom.length === 0) return;
    dropdowns.forEach(id => {
      const select = document.getElementById(id);
      if (!select) return;
      custom.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name + ' (custom)';
        select.appendChild(opt);
      });
    });
  });
}

// ============================================================================
// TOGGLE SUBTITLES
// ============================================================================

async function toggleSubtitles() {
  const button = document.getElementById('toggleSubtitles');
  const statusMsg = document.getElementById('statusMessage');
  button.disabled = true;
  statusMsg.textContent = 'Working...';
  statusMsg.className = 'status-message';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error('No active tab found');

    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { action: 'TOGGLE_SUBTITLES' });
    } catch (msgErr) {
      if (msgErr.message.includes('Receiving end does not exist') ||
          msgErr.message.includes('Could not establish connection')) {
        throw new Error('Content script not loaded. Hard reload (Ctrl+Shift+R) then try again.');
      }
      throw msgErr;
    }

    if (response?.success) {
      updateToggleButton(response.active);
      statusMsg.textContent = response.active ? 'Fetching subtitles...' : 'Subtitles disabled';
      statusMsg.className = 'status-message ' + (response.active ? '' : 'success');
      setTimeout(() => { statusMsg.textContent = ''; }, 3000);
    } else {
      throw new Error(response?.error || 'Toggle failed');
    }
  } catch (error) {
    statusMsg.textContent = error.message;
    statusMsg.className = 'status-message error';
  } finally { button.disabled = false; }
}

function updateToggleButton(isActive) {
  const button = document.getElementById('toggleSubtitles');
  const text = document.getElementById('toggleText');
  if (isActive) { button.classList.add('active'); text.textContent = 'Disable'; }
  else { button.classList.remove('active'); text.textContent = 'Enable'; }
}

async function checkSubtitleStatus() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return;
    const response = await chrome.tabs.sendMessage(tab.id, { action: 'GET_STATUS' });
    if (response?.success) updateToggleButton(response.active);
  } catch (e) { console.warn('[Yume]', e.message); }
}

// ============================================================================
// SERVER STATUS
// ============================================================================

async function checkServers() {
  const whisperEl = document.getElementById('whisperStatus');
  const translationEl = document.getElementById('translationStatus');
  whisperEl.className = 'status-indicator checking';
  translationEl.className = 'status-indicator checking';

  const sendBg = (url, isWhisper = false) => new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'CHECK_SERVER', url, isWhisper }, (r) => {
      resolve(chrome.runtime.lastError ? { healthy: false } : (r || { healthy: false }));
    });
  });

  const { settings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
  const wPort = settings?.whisperPort || 5001;
  const whisperUrl = settings?.whisperUrl || `http://localhost:${wPort}`;
  const tPort = settings?.translationPort || 5000;
  const tUrl = settings?.translationUrl || `http://localhost:${tPort}`;

  try {
    const w = await sendBg(`${whisperUrl}/health`, true);  // isWhisper = true
    whisperEl.className = 'status-indicator ' + (w.healthy ? 'connected' : 'disconnected');
    const infoEl = document.getElementById('whisperInfo');
    if (infoEl && w.data) infoEl.textContent = `${w.data.device || '?'} | ${w.data.model || '?'}`;
    else if (infoEl) infoEl.textContent = `localhost:${wPort}`;
  } catch (e) { whisperEl.className = 'status-indicator disconnected'; }

  try {
    // Try multiple health endpoints — different backends use different paths
    let t = await sendBg(`${tUrl}/health`);
    if (!t.healthy) t = await sendBg(`${tUrl}/v1/models`);
    if (!t.healthy) t = await sendBg(`${tUrl}/api/tags`);  // Ollama
    if (!t.healthy && tPort !== 11434) t = await sendBg('http://localhost:11434/api/tags');
    // Also try just connecting (some servers don't have /health)
    if (!t.healthy) t = await sendBg(`${tUrl}/`);
    translationEl.className = 'status-indicator ' + (t.healthy ? 'connected' : 'disconnected');
    const portEl = document.getElementById('translationInfo');
    if (portEl) portEl.textContent = `localhost:${tPort}`;
  } catch (e) { translationEl.className = 'status-indicator disconnected'; }
}

// Auto-refresh server status while popup is open (fixes stale red indicators)
let _statusInterval = null;
function startStatusAutoRefresh() {
  if (_statusInterval) clearInterval(_statusInterval);
  _statusInterval = setInterval(checkServers, 12000);  // every 12 seconds
}
function stopStatusAutoRefresh() {
  if (_statusInterval) { clearInterval(_statusInterval); _statusInterval = null; }
}
// Start on popup open, stop on close
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopStatusAutoRefresh();
  else startStatusAutoRefresh();
});
startStatusAutoRefresh();

// ============================================================================
// SETTINGS
// ============================================================================

function getDefaultSettings() {
  return {
    whisperUrl: 'http://localhost:5001', whisperPort: 5001,
    translationUrl: 'http://localhost:5000', translationPort: 5000,
    chunkDuration: 30,
    showOriginal: true, showEnglish: true, showRomaji: false,
    showChunkCounter: true, showConfidence: false,
    sourceLanguage: 'ja', targetLanguage: 'English',
    windowTitle: 'Yume', windowBgColor: '#08080c', windowOpacity: 88,
    windowShadow: true,
    originalFont: '', romajiFont: '', translationFont: '',
    originalFontSize: 19, originalColor: '#ffffff',
    romajiFontSize: 13, romajiColor: '#ffc88c',
    translationFontSize: 14, translationColor: '#b4beff',
    timingOffset: 0,
  };
}

async function loadSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['settings'], (result) => {
      const raw = result.settings || {};

      // Migrate showJapanese → showOriginal (renamed in v4.6.0)
      if ('showJapanese' in raw && !('showOriginal' in raw)) {
        raw.showOriginal = raw.showJapanese;
        delete raw.showJapanese;
        chrome.storage.local.set({ settings: raw });
        console.log('[Yume] Migrated showJapanese → showOriginal');
      }

      const s = { ...getDefaultSettings(), ...raw };

      document.getElementById('showOriginal').checked = s.showOriginal !== false;
      document.getElementById('showEnglish').checked = s.showEnglish !== false;
      document.getElementById('showRomaji').checked = s.showRomaji === true;
      document.getElementById('showChunkCounter').checked = s.showChunkCounter !== false;
      if (document.getElementById('showConfidence')) {
        document.getElementById('showConfidence').checked = s.showConfidence === true;
      }
      document.getElementById('chunkDuration').value = s.chunkDuration || 30;
      document.getElementById('sourceLanguage').value = s.sourceLanguage || 'ja';
      document.getElementById('targetLanguage').value = s.targetLanguage || 'English';

      document.getElementById('windowTitle').value = s.windowTitle || 'Yume';
      document.getElementById('windowBgColor').value = s.windowBgColor || '#08080c';
      document.getElementById('windowOpacity').value = s.windowOpacity ?? 88;
      document.getElementById('opacityVal').textContent = (s.windowOpacity ?? 88) + '%';
      document.getElementById('windowShadow').checked = s.windowShadow !== false;

      // Per-line fonts
      document.getElementById('originalFont').value = s.originalFont || '';
      document.getElementById('romajiFont').value = s.romajiFont || '';
      document.getElementById('translationFont').value = s.translationFont || '';

      document.getElementById('originalFontSize').value = s.originalFontSize || 19;
      document.getElementById('originalColor').value = s.originalColor || '#ffffff';
      document.getElementById('romajiFontSize').value = s.romajiFontSize || 13;
      document.getElementById('romajiColor').value = s.romajiColor || '#ffc88c';
      document.getElementById('translationFontSize').value = s.translationFontSize || 14;
      document.getElementById('translationColor').value = s.translationColor || '#b4beff';

      // Ports
      document.getElementById('whisperPort').value = s.whisperPort || 5001;
      document.getElementById('translationPort').value = s.translationPort || 5000;

      // Timing offset
      const offsetTicks = s.timingOffset || 0;
      document.getElementById('timingOffset').value = offsetTicks;
      document.getElementById('timingOffsetVal').textContent = (offsetTicks / 10).toFixed(1) + 's';

      // Glass effect
      const glassEnabled = s.glassEnabled === true;
      const glassBlur = s.glassBlur ?? 18;
      const glassRadius = s.glassRadius ?? 16;
      document.getElementById('glassEnabled').checked = glassEnabled;
      document.getElementById('glassBlur').value = glassBlur;
      document.getElementById('glassBlurVal').textContent = glassBlur + 'px';
      document.getElementById('glassRadius').value = glassRadius;
      document.getElementById('glassRadiusVal').textContent = glassRadius + 'px';
      document.getElementById('glassControls').style.display = glassEnabled ? 'block' : 'none';
      // Glass is applied to the subtitle window via settings storage, not a direct message on load

      updateRomajiLabel();
      updateTranslationFontVisibility();
      resolve();
    });
  });
}

function saveSettings(showNotification = true) {
  chrome.storage.local.get(['settings'], (result) => {
    const existing = result.settings || getDefaultSettings();
    const settings = {
      ...existing,
      showOriginal:        document.getElementById('showOriginal').checked,
      showEnglish:         document.getElementById('showEnglish').checked,
      showRomaji:          document.getElementById('showRomaji').checked,
      showChunkCounter:    document.getElementById('showChunkCounter').checked,
      showConfidence:      document.getElementById('showConfidence')?.checked || false,
      chunkDuration:       parseInt(document.getElementById('chunkDuration').value) || 30,
      sourceLanguage:      document.getElementById('sourceLanguage').value,
      targetLanguage:      document.getElementById('targetLanguage').value,
      windowTitle:         document.getElementById('windowTitle').value || 'Yume',
      windowBgColor:       document.getElementById('windowBgColor').value,
      windowOpacity:       parseInt(document.getElementById('windowOpacity').value) || 88,
      windowShadow:        document.getElementById('windowShadow').checked,
      originalFont:        document.getElementById('originalFont').value,
      romajiFont:          document.getElementById('romajiFont').value,
      translationFont:     document.getElementById('translationFont').value,
      originalFontSize:    parseInt(document.getElementById('originalFontSize').value) || 19,
      originalColor:       document.getElementById('originalColor').value,
      romajiFontSize:      parseInt(document.getElementById('romajiFontSize').value) || 13,
      romajiColor:         document.getElementById('romajiColor').value,
      translationFontSize: parseInt(document.getElementById('translationFontSize').value) || 14,
      translationColor:    document.getElementById('translationColor').value,
      timingOffset:        parseInt(document.getElementById('timingOffset').value) || 0,
      glassEnabled:        document.getElementById('glassEnabled').checked,
      glassBlur:           parseInt(document.getElementById('glassBlur').value) || 18,
      glassRadius:         parseInt(document.getElementById('glassRadius').value) || 20,
    };
    chrome.storage.local.set({ settings }, () => {
      if (showNotification) showToast('Settings saved', 'success');
    });
  });
}

// ============================================================================
// DYNAMIC LABELS
// ============================================================================

function updateRomajiLabel() {
  const src = document.getElementById('sourceLanguage').value;
  const label = ROMA_LABELS[src];
  // Hide romanization for Latin-alphabet source languages (no romanization needed)
  const romajiRow = document.getElementById('showRomaji')?.closest('.setting-row');
  if (romajiRow) romajiRow.style.display = label ? '' : 'none';
  const romajiSection = document.getElementById('romajiAppearanceSection');
  if (romajiSection) romajiSection.style.display = label ? '' : 'none';
  const labelOrDefault = label || { name: 'Romanization', hint: '' };
  const hintEl = document.getElementById('romajiHint');
  if (hintEl) hintEl.textContent = labelOrDefault.hint;
  const appLabel = document.getElementById('romajiAppearanceLabel');
  if (appLabel) appLabel.textContent = labelOrDefault.name;
}

// ============================================================================
// GLASS EFFECT (applied to subtitle window, not popup)
// ============================================================================

function _applyGlass(enabled, blur, radius) {
  // Send glass settings to the active tab's subtitle window
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) {
      chrome.tabs.sendMessage(tabs[0].id, {
        action: 'UPDATE_GLASS', glassEnabled: enabled,
        glassBlur: blur, glassRadius: radius
      }).catch(() => {});
    }
  });
}

// Glass toggle
document.getElementById('glassEnabled')?.addEventListener('change', (e) => {
  const on = e.target.checked;
  document.getElementById('glassControls').style.display = on ? 'block' : 'none';
  const blur = parseInt(document.getElementById('glassBlur').value) || 18;
  const radius = parseInt(document.getElementById('glassRadius').value) || 20;
  _applyGlass(on, blur, radius);
  saveSettings(false);
});

// Glass blur slider
document.getElementById('glassBlur')?.addEventListener('input', (e) => {
  const v = e.target.value;
  document.getElementById('glassBlurVal').textContent = v + 'px';
  const radius = parseInt(document.getElementById('glassRadius').value) || 20;
  _applyGlass(true, parseInt(v), radius);
});
document.getElementById('glassBlur')?.addEventListener('change', () => saveSettings(false));

// Glass radius slider
document.getElementById('glassRadius')?.addEventListener('input', (e) => {
  const v = e.target.value;
  document.getElementById('glassRadiusVal').textContent = v + 'px';
  const blur = parseInt(document.getElementById('glassBlur').value) || 18;
  _applyGlass(true, blur, parseInt(v));
});
document.getElementById('glassRadius')?.addEventListener('change', () => saveSettings(false));

// ============================================================================
// TRANSLATION FONT VISIBILITY
// ============================================================================

function updateTranslationFontVisibility() {
  const target = document.getElementById('targetLanguage').value;
  const fontRow = document.getElementById('translationFontRow');
  if (fontRow) {
    fontRow.style.display = LATIN_TARGETS.includes(target) ? '' : 'none';
  }
}

// ============================================================================
// PORT MANAGEMENT
// ============================================================================

function savePorts() {
  const wPort = parseInt(document.getElementById('whisperPort').value) || 5001;
  const tPort = parseInt(document.getElementById('translationPort').value) || 5000;
  const statusEl = document.getElementById('portsStatus');

  chrome.storage.local.get(['settings'], (result) => {
    const settings = {
      ...(result.settings || getDefaultSettings()),
      whisperPort: wPort,
      whisperUrl: `http://localhost:${wPort}`,
      translationPort: tPort,
      translationUrl: `http://localhost:${tPort}`,
    };
    chrome.storage.local.set({ settings }, () => {
      statusEl.textContent = 'Saved! Checking...';
      statusEl.className = 'status-message';
      setTimeout(() => checkServers().then(() => { statusEl.textContent = '\u2713 Connected'; statusEl.className = 'status-message success'; }).catch(() => { statusEl.textContent = 'Server not reachable'; statusEl.className = 'status-message error'; }), 500);
    });
  });
}

function autoFreePorts() {
  // Pick ports that are unlikely to conflict
  const candidates = [5001, 5002, 5003, 5004, 5005, 5006, 5007, 5008, 5009, 5010];
  const tCandidates = [5000, 5050, 5051, 5052, 5053, 5054, 5055];

  // Simple: just pick next pair that isn't 5000 (macOS Airplay) or commonly used
  let wPort = 5001;
  let tPort = 5050;

  document.getElementById('whisperPort').value = wPort;
  document.getElementById('translationPort').value = tPort;
  savePorts();
  showToast(`Ports: Whisper=${wPort}, Translation=${tPort}`, 'success');
}

// ============================================================================
// CUSTOM STREAM URL (m3u8 / direct media)
// ============================================================================

function setCustomStreamUrl() {
  const url = (document.getElementById('customStreamUrl')?.value || '').trim();
  const statusEl = document.getElementById('streamUrlStatus');
  if (!url) {
    statusEl.textContent = 'Enter a URL first';
    statusEl.className = 'status-message error';
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
    return;
  }
  chrome.storage.local.set({ customStreamUrl: url }, () => {
    statusEl.textContent = '\u2713 Stream URL set. Click Enable to start.';
    statusEl.className = 'status-message success';
    showToast('Stream URL saved', 'success');
  });
}

function clearCustomStreamUrl() {
  const el = document.getElementById('customStreamUrl');
  if (el) el.value = '';
  chrome.storage.local.remove('customStreamUrl');
  const statusEl = document.getElementById('streamUrlStatus');
  statusEl.textContent = 'Cleared';
  statusEl.className = 'status-message';
  setTimeout(() => { statusEl.textContent = ''; }, 2000);
}

// Restore saved stream URL on popup open
chrome.storage.local.get(['customStreamUrl'], (result) => {
  if (result.customStreamUrl) {
    const el = document.getElementById('customStreamUrl');
    if (el) el.value = result.customStreamUrl;
  }
});

// ============================================================================
// MODELS
// ============================================================================

async function loadModels() {
  const infoEl = document.getElementById('modelInfo');
  infoEl.innerHTML = '<span class="model-label">Loading...</span>';

  try {
    const response = await new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: 'LIST_MODELS' }, (r) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(r);
      });
    });

    if (!response?.success) {
      infoEl.innerHTML = '<span class="model-label" style="color:var(--text-dim)">Server not running</span>';
      return;
    }

    const backend = response.backend || '?';
    const models = response.models || [];
    const ggufs = response.local_ggufs || [];
    const note = response.note || '';
    let html = `<span class="model-label">Backend: <b>${backend}</b></span>`;
    if (models.length > 0) {
      html += '<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">Loaded:</div>';
      models.forEach(m => { html += `<div style="font-size:12px;color:var(--text-primary);padding:2px 0">\u2022 ${m.name}</div>`; });
    }
    if (ggufs.length > 0) {
      html += '<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">Local GGUFs:</div>';
      ggufs.forEach(g => { html += `<div style="font-size:12px;color:var(--text-primary);padding:2px 0">\u2022 ${g.name} <span style="color:var(--text-dim)">(${g.size_mb} MB)</span></div>`; });
    }
    if (note) html += `<div style="margin-top:6px;font-size:10px;color:var(--border-gold);font-style:italic">${note}</div>`;
    infoEl.innerHTML = html;
  } catch (e) {
    infoEl.innerHTML = '<span class="model-label" style="color:var(--text-dim)">Could not load models</span>';
  }
}

// ============================================================================
// CACHE
// ============================================================================

async function clearCache() {
  try {
    // Clear server-side subtitle + audio caches
    const serverResult = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'CLEAR_SERVER_CACHE' }, resolve);
    });
    // Clear in-memory translation/romanization caches in background.js
    await new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'CLEAR_TRANSLATION_CACHE' }, resolve);
    });
    // Clear content script's in-memory subtitle data (the actual displayed subs)
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id) {
      await new Promise((resolve) => {
        chrome.tabs.sendMessage(tab.id, { action: 'CLEAR_CONTENT_CACHE' }, (r) => {
          resolve(r); // ignore errors (tab might not have content script)
        });
      }).catch(e => console.warn('[Yume]', e.message));
    }
    const count = serverResult?.cleared || 0;
    showToast(`Cache cleared (${count} server chunks)`, 'success');
  } catch (e) { showToast('Failed to clear cache', 'error'); }
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

function setupEventListeners() {
  document.getElementById('toggleSubtitles').addEventListener('click', toggleSubtitles);
  document.getElementById('refreshStatus').addEventListener('click', () => checkServers().catch(console.error));
  document.getElementById('clearCache').addEventListener('click', clearCache);
  document.getElementById('refreshDiag').addEventListener('click', () => fetchDiagnostics().catch(console.error));
  document.getElementById('downloadDiag').addEventListener('click', () => downloadDiagnostics().catch(console.error));
  document.getElementById('refreshModels').addEventListener('click', () => loadModels().catch(console.error));

  // Port controls
  document.getElementById('savePorts').addEventListener('click', savePorts);
  document.getElementById('autoFreePorts').addEventListener('click', autoFreePorts);

  // Custom stream URL
  document.getElementById('setStreamUrl')?.addEventListener('click', setCustomStreamUrl);
  document.getElementById('clearStreamUrl')?.addEventListener('click', clearCustomStreamUrl);

  // Checkboxes + selects: save on change
  ['showOriginal', 'showEnglish', 'showRomaji', 'showChunkCounter',
   'showConfidence', 'windowShadow'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => saveSettings(true));
  });

  // Source language → update romaji label + save
  document.getElementById('sourceLanguage')?.addEventListener('change', () => {
    updateRomajiLabel();
    saveSettings(true);
  });

  // Target language → update font visibility + save
  document.getElementById('targetLanguage')?.addEventListener('change', () => {
    updateTranslationFontVisibility();
    saveSettings(true);
  });

  // Font dropdowns
  ['originalFont', 'romajiFont', 'translationFont'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => saveSettings(true));
  });

  // Numbers + text: save on change
  ['chunkDuration', 'windowTitle', 'originalFontSize', 'romajiFontSize', 'translationFontSize'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => saveSettings(true));
  });

  // Range slider
  const opacitySlider = document.getElementById('windowOpacity');
  opacitySlider?.addEventListener('input', () => {
    document.getElementById('opacityVal').textContent = opacitySlider.value + '%';
    saveSettings(false);
  });
  opacitySlider?.addEventListener('change', () => showToast('Settings saved', 'success'));

  // Timing offset slider
  const offsetSlider = document.getElementById('timingOffset');
  offsetSlider?.addEventListener('input', () => {
    document.getElementById('timingOffsetVal').textContent = (offsetSlider.value / 10).toFixed(1) + 's';
    saveSettings(false);
  });
  offsetSlider?.addEventListener('change', () => showToast('Settings saved', 'success'));

  // Color pickers
  ['windowBgColor', 'originalColor', 'romajiColor', 'translationColor'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('input', () => saveSettings(false));
    el.addEventListener('change', () => { saveSettings(false); showToast('Settings saved', 'success'); });
  });

  // Hallucination controls
  document.getElementById('reportHallucination').addEventListener('click', reportHallucination);
  document.getElementById('updateBlacklist').addEventListener('click', updateBlacklistOnServer);
  document.getElementById('clearBlacklist').addEventListener('click', clearBlacklist);
  document.getElementById('toggleBlacklistView').addEventListener('click', toggleBlacklistView);
  loadBlacklist();

  // Stats dashboard
  document.getElementById('refreshStats')?.addEventListener('click', () => fetchStats().catch(console.error));

  // Whisper model switcher
  document.getElementById('switchWhisperModel')?.addEventListener('click', () => switchWhisperModel().catch(console.error));

  // SRT export
  document.getElementById('exportSrt')?.addEventListener('click', () => exportSrt().catch(console.error));
}

// ============================================================================
// DIAGNOSTICS
// ============================================================================

let lastDiagData = null;

async function fetchDiagnostics() {
  const statusEl = document.getElementById('diagStatus');
  const logEl = document.getElementById('diagLog');
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { statusEl.textContent = 'No active tab'; return; }
    const response = await chrome.tabs.sendMessage(tab.id, { action: 'GET_DIAGNOSTICS' });
    if (!response?.success) { statusEl.textContent = 'Not running'; logEl.innerHTML = ''; return; }
    const d = response.diagnostics;
    if (!d) { statusEl.textContent = 'Idle'; logEl.innerHTML = ''; return; }

    lastDiagData = { ...d, tabUrl: tab.url, timestamp: new Date().toISOString() };
    const parts = [
      d.active ? 'ACTIVE' : 'STOPPED',
      d.prepared ? 'PREPARED' : 'NO AUDIO',
      `chunks: ${d.fetchedCount}/${d.totalChunks || '?'}`,
      d.fetchingCount > 0 ? `loading: ${d.fetchingCount}` : '',
      d.pipelineRunning ? 'PIPELINE' : '',
      d.stopped ? 'PREFETCH STOPPED' : '',
      `${d.chunkDuration}s/${d.stepSize || 25}s`
    ].filter(Boolean);
    statusEl.textContent = parts.join(' | ');

    if (!d.log || d.log.length === 0) {
      logEl.innerHTML = '<div style="color:var(--text-dim);font-style:italic">No entries yet</div>';
      return;
    }
    logEl.innerHTML = d.log.map(entry => {
      const badge = `<span class="diag-badge ${entry.status}">${entry.status}</span>`;
      const segs = entry.segments > 0 ? `<span class="diag-segs">${entry.segments}</span>` : '';
      const elapsed = entry.elapsed ? `${entry.elapsed}s` : '';
      const chunkLabel = entry.chunk >= 0 ? `#${entry.chunk}` : 'prep';
      return `<div class="diag-entry"><span class="diag-time">${entry.time}</span><span class="diag-chunk">${chunkLabel}</span>${badge}${segs}<span class="diag-detail">${elapsed} ${entry.details || ''}</span></div>`;
    }).join('');
    logEl.scrollTop = logEl.scrollHeight;
  } catch (e) { statusEl.textContent = 'Content script not loaded'; logEl.innerHTML = ''; }
}

async function downloadDiagnostics() {
  await fetchDiagnostics();
  if (!lastDiagData) { showToast('No diagnostics', 'error'); return; }
  const blob = new Blob([JSON.stringify(lastDiagData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url;
  a.download = `yume-diag-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
  showToast('Diagnostics downloaded', 'success');
}

// ============================================================================
// HALLUCINATION BLACKLIST
// ============================================================================

let blacklistVisible = false;

function toggleBlacklistView() {
  blacklistVisible = !blacklistVisible;
  const container = document.getElementById('blacklistContainer');
  const btn = document.getElementById('toggleBlacklistView');
  container.style.display = blacklistVisible ? '' : 'none';
  btn.textContent = blacklistVisible ? '\u25B2' : '\u25BC';
  if (blacklistVisible) loadBlacklist();
}

async function reportHallucination() {
  const previewEl = document.getElementById('hallucinationPreview');
  const statusEl = document.getElementById('blacklistStatus');
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { statusEl.textContent = 'No active tab'; statusEl.className = 'status-message error'; return; }
    const response = await chrome.tabs.sendMessage(tab.id, { action: 'GET_CURRENT_SUBTITLE' });
    if (!response?.success || !response.subtitle?.original) {
      statusEl.textContent = 'No subtitle currently displayed';
      statusEl.className = 'status-message error';
      setTimeout(() => { statusEl.textContent = ''; }, 3000);
      return;
    }
    const text = response.subtitle.original.trim();
    if (!text) { statusEl.textContent = 'Empty subtitle'; statusEl.className = 'status-message error'; setTimeout(() => { statusEl.textContent = ''; }, 3000); return; }

    previewEl.style.display = 'block';
    previewEl.innerHTML = `
      <div class="preview-label">Captured:</div>
      <div class="preview-text">${_escapeHtml(text)}</div>
      <div style="display:flex;gap:6px;margin-top:6px">
        <button class="yume-button primary preview-confirm" style="flex:1;padding:4px 8px;font-size:11px">Add to Blacklist</button>
        <button class="yume-button secondary preview-cancel" style="flex:1;padding:4px 8px;font-size:11px">Cancel</button>
      </div>
    `;
    previewEl.querySelector('.preview-confirm').addEventListener('click', () => { addToBlacklist(text); previewEl.style.display = 'none'; });
    previewEl.querySelector('.preview-cancel').addEventListener('click', () => { previewEl.style.display = 'none'; });
  } catch (e) { statusEl.textContent = 'Content script not loaded'; statusEl.className = 'status-message error'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }
}

function addToBlacklist(text) {
  chrome.storage.local.get(['hallucinationBlacklist'], (result) => {
    const list = result.hallucinationBlacklist || [];
    const normalized = text.trim().toLowerCase();
    if (list.some(item => item.toLowerCase() === normalized)) { showToast('Already in blacklist', 'success'); return; }
    list.push(text.trim());
    chrome.storage.local.set({ hallucinationBlacklist: list }, () => { showToast('Added to blacklist', 'success'); loadBlacklist(); });
  });
}

function loadBlacklist() {
  chrome.storage.local.get(['hallucinationBlacklist'], (result) => {
    const list = result.hallucinationBlacklist || [];
    document.getElementById('blacklistCount').textContent = `${list.length} item${list.length !== 1 ? 's' : ''}`;
    const container = document.getElementById('blacklistContainer');
    if (list.length === 0) { container.innerHTML = '<div class="blacklist-empty">No items yet</div>'; return; }
    container.innerHTML = list.map((item, i) => `
      <div class="blacklist-item">
        <span class="blacklist-text">${_escapeHtml(item)}</span>
        <button class="blacklist-remove" data-index="${i}" title="Remove">\u00d7</button>
      </div>
    `).join('');
    container.querySelectorAll('.blacklist-remove').forEach(btn => {
      btn.addEventListener('click', () => removeFromBlacklist(parseInt(btn.getAttribute('data-index'))));
    });
  });
}

function removeFromBlacklist(index) {
  chrome.storage.local.get(['hallucinationBlacklist'], (result) => {
    const list = result.hallucinationBlacklist || [];
    list.splice(index, 1);
    chrome.storage.local.set({ hallucinationBlacklist: list }, () => { loadBlacklist(); showToast('Removed', 'success'); });
  });
}

function clearBlacklist() {
  chrome.storage.local.set({ hallucinationBlacklist: [] }, () => {
    loadBlacklist();
    showToast('Blacklist cleared', 'success');
    document.getElementById('blacklistStatus').textContent = '';
  });
}

async function updateBlacklistOnServer() {
  const statusEl = document.getElementById('blacklistStatus');
  statusEl.textContent = 'Sending to server...';
  statusEl.className = 'status-message';
  try {
    const { hallucinationBlacklist, settings } = await new Promise(r =>
      chrome.storage.local.get(['hallucinationBlacklist', 'settings'], r)
    );
    const list = hallucinationBlacklist || [];
    if (list.length === 0) { statusEl.textContent = 'Blacklist is empty'; statusEl.className = 'status-message error'; setTimeout(() => { statusEl.textContent = ''; }, 3000); return; }

    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
    const response = await new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: 'UPDATE_BLACKLIST', blacklist: list, whisperUrl }, (r) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(r);
      });
    });

    if (response?.success) {
      statusEl.textContent = `\u2713 Server updated (${list.length} items). New transcriptions will also be filtered.`;
      statusEl.className = 'status-message success';
    } else {
      statusEl.textContent = response?.error || 'Update failed';
      statusEl.className = 'status-message error';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
  } catch (e) { statusEl.textContent = 'Failed: ' + e.message; statusEl.className = 'status-message error'; setTimeout(() => { statusEl.textContent = ''; }, 4000); }
}

function _escapeHtml(text) { const div = document.createElement('div'); div.textContent = text; return div.innerHTML; }

// ============================================================================
// SERVER STATS DASHBOARD
// ============================================================================

let statsInterval = null;

async function fetchStats() {
  const el = document.getElementById('statsContent');
  if (!el) return;
  try {
    const { settings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';

    // Ensure we have a valid API token (discover from /health if needed)
    let token = await _getApiToken();
    if (!token) {
      try {
        const hc = new AbortController();
        setTimeout(() => hc.abort(), 3000);
        const hr = await fetch(`${whisperUrl}/health`, { signal: hc.signal });
        if (hr.ok) {
          const hd = await hr.json();
          if (hd.api_token) {
            token = hd.api_token;
            try { await chrome.storage.session.set({ apiToken: token }); } catch (e) { console.warn('[Yume]', e.message); }
          }
        }
      } catch (e) { /* health check failed, proceed anyway */ }
    }

    const controller = new AbortController();
    setTimeout(() => controller.abort(), 5000);
    const headers = {};
    if (token) headers['X-API-Token'] = token;
    const response = await fetch(`${whisperUrl}/stats`, { headers, signal: controller.signal });
    if (!response.ok) {
      if (response.status === 403) throw new Error('Token expired — reopen popup');
      throw new Error(`HTTP ${response.status}`);
    }
    const s = await response.json();

    // Build display
    let html = '';

    // GPU section
    if (s.gpu) {
      const pct = Math.round((s.gpu.vram_used_mb / s.gpu.vram_total_mb) * 100);
      const color = pct > 90 ? 'var(--accent-red)' : pct > 70 ? 'var(--border-gold)' : 'var(--accent-green)';
      html += `<div style="margin-bottom:8px">`;
      html += `<b>${_escapeHtml(s.gpu.gpu_name)}</b><br>`;
      html += `<div style="display:flex;align-items:center;gap:8px;margin:4px 0">`;
      html += `<div style="flex:1;height:8px;background:rgba(255,255,255,0.1);border-radius:4px;overflow:hidden">`;
      html += `<div style="width:${pct}%;height:100%;background:${color};border-radius:4px"></div></div>`;
      html += `<span style="font-size:11px;color:${color}">${s.gpu.vram_used_mb}/${s.gpu.vram_total_mb} MB</span></div>`;
      html += `GPU: ${s.gpu.gpu_util_pct}% &nbsp;|&nbsp; ${s.gpu.gpu_temp_c}\u00b0C`;
      html += `</div>`;
    }

    // Session stats
    html += `<b>Whisper:</b> ${_escapeHtml(s.model)} (${_escapeHtml(s.device)}/${_escapeHtml(s.compute_type)})<br>`;
    html += `Chunks: <b>${s.chunks_transcribed}</b> &nbsp;|&nbsp; Segments: <b>${s.segments_produced}</b><br>`;
    html += `Avg time/chunk: <b>${s.avg_whisper_time}s</b> &nbsp;|&nbsp; Last: ${s.last_chunk_whisper_time}s (${s.last_chunk_segments} segs)<br>`;
    html += `Audio processed: <b>${Math.round(s.total_audio_seconds)}s</b> &nbsp;|&nbsp; Cache hits: ${s.cache_hits}<br>`;
    html += `Hallucinations blocked: <b>${s.hallucinations_filtered}</b> &nbsp;|&nbsp; Blacklist: ${s.blacklist_size}<br>`;
    html += `Uptime: ${s.uptime_human} &nbsp;|&nbsp; Cache: ${s.subtitle_cache_size} chunks`;

    el.innerHTML = html;

    // Also update model switcher display
    const modelEl = document.getElementById('currentWhisperModel');
    if (modelEl) modelEl.textContent = s.model;
    const selectEl = document.getElementById('whisperModelSelect');
    if (selectEl) selectEl.value = s.model;

  } catch (e) {
    el.textContent = 'Stats unavailable: ' + e.message;
  }
}

// Auto-refresh stats when section is open
function startStatsPolling() {
  if (statsInterval) return;
  fetchStats().catch(e => console.warn('[Yume]', e.message));
  statsInterval = setInterval(() => fetchStats().catch(e => console.warn('[Yume]', e.message)), 5000);
}

function stopStatsPolling() {
  if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }
}

// ============================================================================
// WHISPER MODEL SWITCHER
// ============================================================================

async function switchWhisperModel() {
  const selectEl = document.getElementById('whisperModelSelect');
  const statusEl = document.getElementById('modelSwitchStatus');
  if (!selectEl || !statusEl) return;

  const newModel = selectEl.value;
  statusEl.textContent = `Loading ${newModel}...`;
  statusEl.className = 'status-message';

  try {
    const { settings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
    const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';
    const token = await _getApiToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['X-API-Token'] = token;
    const response = await fetch(`${whisperUrl}/model/switch`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ model: newModel })
    });
    const result = await response.json();

    if (response.ok) {
      if (result.status === 'already_loaded') {
        statusEl.textContent = `Already using ${newModel}`;
        statusEl.className = 'status-message';
      } else {
        statusEl.textContent = `\u2713 Switched to ${result.model}`;
        statusEl.className = 'status-message success';
        showToast(`Model: ${result.model}`, 'success');
        const modelEl = document.getElementById('currentWhisperModel');
        if (modelEl) modelEl.textContent = result.model;
      }
    } else {
      statusEl.textContent = result.error || 'Switch failed';
      statusEl.className = 'status-message error';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
    statusEl.className = 'status-message error';
    setTimeout(() => { statusEl.textContent = ''; }, 4000);
  }
}

// ============================================================================
// SRT EXPORT
// ============================================================================

async function exportSrt() {
  const statusEl = document.getElementById('exportStatus');
  if (statusEl) { statusEl.textContent = 'Exporting...'; statusEl.className = 'status-message'; }

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error('No active tab');

    const response = await new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tab.id, { action: 'EXPORT_SRT' }, (r) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(r);
      });
    });

    if (!response?.success) throw new Error(response?.error || 'Export failed');
    if (!response.srt || response.count === 0) throw new Error('No subtitles to export');

    // Create and download file
    const blob = new Blob([response.srt], { type: 'text/srt;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const filename = `yume-subtitles-${new Date().toISOString().slice(0,16).replace(/[T:]/g,'-')}.srt`;

    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    const pct = response.total > 0 ? Math.round((response.fetched / response.total) * 100) : 0;
    if (statusEl) {
      statusEl.textContent = `\u2713 Exported ${response.count} subtitles (${pct}% of video processed)`;
      statusEl.className = 'status-message success';
    }
    showToast(`Exported ${response.count} subtitles`, 'success');
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 5000);
  } catch (e) {
    if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'status-message error'; }
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 4000);
  }
}

// ============================================================================
// TOAST
// ============================================================================

function showToast(message, type = 'success') {
  document.querySelectorAll('.yume-toast').forEach(t => t.remove());
  const toast = document.createElement('div');
  toast.className = `yume-toast ${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 1500);
}
