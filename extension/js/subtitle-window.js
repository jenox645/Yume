// ============================================================================
// SUBTITLE WINDOW
// Draggable/resizable overlay, progress badge, ready toast, per-line fonts.
// Lives in a Shadow DOM: its CSS used to be injected into EVERY page, where
// generic selectors like .close-btn or .resize-handle restyled the sites' own
// elements (and site CSS leaked into the overlay).
// ============================================================================

// [selector, settings prefix, default size (px), default colour] of each line
const LINE_STYLES = [
  ['.subtitle-original', 'original', 19, '#ffffff'],
  ['.subtitle-romaji', 'romaji', 13, '#ffc88c'],
  ['.subtitle-english', 'translation', 14, '#b4beff'],
];

// '#rrggbb' → [r, g, b]; an unreadable channel takes the default's
function hexRGB(hex, fallback) {
  return fallback.map((d, i) => {
    const n = parseInt(String(hex).slice(1 + 2 * i, 3 + 2 * i), 16);
    return Number.isNaN(n) ? d : n;
  });
}

// The window's own style: background, shadow, corners and the glass effect
function windowStyle(s) {
  const [r, g, b] = hexRGB(s.windowBgColor || '#08080c', [8, 8, 12]);
  const opacity = (s.windowOpacity ?? 88) / 100;
  const radius = s.glassRadius ?? 20;  // 20 = the popup slider's default
  const glass = Boolean(s.glassEnabled);
  const blur = `blur(${s.glassBlur ?? 18}px) saturate(1.3)`;
  return {
    // Glass: a more transparent background so the blur behind shows through
    background: `rgba(${r}, ${g}, ${b}, ${glass ? Math.min(opacity, 0.45) : opacity})`,
    boxShadow: s.windowShadow === false ? 'none' : '',
    borderRadius: radius > 0 ? `${radius}px` : '',
    backdropFilter: glass ? blur : 'none',
    webkitBackdropFilter: glass ? blur : 'none',
    border: glass ? '1px solid rgba(255, 255, 255, 0.15)' : '',
  };
}

// Opt-in: a less sure line (Whisper's avg_logprob) is shown fainter
function confidenceOpacity(show, c) {
  if (!show || c == null || c >= 0) return 1.0;
  return c > -0.3 ? 1.0 : c > -0.7 ? 0.85 : 0.65;
}

// eslint-disable-next-line no-redeclare
class SubtitleWindow {
  constructor() {
    this.host       = null;   // <yume-subtitles> in the page; the overlay lives in its shadow root
    this.element    = null;
    this.isDragging = false;
    this.isResizing = false;
    this.dragOffset = { x: 0, y: 0 };
    this.isPlaying  = false;
    this.settings   = {};
    this.currentOriginal = '';
    this.currentEnglish  = '';
    this.currentRomaji   = '';

    // Create element synchronously so it's never null when events fire
    this.create();
    this.attachEventListeners();
    this._loadAndApplySettings();

    // Fullscreen: the browser renders ONLY the fullscreen element, so a
    // body-appended overlay disappears the moment the user goes fullscreen —
    // the most common way to watch. Reparent into the fullscreen container.
    this._onFullscreenChange = () => this._handleFullscreenChange();
    document.addEventListener('fullscreenchange', this._onFullscreenChange);
    document.addEventListener('webkitfullscreenchange', this._onFullscreenChange);
    // The user may enable subtitles while ALREADY in fullscreen (Alt+Y)
    this._handleFullscreenChange();
  }

  _handleFullscreenChange() {
    if (!this.element) return;
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement || null;
    // If the fullscreen element is the <video> itself we can't inject children
    // into it — leave the window in body (hidden in fullscreen, same as before).
    // Most players (YouTube included) fullscreen a container div, which works.
    const canHost = fsEl && fsEl.tagName !== 'VIDEO' && !this.host.contains(fsEl);
    const parent = canHost ? fsEl : document.documentElement;
    if (this.host.parentNode !== parent) {
      parent.appendChild(this.host);
      this._clampToViewport();
    }
  }

