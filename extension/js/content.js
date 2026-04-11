// ============================================================================
// CONTENT SCRIPT v0.0.8 - Main orchestrator
// ============================================================================

(async function() {
  'use strict';

  DEBUG.info('ContentScript', 'Extension initializing on page', {
    url: window.location.href,
    readyState: document.readyState
  });

  // ========================================================================
  // STATE
  // ========================================================================

  let state = {
    isActive: false,
    videoElement: null,
    videoId: null,
    subtitleWindow: null,
    audioCapture: null,
    settings: {}
  };

  // ========================================================================
  // INITIALIZATION
  // ========================================================================

  async function initialize() {
    try {
      await loadSettings();

      state.audioCapture = new AudioCapture();
      if (state.settings.chunkDuration) {
        state.audioCapture.setChunkDuration(state.settings.chunkDuration);
      }

      state.videoElement = await waitForVideo();

      if (state.videoElement) {
        state.videoId = getVideoId();
        setupEventListeners();
        startUrlWatcher();
        DEBUG.success('ContentScript', 'Initialization complete');
      }
    } catch (error) {
      DEBUG.error('ContentScript', 'Initialization failed', { error: error.message });
    }
  }

  // ========================================================================
  // VIDEO DETECTION
  // ========================================================================

  function waitForVideo(maxAttempts = 20) {
    return new Promise((resolve) => {
      let attempts = 0;
      const interval = setInterval(() => {
        attempts++;
        const video = findVideoElement();
        if (video) { clearInterval(interval); resolve(video); }
        else if (attempts >= maxAttempts) { clearInterval(interval); resolve(null); }
      }, 500);
    });
  }

  function findVideoElement() {
    const selectors = ['video', '.video-player video', '#player video', '[class*="player"] video'];
    for (const sel of selectors) {
      const video = document.querySelector(sel);
      if (video && video.readyState >= 2) return video;
    }
    return null;
  }

  function getVideoId() {
    const url = window.location.href;
    if (url.includes('youtube.com') || url.includes('youtu.be')) {
      const urlParams = new URLSearchParams(window.location.search);
      return urlParams.get('v') || 'youtube-' + Date.now();
    }
    return window.location.pathname.replace(/[^a-zA-Z0-9]/g, '-') + '-' + Date.now();
  }

  // ========================================================================
  // START SUBTITLES
  // ========================================================================

  async function startSubtitles() {
    try {
      state.subtitleWindow = new SubtitleWindow();
      state.subtitleWindow.updateStatus('Starting...', 'loading');

      await state.audioCapture.startCapture();

      state.isActive = true;
      // Status managed by prefetch-ready event
    } catch (error) {
      if (state.subtitleWindow) state.subtitleWindow.showError('Error: ' + error.message);
      throw error;
    }
  }

  // ========================================================================
  // STOP SUBTITLES
  // ========================================================================

  async function stopSubtitles() {
    if (state.audioCapture) state.audioCapture.stopCapture();
    if (state.subtitleWindow) { state.subtitleWindow.close(); state.subtitleWindow = null; }
    state.isActive = false;
  }

  // ========================================================================
  // URL CHANGE DETECTION (YouTube SPA)
  // ========================================================================

  let lastUrl = window.location.href;

  function startUrlWatcher() {
    window.addEventListener('yt-navigate-finish', onUrlChange);
    setInterval(() => {
      if (window.location.href !== lastUrl) onUrlChange();
    }, 1000);

    const origPushState = history.pushState;
    history.pushState = function() {
      origPushState.apply(this, arguments);
      setTimeout(onUrlChange, 100);
    };
    const origReplaceState = history.replaceState;
    history.replaceState = function() {
      origReplaceState.apply(this, arguments);
      setTimeout(onUrlChange, 100);
    };
    window.addEventListener('popstate', () => setTimeout(onUrlChange, 100));
  }

  function onUrlChange() {
    const newUrl = window.location.href;
    if (newUrl === lastUrl) return;

    const oldV = new URL(lastUrl, window.location.origin).searchParams.get('v');
    const newV = new URL(newUrl, window.location.origin).searchParams.get('v');
    lastUrl = newUrl;

    if (oldV && newV && oldV === newV) return;
    if (!state.isActive) return;

    restartForNewVideo();
  }

  async function restartForNewVideo() {
    DEBUG.info('ContentScript', 'Restarting for new video');
    if (state.audioCapture) state.audioCapture.stopCapture();

    await new Promise(r => setTimeout(r, 1500));

    const newVideo = await waitForVideo(10);
    if (!newVideo) {
      if (state.subtitleWindow) state.subtitleWindow.showError('No video found');
      return;
    }
    state.videoElement = newVideo;
    state.videoId = getVideoId();

    if (state.subtitleWindow) {
      state.subtitleWindow.isPlaying = false;
      state.subtitleWindow.updateStatus('Loading subtitles...', 'loading');
    }

    await loadSettings();

    state.audioCapture = new AudioCapture();
    if (state.settings.chunkDuration) {
      state.audioCapture.setChunkDuration(state.settings.chunkDuration);
    }

    try {
      await state.audioCapture.startCapture();
    } catch (error) {
      if (state.subtitleWindow) state.subtitleWindow.showError('Restart failed: ' + error.message);
    }
  }

  // ========================================================================
  // RESTART FOR SETTINGS CHANGE
  // ========================================================================

  async function restartPipelineForSettingsChange(newSettings) {
    if (!state.isActive || !state.audioCapture) return;

    state.audioCapture.stopCapture();

    if (state.subtitleWindow) {
      state.subtitleWindow.isPlaying = false;
      state.subtitleWindow.updateStatus('Reloading with new settings...', 'loading');
    }

    state.settings = { ...state.settings, ...newSettings };
    await new Promise(r => setTimeout(r, 300));

    state.audioCapture = new AudioCapture();
    if (state.settings.chunkDuration) {
      state.audioCapture.setChunkDuration(state.settings.chunkDuration);
    }

    try {
      await state.audioCapture.startCapture();
    } catch (error) {
      if (state.subtitleWindow) state.subtitleWindow.showError('Restart failed: ' + error.message);
    }
  }

  // Re-romanize cached chunks without full restart
  async function reRomanizeCachedChunks() {
    if (!state.audioCapture || !state.isActive) return;
    if (state.subtitleWindow) {
      state.subtitleWindow.updateStatus('Adding romanization...', 'loading');
    }
    await state.audioCapture.reRomanizeCachedChunks();
    if (state.subtitleWindow) {
      state.subtitleWindow.isPlaying = true;
    }
  }

  // ========================================================================
  // EVENT LISTENERS
  // ========================================================================

  function setupEventListeners() {
    window.addEventListener('capture-buffering', (e) => {
      if (state.subtitleWindow) state.subtitleWindow.showBuffering(e.detail.chunkIndex, e.detail.totalChunks);
    });

    window.addEventListener('prefetch-ready', () => {
      if (state.subtitleWindow) state.subtitleWindow.showReady();
    });

    window.addEventListener('display-subtitle', (e) => {
      if (state.subtitleWindow) {
        state.subtitleWindow.updateSubtitle(
          e.detail.original, e.detail.english, e.detail.romaji, e.detail.confidence
        );
      }
    });

    window.addEventListener('display-error', (e) => {
      if (state.subtitleWindow) state.subtitleWindow.showError(e.detail.message);
    });

    window.addEventListener('display-status', (e) => {
      if (state.subtitleWindow) state.subtitleWindow.updateStatus(e.detail.message, e.detail.type);
    });

    window.addEventListener('subtitle-window-closed', () => { stopSubtitles(); });

    // Chunk progress
    window.addEventListener('chunk-progress', (e) => {
      if (state.subtitleWindow) {
        state.subtitleWindow.updateChunkProgress(e.detail);
      }
    });

    // Settings changes — smart handler
    chrome.storage.onChanged.addListener((changes) => {
      if (!changes.settings || !state.isActive) return;
      const oldS = changes.settings.oldValue || {};
      const newS = changes.settings.newValue || {};

      const langChanged =
        oldS.sourceLanguage !== newS.sourceLanguage ||
        oldS.targetLanguage !== newS.targetLanguage;

      const romajiToggled = oldS.showRomaji !== newS.showRomaji;

      if (langChanged) {
        // Language change → full pipeline restart
        restartPipelineForSettingsChange(newS);
      } else if (romajiToggled && newS.showRomaji) {
        // Romaji toggled ON → re-romanize cached chunks (no restart)
        reRomanizeCachedChunks();
      }
      // Romaji OFF → subtitle-window hides the line (its own onChanged listener)
      // Appearance changes → subtitle-window handles via its own listener
    });

    DEBUG.success('ContentScript', 'Event listeners attached');
  }

  // ========================================================================
  // SETTINGS
  // ========================================================================

  async function loadSettings() {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'GET_SETTINGS' }, (response) => {
        state.settings = response?.settings || {};
        resolve();
      });
    });
  }

  // ========================================================================
  // MESSAGE HANDLER (from popup)
  // ========================================================================

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.action === 'TOGGLE_SUBTITLES') {
      (async () => {
        try {
          if (state.isActive) {
            await stopSubtitles();
            sendResponse({ success: true, active: false });
          } else {
            await startSubtitles();
            sendResponse({ success: true, active: true });
          }
        } catch (error) {
          sendResponse({ success: false, error: error.message });
        }
      })();
      return true;
    }

    if (message.action === 'GET_STATUS') {
      sendResponse({ success: true, active: state.isActive, hasVideo: !!state.videoElement });
      return false;
    }

    if (message.action === 'GET_DIAGNOSTICS') {
      const diag = state.audioCapture ? state.audioCapture.getDiagnostics() : null;
      sendResponse({ success: true, active: state.isActive, diagnostics: diag });
      return false;
    }

    if (message.action === 'GET_CURRENT_SUBTITLE') {
      let text = null;
      if (state.subtitleWindow) text = state.subtitleWindow.getCurrentText();
      if ((!text || !text.original) && state.audioCapture) {
        text = state.audioCapture.getCurrentSubtitle();
      }
      sendResponse({ success: true, subtitle: text });
      return false;
    }

    if (message.action === 'EXPORT_SRT') {
      if (!state.audioCapture || !state.isActive) {
        sendResponse({ success: false, error: 'No active session' });
        return false;
      }
      const result = state.audioCapture.exportSRT();
      sendResponse({ success: true, ...result });
      return false;
    }

    if (message.action === 'CLEAR_CONTENT_CACHE') {
      const cleared = state.audioCapture ? state.audioCapture.clearContentCache() : 0;
      sendResponse({ success: true, cleared });
      return false;
    }

    if (message.action === 'UPDATE_GLASS') {
      // Live glass update from popup slider — apply to subtitle window immediately
      if (state.subtitleWindow?.element) {
        const s = state.subtitleWindow.settings || {};
        s.glassEnabled = message.glassEnabled;
        s.glassBlur = message.glassBlur;
        s.glassRadius = message.glassRadius;
        state.subtitleWindow.settings = s;
        state.subtitleWindow.applyCustomStyles();
      }
      sendResponse({ success: true });
      return false;
    }
  });

  // ========================================================================
  // STARTUP
  // ========================================================================

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize);
  } else {
    initialize();
  }
})();
