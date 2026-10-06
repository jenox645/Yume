// ============================================================================
// SUBTITLE WINDOW
// Draggable/resizable overlay, progress badge, ready toast, per-line fonts.
// Lives in a Shadow DOM: its CSS used to be injected into EVERY page, where
// generic selectors like .close-btn or .resize-handle restyled the sites' own
// elements (and site CSS leaked into the overlay).
// ============================================================================

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

    this.element.innerHTML = `
      <div class="subtitle-header">
        <div class="subtitle-title"></div>
        <div class="subtitle-controls">
          <div class="chunk-badge" style="display:none" title="Progress"></div>
          <button class="control-btn minimize-btn" title="Minimize">\u2212</button>
          <button class="control-btn close-btn" title="Close">\u00d7</button>
        </div>
      </div>
      <div class="subtitle-content">
        <div class="subtitle-status">Initializing...</div>
        <div class="subtitle-original" style="display:none"></div>
        <div class="subtitle-romaji" style="display:none"></div>
        <div class="subtitle-english" style="display:none"></div>
      </div>
      <div class="ready-toast" style="display:none">Ready \u2713</div>
      <div class="resize-handle"></div>
    `;

    // Set title via textContent (not innerHTML) to prevent XSS
    this.element.querySelector('.subtitle-title').textContent =
      this.settings.windowTitle || 'Yume';

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

    // Background
    const bgColor = s.windowBgColor || '#08080c';
    const opacity = (s.windowOpacity ?? 88) / 100;
    const r = parseInt(bgColor.slice(1,3), 16) || 8;
    const g = parseInt(bgColor.slice(3,5), 16) || 8;
    const b = parseInt(bgColor.slice(5,7), 16) || 12;
    this.element.style.background = `rgba(${r}, ${g}, ${b}, ${opacity})`;

    if (s.windowShadow === false) this.element.style.boxShadow = 'none';
    else this.element.style.boxShadow = '';

    // Corner radius — always applied (independent of glass effect).
    // Default 20 matches the popup slider's default.
    const radius = s.glassRadius ?? 20;
    this.element.style.borderRadius = radius > 0 ? `${radius}px` : '';

    // Glass effect — transparent blur behind subtitles
    if (s.glassEnabled) {
      const blur = s.glassBlur ?? 18;
      this.element.style.backdropFilter = `blur(${blur}px) saturate(1.3)`;
      this.element.style.webkitBackdropFilter = `blur(${blur}px) saturate(1.3)`;
      this.element.style.border = '1px solid rgba(255, 255, 255, 0.15)';
      // Make background more transparent so glass shows through
      this.element.style.background = `rgba(${r}, ${g}, ${b}, ${Math.min(opacity, 0.45)})`;
    } else {
      this.element.style.backdropFilter = 'none';
      this.element.style.webkitBackdropFilter = 'none';
      this.element.style.border = '';
    }

    // Strip characters that could malform CSS font-family strings
    const sanitizeFontName = (name) => name ? name.replace(/["'\\;]/g, '') : '';

    // Per-line: Original text — uses originalFont if set
    const origEl = this.element.querySelector('.subtitle-original');
    if (origEl) {
      origEl.style.fontSize = `${s.originalFontSize || 19}px`;
      origEl.style.color = s.originalColor || '#ffffff';
      origEl.style.fontFamily = s.originalFont
        ? `"${sanitizeFontName(s.originalFont)}", -apple-system, sans-serif`
        : '';
    }

    // Per-line: Romaji — uses romajiFont (Latin alphabet)
    const rmEl = this.element.querySelector('.subtitle-romaji');
    if (rmEl) {
      rmEl.style.fontSize = `${s.romajiFontSize || 13}px`;
      rmEl.style.color = s.romajiColor || '#ffc88c';
      rmEl.style.fontFamily = s.romajiFont
        ? `"${sanitizeFontName(s.romajiFont)}", -apple-system, sans-serif`
        : '';
    }

    // Per-line: Translation — uses translationFont (only for Latin targets)
    const enEl = this.element.querySelector('.subtitle-english');
    if (enEl) {
      enEl.style.fontSize = `${s.translationFontSize || 14}px`;
      enEl.style.color = s.translationColor || '#b4beff';
      enEl.style.fontFamily = s.translationFont
        ? `"${sanitizeFontName(s.translationFont)}", -apple-system, sans-serif`
        : '';
    }

    // Chunk badge visibility
    const badge = this.element?.querySelector('.chunk-badge');
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
        const titleEl = this.element?.querySelector('.subtitle-title');
        if (titleEl) titleEl.textContent = this.settings.windowTitle || 'Yume';

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
          setTimeout(() => { badge.style.display = 'none'; }, 600);
        }
      }, 3000);
      return;
    }
    // Say what is being counted: it switches from audio sections to translated
    // lines once Whisper is done, and translations arrive a batch at a time
    if (!total) {
      const phase = { downloading: ['Downloading\u2026', 'Downloading audio'],
        separating: ['Separating\u2026', 'Separating the vocals from the music'] }[status];
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
    const originalEl = this.element.querySelector('.subtitle-original');
    const englishEl  = this.element.querySelector('.subtitle-english');
    const romajiEl   = this.element.querySelector('.subtitle-romaji');

    if (!original && !english) {
      if (originalEl) { originalEl.textContent = ''; originalEl.style.display = 'none'; }
      if (englishEl)  { englishEl.textContent  = ''; englishEl.style.display  = 'none'; }
      if (romajiEl)   { romajiEl.textContent   = ''; romajiEl.style.display   = 'none'; }
      this.currentOriginal = ''; this.currentEnglish = ''; this.currentRomaji = '';
      return;
    }

    this.isPlaying = true;
    this.currentOriginal = original || '';
    this.currentEnglish  = english || '';
    this.currentRomaji   = romaji || '';
    this.currentConfidence = confidence;

    const statusEl = this.element.querySelector('.subtitle-status');
    if (statusEl) statusEl.style.display = 'none';

    const showJp = this.settings.showOriginal !== false;
    // RTL per line: the original is Arabic when the SOURCE is, the translation
    // when the TARGET is (romanization is always Latin, LTR).
    const srcRTL = this.settings.sourceLanguage === 'ar';
    const tgtRTL = this.settings.targetLanguage === 'Arabic';
    // Align all subtitle lines consistently (center for LTR, right for RTL source)
    const align = srcRTL ? 'right' : 'center';
    if (originalEl) { originalEl.style.direction = srcRTL ? 'rtl' : 'ltr'; originalEl.style.textAlign = align; }
    if (englishEl)  { englishEl.style.direction = tgtRTL ? 'rtl' : 'ltr'; englishEl.style.textAlign = align; }
    if (romajiEl)   romajiEl.style.textAlign = align;

    const showEn = this.settings.showEnglish !== false;
    const showRm = this.settings.showRomaji === true;

    if (originalEl) {
      originalEl.textContent = original || '';
      originalEl.style.display = (original && showJp) ? 'block' : 'none';
      // Confidence indicator: subtle opacity on translation line (opt-in)
      // avg_logprob: > -0.3 high, -0.3 to -0.7 medium, < -0.7 low
      if (englishEl && this.settings.showConfidence && confidence != null && confidence < 0) {
        let opacity;
        if (confidence > -0.3) opacity = 1.0;
        else if (confidence > -0.7) opacity = 0.85;
        else opacity = 0.65;
        englishEl.style.opacity = opacity;
      } else if (englishEl) {
        englishEl.style.opacity = 1.0;
      }
    }
    if (romajiEl)   { romajiEl.textContent = romaji || '';     romajiEl.style.display = (romaji && showRm) ? 'block' : 'none'; }
    if (englishEl)  { englishEl.textContent = english || '';    englishEl.style.display = (english && showEn) ? 'block' : 'none'; }
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
  toggleMinimize() { this.element.classList.toggle('minimized'); }
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