  _loadAndApplySettings() {
    chrome.storage.local.get(['settings', 'windowPosition'], (result) => {
      if (chrome.runtime.lastError) {
        console.warn('[Yume] Failed to load settings:', chrome.runtime.lastError);
        this.settings = {};
        this.element.style.left   = '20px';
        this.element.style.bottom = '80px';
        this.applyCustomStyles();
        return;
      }
      this.settings = result.settings || {};
      const p = result.windowPosition;
      if (p) {
        if (p.left)   this.element.style.left   = p.left;
        if (p.top) {
          this.element.style.top = p.top;
          // The stylesheet's default is bottom:80px; with both top and bottom set,
          // a fixed element stretches between them — every window opened after a
          // drag used to come up stretched to the bottom of the screen.
          this.element.style.bottom = 'auto';
        }
        if (p.width)  this.element.style.width   = p.width;
        if (p.height) this.element.style.height  = p.height;
        // A position saved on a larger screen (or after a stray drag) can land
        // entirely offscreen with no way to grab it back — clamp into view.
        this._clampToViewport();
      } else {
        this.element.style.left   = '20px';
        this.element.style.bottom = '80px';
      }
      this.applyCustomStyles();
    });
  }

  // Keep at least the header reachable: clamp so a grabbable strip of the
  // window is always inside the viewport.
  _clampToViewport() {
    if (!this.element) return;
    const rect = this.element.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return; // not laid out yet
    const margin = 60; // px of the window that must stay visible
    const maxLeft = window.innerWidth - margin;
    const maxTop  = window.innerHeight - 40; // header height
    const minLeft = margin - rect.width;
    let left = rect.left, top = rect.top, moved = false;
    if (left > maxLeft) { left = maxLeft; moved = true; }
    if (left < minLeft) { left = Math.max(0, minLeft); moved = true; }
    if (top > maxTop)   { top = maxTop; moved = true; }
    if (top < 0)        { top = 0; moved = true; }
    if (moved) {
      this.element.style.left = `${left}px`;
      this.element.style.top = `${top}px`;
      this.element.style.bottom = 'auto';
      this.element.style.right = 'auto';
    }
  }

  create() {
    // Remove any orphaned prior overlay so we never stack two windows
    // (a second construction without close() leaves a frozen, un-closable box).
    document.querySelectorAll('yume-subtitles').forEach((el) => el.remove());
    this.host = document.createElement('yume-subtitles');
    const root = this.host.attachShadow({ mode: 'closed' });
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = chrome.runtime.getURL('css/content.css');
    root.appendChild(css);

    this.element = document.createElement('div');
    this.element.className = 'subtitle-window';
    this.element.setAttribute('role', 'region');
    this.element.setAttribute('aria-label', 'Yume subtitles');

    this.element.innerHTML = `
      <div class="subtitle-header">
        <div class="subtitle-title">Yume</div>
        <div class="subtitle-controls">
          <div class="chunk-badge" style="display:none" title="Progress"></div>
          <button class="control-btn minimize-btn" title="Minimize" aria-label="Minimize subtitles" aria-expanded="true">\u2212</button>
          <button class="control-btn close-btn" title="Close" aria-label="Close subtitles">\u00d7</button>
        </div>
      </div>
      <div class="subtitle-content">
        <div class="subtitle-notice" style="display:none" role="status"></div>
        <div class="subtitle-status">Initializing...</div>
        <div class="subtitle-original" style="display:none"></div>
        <div class="subtitle-romaji" style="display:none"></div>
        <div class="subtitle-english" style="display:none"></div>
      </div>
      <div class="ready-toast" style="display:none">Ready \u2713</div>
      <div class="resize-handle"></div>
    `;

    // The title is set via textContent (not innerHTML) by applyCustomStyles()
    root.appendChild(this.element);
    // documentElement, not body: some pages replace <body> on navigation
    document.documentElement.appendChild(this.host);
    this._injectBundledFonts();
  }

