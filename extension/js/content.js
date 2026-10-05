// ============================================================================
// CONTENT SCRIPT
// Page lifecycle: find the video, start/stop the subtitle session, follow SPA
// navigation, relay popup requests.
// ============================================================================

(function () {
  'use strict';

  const state = {
    active: false,
    starting: false,
    video: null,
    session: null,
    window: null,
  };

  // ── video detection ──────────────────────────────────────────────────────

  // The main video: the largest one that is visible and has data. A bare
  // querySelector('video') picked whatever came first — on YouTube's home page
  // that is a hover preview, on many sites an ad.
  function findVideo() {
    let best = null;
    let bestArea = 0;
    for (const v of document.querySelectorAll('video')) {
      if (v.readyState < 1) continue;
      const r = v.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > bestArea) { best = v; bestArea = area; }
    }
    return best;
  }

  function waitForVideo(attempts = 20) {
    return new Promise((resolve) => {
      const tick = () => {
        const v = findVideo();
        if (v || --attempts <= 0) resolve(v);
        else setTimeout(tick, 500);
      };
      tick();
    });
  }

  // ── start / stop ─────────────────────────────────────────────────────────

  // Resolves once the video is found and the session is starting (the popup
  // answers right away); `done` settles when the session is running or failed.
  // Errors after that point are shown in the subtitle window.
  // Every start() takes a number; stop() and newer starts invalidate older
  // ones. Without it, two starts overlapping across the video wait (Enable
  // pressed twice, two quick navigations) each created a session and the
  // first one kept running, and a stop() during the wait was undone.
  let startSeq = 0;

  async function start() {
    const seq = ++startSeq;
    state.active = true;  // a toggle during the wait below means "stop"
    const video = (state.video && document.contains(state.video)) ? state.video : await waitForVideo(6);
    if (seq !== startSeq) return { done: Promise.resolve() };  // stopped or superseded meanwhile
    if (!video) {
      state.active = false;
      throw new Error('No video found on this page — start playing a video, then try again.');
    }
    state.video = video;
    if (!state.window) state.window = new SubtitleWindow();
    state.window.resetProgress();
    state.window.updateStatus('Starting...', 'loading');
    if (state.session) state.session.stop();
    const session = new SubtitleSession();
    state.session = session;
    state.starting = true;
    const done = session.start(video).catch((e) => {
      session.stop();  // releases its storage listener
      if (state.session !== session) return;  // stopped or restarted meanwhile
      if (state.window) state.window.showError(e.message);
      state.active = false;
    }).finally(() => {
      if (state.session === session) state.starting = false;
    });
    return { done };
  }

  function stop() {
    startSeq++;  // a start() still waiting for the video must not resume
    if (state.session) state.session.stop();
    state.session = null;
    state.active = false;
    // Null BEFORE close(): close() dispatches 'subtitle-window-closed', whose
    // handler calls stop() again — re-entering close() would recurse forever.
    if (state.window) { const w = state.window; state.window = null; w.close(); }
  }

  async function restart(reason) {
    if (!state.active) return;
    if (state.session) state.session.stop();
    if (state.window) {
      state.window.isPlaying = false;
      state.window.updateStatus(reason, 'loading');
    }
    state.video = null;
    try { await start(); } catch (e) {
      if (state.window) state.window.showError(e.message);
      state.active = false;
    }
  }

  // ── SPA navigation ───────────────────────────────────────────────────────
  // Polling catches every navigation. (Wrapping history.pushState from a
  // content script does nothing: it runs in an isolated world, so the page's
  // own pushState calls never go through our copy.)

  let lastUrl = window.location.href;
  function onUrlChange() {
    const url = window.location.href;
    if (url === lastUrl) return;
    const prevId = lastUrl;
    lastUrl = url;
    if (!state.active) return;
    // Same YouTube video, other params (&t=, playlists): nothing to do
    const v = (u) => { try { return new URL(u).searchParams.get('v'); } catch (_e) { return null; } };
    if (v(prevId) && v(prevId) === v(url)) return;
    // Stop now: the old session would otherwise keep drawing the previous
    // video's lines over the new one until the restart below.
    if (state.session) state.session.stop();
    if (state.window) {
      state.window.updateSubtitle('', '', '');
      state.window.updateStatus('Loading subtitles for the new video...', 'loading');
    }
    // Give the page a moment to swap the video source before reading its duration
    setTimeout(() => restart('Loading subtitles for the new video...'), 1500);
  }
  setInterval(onUrlChange, 1000);
  window.addEventListener('popstate', () => setTimeout(onUrlChange, 100));
  window.addEventListener('yt-navigate-start', onUrlChange);
  window.addEventListener('yt-navigate-finish', onUrlChange);
  // A player swapping its source fires "emptied" at once (media events don't
  // bubble, hence the capture phase): catches the switch before the next poll.
  document.addEventListener('emptied', onUrlChange, true);

  // ── session → window events ──────────────────────────────────────────────

  const toWindow = (fn) => (e) => { if (state.window) fn(state.window, e.detail || {}); };
  window.addEventListener('pipeline-reset', toWindow((w) => w.resetProgress()));
  window.addEventListener('prefetch-ready', toWindow((w) => w.showReady()));
  window.addEventListener('display-subtitle', toWindow((w, d) => w.updateSubtitle(d.original, d.english, d.romaji, d.confidence)));
  window.addEventListener('display-error', toWindow((w, d) => w.showError(d.message)));
  window.addEventListener('display-status', toWindow((w, d) => w.updateStatus(d.message, d.type)));
  window.addEventListener('chunk-progress', toWindow((w, d) => w.updateChunkProgress(d)));
  window.addEventListener('subtitle-window-closed', () => stop());

  // Settings that change WHAT the server produces need a new job; display-only
  // settings are applied by SubtitleWindow / SubtitleSession themselves.
  chrome.storage.onChanged.addListener((changes) => {
    if (!changes.settings || !state.active) return;
    const o = changes.settings.oldValue || {};
    const n = changes.settings.newValue || {};
    if (o.sourceLanguage !== n.sourceLanguage || o.targetLanguage !== n.targetLanguage ||
        o.showEnglish !== n.showEnglish || o.whisperUrl !== n.whisperUrl) {
      restart('Reloading with new settings...');
    } else if (o.showRomaji !== n.showRomaji && state.session) {
      state.session.setRomanize(n.showRomaji === true);
    }
  });

  // ── popup / background messages ──────────────────────────────────────────

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    switch (message.action) {
      case 'TOGGLE_SUBTITLES':
        if (state.active) {
          stop();
          sendResponse({ success: true, active: false });
          return false;
        }
        start().then(() => sendResponse({ success: true, active: state.active }),
          (e) => sendResponse({ success: false, error: e.message }));
        return true;
      case 'GET_STATUS':
        sendResponse({ success: true, active: state.active, hasVideo: !!findVideo() });
        return false;
      case 'GET_DIAGNOSTICS':
        if (!state.session) { sendResponse({ success: true, active: false, diagnostics: null }); return false; }
        state.session.getDiagnostics().then((d) => sendResponse({ success: true, active: state.active, diagnostics: d }));
        return true;
      case 'REFRESH_SUBTITLES':
        if (state.session) state.session.refresh();
        sendResponse({ success: true });
        return false;
      case 'GET_CURRENT_SUBTITLE':
        sendResponse({ success: true, subtitle: state.session ? state.session.getCurrentSubtitle() : null });
        return false;
      case 'EXPORT_SRT':
      case 'EXPORT_VTT':
        if (!state.session) { sendResponse({ success: false, error: 'No active session' }); return false; }
        state.session.exportSubtitles(message.action === 'EXPORT_VTT' ? 'vtt' : 'srt').then(sendResponse);
        return true;
      case 'UPDATE_GLASS':
        if (state.window) {
          Object.assign(state.window.settings, {
            glassEnabled: message.glassEnabled, glassBlur: message.glassBlur, glassRadius: message.glassRadius,
          });
          state.window.applyCustomStyles();
        }
        sendResponse({ success: true });
        return false;
      default:
        return false;
    }
  });
})();
