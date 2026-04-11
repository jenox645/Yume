// ============================================================================
// SUBTITLE WINDOW v0.0.9 - Chunk counter, ready toast, per-line fonts
// ============================================================================

// eslint-disable-next-line no-redeclare
class SubtitleWindow {
  constructor() {
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
        if (p.top)    this.element.style.top     = p.top;
        if (p.width)  this.element.style.width   = p.width;
        if (p.height) this.element.style.height  = p.height;
      } else {
        this.element.style.left   = '20px';
        this.element.style.bottom = '80px';
      }
      this.applyCustomStyles();
    });
  }

  create() {
    this.element = document.createElement('div');
    this.element.id = 'ai-subtitle-window';
    this.element.className = 'subtitle-window';

    this.element.innerHTML = `
      <div class="subtitle-header">
        <div class="subtitle-title"></div>
        <div class="subtitle-controls">
          <div class="chunk-badge" style="display:none" title="Pipeline progress"></div>
          <div class="roma-badge" style="display:none" title="Romanization progress"></div>
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

    document.body.appendChild(this.element);
    this._injectBundledFonts();
  }

  _injectBundledFonts() {
    // Inject @font-face for any .ttf/.otf files placed in extension/fonts/
    // Users can add custom fonts by placing files there and listing them in popup.js BUNDLED_FONTS
    try {
      chrome.storage.local.get(['bundledFonts'], (result) => {
        const fonts = result.bundledFonts || [];
        if (!fonts.length) return;
        let css = '';
        for (const f of fonts) {
          const url = chrome.runtime.getURL('fonts/' + f.file);
          css += `@font-face { font-family: "${f.name}"; src: url("${url}"); font-display: swap; }\n`;
        }
        if (css) {
          const style = document.createElement('style');
          style.textContent = css;
          document.head.appendChild(style);
        }
      });
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

    // Corner radius — always applied (independent of glass effect)
    const radius = s.glassRadius ?? 0;
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
    document.addEventListener('mousemove', (e) => this.drag(e));
    document.addEventListener('mouseup', () => this.stopDrag());

    const resizeHandle = this.element.querySelector('.resize-handle');
    resizeHandle.addEventListener('mousedown', (e) => this.startResize(e));
    document.addEventListener('mousemove', (e) => this.resize(e));
    document.addEventListener('mouseup', () => this.stopResize());

    chrome.storage.onChanged.addListener((changes) => {
      if (changes.settings) {
        this.settings = changes.settings.newValue || {};
        this.applyCustomStyles();
        const titleEl = this.element?.querySelector('.subtitle-title');
        if (titleEl) titleEl.textContent = this.settings.windowTitle || 'Yume';

        // Immediately toggle chunk badge visibility on settings change
        const badge = this.element?.querySelector('.chunk-badge');
        if (badge) {
          if (this.settings.showChunkCounter === false) {
            badge.style.display = 'none';
          } else if (this._lastProgress && !this._lastProgress.complete) {
            const showTrans = this.settings.showEnglish !== false;
            const cnt = showTrans ? (this._lastProgress.translated || 0) : this._lastProgress.fetched;
            badge.textContent = `${cnt}/${this._lastProgress.total}`;
            badge.className = 'chunk-badge active';
            badge.style.display = '';
          } else if (badge.classList.contains('active') || badge.classList.contains('complete')) {
            badge.style.display = '';
          }
        }
      }
    });
  }

  // ========================================================================
  // CHUNK PROGRESS BADGE
  // ========================================================================

  updateChunkProgress(detail) {
    if (!this.element) return;
    const { fetched, total, complete, translated, romanized } = detail;
    const badge = this.element.querySelector('.chunk-badge');
    const romaBadge = this.element.querySelector('.roma-badge');
    if (!badge) return;

    this._lastProgress = detail;
    const hidden = this.settings.showChunkCounter === false;

    // Use translated count as main progress when translation is enabled,
    // otherwise use fetched (whisper-only). This ensures the counter only
    // goes up when ALL enabled processing is done for a chunk.
    const showTranslation = this.settings.showEnglish !== false;
    const _mainCount = showTranslation ? (translated || 0) : fetched;
    // Show as complete when all chunks are fetched — empty chunks (instrumental
    // sections with no vocals) are legitimately done, not missing.
    const allDone = complete;

    if (allDone) {
      badge.textContent = '\u2713';
      badge.className = 'chunk-badge complete';
      if (hidden) { badge.style.display = 'none'; }
      else {
        badge.style.display = '';
        setTimeout(() => {
          if (badge.classList.contains('complete')) {
            badge.classList.add('fade-out');
            setTimeout(() => { badge.style.display = 'none'; }, 600);
          }
        }, 3000);
      }
    } else {
      badge.textContent = `${fetched}/${total}`;
      badge.className = 'chunk-badge active';
      badge.style.display = hidden ? 'none' : '';
    }

    // ── Roma badge (warm amber): romanization progress ──
    if (romaBadge) {
      const showRoma = this.settings.showRomaji === true && !hidden;
      const romaCount = romanized || 0;

      if (showRoma && fetched > 0) {
        if (romaCount >= total && complete) {
          romaBadge.textContent = '\u2713';
          romaBadge.className = 'roma-badge complete';
          romaBadge.style.display = '';
          setTimeout(() => {
            if (romaBadge.classList.contains('complete')) {
              romaBadge.classList.add('fade-out');
              setTimeout(() => { romaBadge.style.display = 'none'; }, 600);
            }
          }, 3000);
        } else {
          romaBadge.textContent = `${romaCount}R`;
          romaBadge.className = 'roma-badge active';
          romaBadge.style.display = '';
        }
      } else {
        romaBadge.style.display = 'none';
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

    const statusEl = this.element.querySelector('.subtitle-status');
    if (statusEl) statusEl.style.display = 'none';

    const showJp = this.settings.showOriginal !== false;
    // RTL support for Arabic source language
    const isRTL = this.settings.sourceLanguage === 'ar';
    // Align all subtitle lines consistently (center for LTR, right for RTL)
    const align = isRTL ? 'right' : 'center';
    const dir   = isRTL ? 'rtl' : 'ltr';
    if (originalEl) { originalEl.style.direction = dir; originalEl.style.textAlign = align; }
    if (englishEl)  englishEl.style.textAlign = align;
    if (romajiEl)   romajiEl.style.textAlign = align;

    const showEn = this.settings.showEnglish !== false;
    const showRm = this.settings.showRomaji === true;

    if (originalEl) {
      originalEl.textContent = original || '';
      originalEl.style.display = (original && showJp) ? 'block' : 'none';
      // Confidence indicator: subtle opacity on translation line (opt-in)
      // avg_logprob: > -0.3 high, -0.3 to -0.7 medium, < -0.7 low
      if (englishEl && this.settings.showConfidence && confidence && confidence < 0) {
        const opacity = confidence > -0.3 ? 1.0 : confidence > -0.7 ? 0.85 : 0.65;
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
        setTimeout(() => { if (toast) toast.style.display = 'none'; }, 600);
      }, 2000);
    }
    if (!this.isPlaying) {
      const statusEl = this.element.querySelector('.subtitle-status');
      if (statusEl) { statusEl.textContent = 'Ready \u2713'; statusEl.style.display = 'block'; statusEl.className = 'subtitle-status status-success'; }
    }
  }

  showBuffering() {}

  getCurrentText() {
    return { original: this.currentOriginal, english: this.currentEnglish, romaji: this.currentRomaji };
  }

  // ========================================================================
  // DRAGGING / RESIZING / PERSISTENCE
  // ========================================================================

  startDrag(e) {
    if (e.target.closest('button') || e.target.closest('.chunk-badge') || e.target.closest('.roma-badge')) return;
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
  stopDrag() { if (this.isDragging) { this.isDragging = false; this.element.style.cursor = 'default'; this.savePosition(); } }

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
  close() { if (this.element?.parentNode) this.element.remove(); window.dispatchEvent(new CustomEvent('subtitle-window-closed')); }
  destroy() { this.close(); }
}

if (typeof window !== 'undefined') { window.SubtitleWindow = SubtitleWindow; }