  _injectBundledFonts() {
    // Inject @font-face for the fonts registered in js/bundled-fonts.js (the same
    // list the popup offers). It used to read a 'bundledFonts' storage key that
    // nothing ever wrote, so a selected bundled font never actually loaded.
    if (document.getElementById('yume-bundled-fonts')) return;
    try {
      const fonts = typeof BUNDLED_FONTS !== 'undefined' ? BUNDLED_FONTS : [];
      if (!fonts.length) return;
      const clean = (v) => String(v).replace(/["'\\;<>]/g, '');
      let css = '';
      for (const f of fonts) {
        const url = chrome.runtime.getURL('fonts/' + clean(f.file));
        css += `@font-face { font-family: "${clean(f.name)}"; src: url("${url}"); font-display: swap; }\n`;
      }
      const style = document.createElement('style');
      style.id = 'yume-bundled-fonts';
      style.textContent = css;
      document.head.appendChild(style);
    } catch (e) { console.warn('[Yume] Font injection failed:', e.message); }
  }

  applyCustomStyles() {
    if (!this.element) return;
    const s = this.settings;
    Object.assign(this.element.style, windowStyle(s));
    // Here, not only on a settings change: create() runs before the settings
    // are read, so a custom title used to show only after the next change
    const titleEl = this.element.querySelector('.subtitle-title');
    if (titleEl) titleEl.textContent = s.windowTitle || 'Yume';
    // Per line: size, colour and font from the settings named after it
    for (const [selector, name, size, color] of LINE_STYLES) {
      const el = this.element.querySelector(selector);
      if (!el) continue;
      el.style.fontSize = `${s[name + 'FontSize'] || size}px`;
      el.style.color = s[name + 'Color'] || color;
      // Characters that could break out of the CSS font-family string are dropped
      const font = (s[name + 'Font'] || '').replace(/["'\\;]/g, '');
      el.style.fontFamily = s[name + 'Font'] ? `"${font}", -apple-system, sans-serif` : '';
    }
    const badge = this.element.querySelector('.chunk-badge');
    if (badge && s.showChunkCounter === false) badge.style.display = 'none';
  }

  attachEventListeners() {
    this.element.querySelector('.close-btn').addEventListener('click', () => this.close());
    this.element.querySelector('.minimize-btn').addEventListener('click', () => this.toggleMinimize());

    const header = this.element.querySelector('.subtitle-header');
    header.addEventListener('mousedown', (e) => this.startDrag(e));
    const resizeHandle = this.element.querySelector('.resize-handle');
    resizeHandle.addEventListener('mousedown', (e) => this.startResize(e));

    // Document/storage listeners outlive the element — kept as fields so close()
    // can remove them (each enable creates a new window; they used to pile up).
    this._onMouseMove = (e) => { this.drag(e); this.resize(e); };
    this._onMouseUp = () => { this.stopDrag(); this.stopResize(); };
    document.addEventListener('mousemove', this._onMouseMove);
    document.addEventListener('mouseup', this._onMouseUp);

    this._onStorageChanged = (changes) => {
      if (changes.settings) {
        this.settings = changes.settings.newValue || {};
        this.applyCustomStyles();

        // Badge visibility follows the counter setting immediately
        if (this._lastProgress) this.updateChunkProgress(this._lastProgress);

        // Re-render the CURRENTLY displayed cue so show/hide toggles (original,
        // romaji, english), RTL, and confidence apply live to the line already on
        // screen — not only to the next cue.
        if (this.isPlaying && (this.currentOriginal || this.currentEnglish)) {
          this.updateSubtitle(this.currentOriginal, this.currentEnglish,
                              this.currentRomaji, this.currentConfidence);
        }
      }
    };
    chrome.storage.onChanged.addListener(this._onStorageChanged);
  }

  // ========================================================================
  // CHUNK PROGRESS BADGE
  // ========================================================================

  // detail: {done, total, complete, lines, translated, status} (regions / lines)
  updateChunkProgress(detail) {
    if (!this.element) return;
    const badge = this.element.querySelector('.chunk-badge');
    if (!badge) return;
    this._lastProgress = detail;
    if (this.settings.showChunkCounter === false) { badge.style.display = 'none'; return; }
    const { done = 0, total = 0, complete, lines = 0, translated = 0, status } = detail;

    if (complete) {
      if (badge.classList.contains('complete')) return;  // already shown/fading
      badge.textContent = '\u2713';
      badge.title = 'Done';
      badge.className = 'chunk-badge complete';
      badge.style.display = '';
      setTimeout(() => {
        if (badge.classList.contains('complete')) {
          badge.classList.add('fade-out');
          // Still the finished run's badge: a new run (resetProgress) may have taken it over
          setTimeout(() => { if (badge.classList.contains('complete')) badge.style.display = 'none'; }, 600);
        }
      }, 3000);
      return;
    }
    // Say what is being counted: it switches from audio sections to translated
    // lines once Whisper is done, and translations arrive a batch at a time
    const phase = { downloading: ['Downloading\u2026', 'Downloading audio'],
      separating: ['Separating\u2026', 'Separating the vocals from the music'] }[status];
    if (phase || !total) {  // the stream preview's single region must not read "Listening 1/1" here
      badge.textContent = phase ? phase[0] : '\u2026';
      badge.title = phase ? phase[1] : 'Starting';
    } else if (status === 'translating') {
      badge.textContent = `Translating ${translated}/${lines}`;
      badge.title = 'Lines translated (the translator works through them in batches, lines near the playhead first)';
    } else {
      badge.textContent = `Listening ${done}/${total}`;
      badge.title = 'Audio sections transcribed';
    }
    badge.className = 'chunk-badge active';
    badge.style.display = '';
  }

  // Reset progress UI when a (new) pipeline run starts, so a previous run's stale
  // "✓ complete" badge or "Ready ✓" toast doesn't linger through the next run's
  // download + first-chunk wait. Shows an immediate "working" badge (…) so the user
  // gets feedback that something is happening during that (often multi-second) gap
  // instead of a frozen/blank window.
  resetProgress() {
    this._lastProgress = null;
    if (!this.element) return;
    this.showNotice('');  // about the previous video
    const toast = this.element.querySelector('.ready-toast');
    if (toast) { toast.classList.remove('fade-out'); toast.style.display = 'none'; }
    const badge = this.element.querySelector('.chunk-badge');
    if (badge) {
      if (this.settings.showChunkCounter === false) {
        badge.style.display = 'none';
      } else {
        badge.textContent = '…';  // … — "working, counting soon"
        badge.className = 'chunk-badge active';
        badge.style.display = '';
      }
    }
  }

  // ========================================================================
  // CONTENT UPDATES
  // ========================================================================

  updateSubtitle(original, english, romaji, confidence) {
    if (!this.element) return;
    const s = this.settings;
    const q = (selector) => this.element.querySelector(selector);
    // RTL per line: the original is Arabic when the SOURCE is, the translation
    // when the TARGET is (romanization is always Latin; null = left as is).
    const srcRTL = s.sourceLanguage === 'ar';
    const lines = [  // [element, text, shown, right-to-left]
      [q('.subtitle-original'), original, s.showOriginal !== false, srcRTL],
      [q('.subtitle-english'), english, s.showEnglish !== false, s.targetLanguage === 'Arabic'],
      [q('.subtitle-romaji'), romaji, s.showRomaji === true, null],
    ];
    if (!original && !english) {
      for (const [el] of lines) if (el) { el.textContent = ''; el.style.display = 'none'; }
      this.currentOriginal = ''; this.currentEnglish = ''; this.currentRomaji = '';
      return;
    }

    this.isPlaying = true;
    this.currentOriginal = original || '';
    this.currentEnglish  = english || '';
    this.currentRomaji   = romaji || '';
    this.currentConfidence = confidence;

    const statusEl = q('.subtitle-status');
    if (statusEl) statusEl.style.display = 'none';
    for (const [el, text, shown, rtl] of lines) {
      if (!el) continue;
      if (rtl !== null) el.style.direction = rtl ? 'rtl' : 'ltr';
      el.style.textAlign = srcRTL ? 'right' : 'center';  // every line aligned like the original
      el.textContent = text || '';
      el.style.display = text && shown ? 'block' : 'none';
    }
    const englishEl = lines[1][0];
    if (englishEl) englishEl.style.opacity = confidenceOpacity(s.showConfidence, confidence);
  }

  updateStatus(message, type = 'info') {
    if (this.isPlaying || !this.element) return;
    const statusEl = this.element.querySelector('.subtitle-status');
    if (statusEl) {
      statusEl.textContent = message;
      statusEl.style.display = 'block';
      statusEl.className = `subtitle-status status-${type}`;
    }
    ['subtitle-original','subtitle-english','subtitle-romaji'].forEach(c => {
      const el = this.element.querySelector('.'+c);
      if (el) el.style.display = 'none';
    });
  }

  showError(message) { this.isPlaying = false; this.updateStatus(message, 'error'); }

  // A hint about the whole video (it sounds like another language than the one
  // picked), shown above the lines while they play; '' hides it
  showNotice(message) {
    const el = this.element?.querySelector('.subtitle-notice');
    if (!el) return;
    el.textContent = message || '';
    el.style.display = message ? 'block' : 'none';
  }

  showReady() {
    if (!this.element) return;
    const toast = this.element.querySelector('.ready-toast');
    if (toast) {
      toast.style.display = 'block';
      toast.classList.remove('fade-out');
      setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => { toast.style.display = 'none'; }, 600);
      }, 2000);
    }
    if (!this.isPlaying) {
      const statusEl = this.element.querySelector('.subtitle-status');
      if (statusEl) { statusEl.textContent = 'Ready \u2713'; statusEl.style.display = 'block'; statusEl.className = 'subtitle-status status-success'; }
    }
  }

  getCurrentText() {
    return { original: this.currentOriginal, english: this.currentEnglish, romaji: this.currentRomaji };
  }

  // ========================================================================
  // DRAGGING / RESIZING / PERSISTENCE
  // ========================================================================

  startDrag(e) {
    if (e.target.closest('button') || e.target.closest('.chunk-badge')) return;
    const sel = window.getSelection();
    if (sel && sel.toString().length > 0) return;
    this.isDragging = true;
    this.dragOffset = { x: e.clientX - this.element.offsetLeft, y: e.clientY - this.element.offsetTop };
    this.element.style.cursor = 'grabbing';
  }
  drag(e) {
    if (!this.isDragging) return; e.preventDefault();
    this.element.style.left = `${e.clientX - this.dragOffset.x}px`;
    this.element.style.top  = `${e.clientY - this.dragOffset.y}px`;
    this.element.style.bottom = 'auto'; this.element.style.right = 'auto';
  }
  stopDrag() { if (this.isDragging) { this.isDragging = false; this.element.style.cursor = 'default'; this._clampToViewport(); this.savePosition(); } }

  startResize(e) { e.preventDefault(); this.isResizing = true; this.resizeStart = { x: e.clientX, y: e.clientY, width: this.element.offsetWidth, height: this.element.offsetHeight }; }
  resize(e) {
    if (!this.isResizing) return; e.preventDefault();
    this.element.style.width  = Math.max(300, this.resizeStart.width  + (e.clientX - this.resizeStart.x)) + 'px';
    this.element.style.height = Math.max(100, Math.min(600, this.resizeStart.height + (e.clientY - this.resizeStart.y))) + 'px';
  }
  stopResize() { if (this.isResizing) { this.isResizing = false; this.savePosition(); } }

  savePosition() {
    chrome.storage.local.set({ windowPosition: {
      left: this.element.style.left, top: this.element.style.top,
      width: this.element.style.width, height: this.element.style.height
    }});
  }
  toggleMinimize() {
    const minimized = this.element.classList.toggle('minimized');
    const btn = this.element.querySelector('.minimize-btn');
    if (btn) {
      btn.setAttribute('aria-expanded', String(!minimized));
      btn.setAttribute('aria-label', minimized ? 'Restore subtitles' : 'Minimize subtitles');
      btn.title = minimized ? 'Restore' : 'Minimize';
    }
  }
  close() {
    if (this._onMouseMove) document.removeEventListener('mousemove', this._onMouseMove);
    if (this._onMouseUp) document.removeEventListener('mouseup', this._onMouseUp);
    if (this._onStorageChanged) chrome.storage.onChanged.removeListener(this._onStorageChanged);
    this._onMouseMove = this._onMouseUp = this._onStorageChanged = null;
    if (this._onFullscreenChange) {
      document.removeEventListener('fullscreenchange', this._onFullscreenChange);
      document.removeEventListener('webkitfullscreenchange', this._onFullscreenChange);
      this._onFullscreenChange = null;
    }
    if (this.host?.parentNode) this.host.remove();
    window.dispatchEvent(new CustomEvent('subtitle-window-closed'));
  }
  destroy() { this.close(); }
}

if (typeof window !== 'undefined') { window.SubtitleWindow = SubtitleWindow; }
