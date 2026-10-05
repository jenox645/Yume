// ============================================================================
// SUBTITLE SESSION
//
// Asks the local Yume server for a subtitle job for this video, polls it, and
// shows the cue under the playhead. The server does all the work (download,
// Whisper, translation, romanization, caching); this file only renders.
//
// Events dispatched on window (consumed by content.js → SubtitleWindow):
//   display-subtitle {original, english, romaji, confidence}
//   display-status   {message, type}
//   display-error    {message}
//   chunk-progress   {done, total, complete, lines, translated, status}
//   prefetch-ready   — first visible line available
//   pipeline-reset   — a new run started
// ============================================================================

const POLL_ACTIVE_MS = 1000;   // while transcribing/translating
const POLL_IDLE_MS = 4000;     // job done: only blacklist edits can still change lines
const POLL_HIDDEN_MS = 5000;   // tab in background
const HEALTH_WAIT_S = 120;     // how long to wait for the Whisper model to load
const START_WAIT_S = 900;      // starting Yume ourselves (first run downloads the model)
const START_GRACE_S = 15;      // the helper may not have written its state yet
const NOT_RUNNING = 'Yume is not running — start it with START_YUME (or turn on one-click start: ' +
  '"python pocket_yume.py autostart on")';

// eslint-disable-next-line no-redeclare
class SubtitleSession {
  constructor() {
    this.active = false;
    this.video = null;
    this.videoId = null;
    this.jobId = null;
    this.rev = 0;
    this.status = 'idle';
    this.error = '';
    this.progress = null;
    this.segments = new Map();  // id -> segment
    this.cues = [];             // visible segments sorted by start
    this.current = null;        // cue on screen
    this.generation = 0;        // bumped on stop: stale async work checks it
    this.timingOffset = 0;      // seconds (+ = later)
    this.params = null;
    this._pollTimer = null;
    this._readySignaled = false;
    this._unreachable = 0;  // consecutive polls that got no answer

    this._onTime = () => this._render();
    this._onStorage = (changes) => {
      if (changes.settings) {
        const s = changes.settings.newValue || {};
        this.timingOffset = (s.timingOffset || 0) / 10;  // stored as tenths of a second
        this._render(true);
      }
    };
  }

  // ── lifecycle ──────────────────────────────────────────────────────────

  async start(video) {
    const gen = ++this.generation;
    const aborted = () => gen !== this.generation;
    this.video = video;
    this.active = true;
    this._readySignaled = false;
    window.dispatchEvent(new CustomEvent('pipeline-reset'));

    const { settings = {}, customStreamUrl } = await chrome.storage.local.get(['settings', 'customStreamUrl']);
    this.timingOffset = (settings.timingOffset || 0) / 10;
    chrome.storage.onChanged.addListener(this._onStorage);

    await this._waitForServer(aborted);
    if (aborted()) return false;

    this.videoId = SubtitleSession.videoId(video);
    this.params = {
      url: window.location.href,
      video_id: this.videoId,
      stream_url: customStreamUrl || undefined,
      language: settings.sourceLanguage && settings.sourceLanguage !== 'auto' ? settings.sourceLanguage : '',
      target: settings.showEnglish === false ? '' : (settings.targetLanguage || 'English'),
      romanize: settings.showRomaji === true,
      duration: Number.isFinite(video.duration) ? video.duration : 0,
    };
    this._status('Starting...', 'loading');
    await this._createJob();
    if (aborted()) return false;
    // A custom stream URL is one-shot: the next video uses its own page URL
    if (customStreamUrl) chrome.storage.local.remove('customStreamUrl');

    video.addEventListener('timeupdate', this._onTime);
    video.addEventListener('seeked', this._onTime);
    this._schedulePoll(0, gen);
    return true;
  }

  stop() {
    this.generation++;
    this.active = false;
    clearTimeout(this._pollTimer);
    if (this.video) {
      this.video.removeEventListener('timeupdate', this._onTime);
      this.video.removeEventListener('seeked', this._onTime);
    }
    chrome.storage.onChanged.removeListener(this._onStorage);
    this.segments.clear();
    this.cues = [];
    this.current = null;
    this.jobId = null;
    this.rev = 0;
  }

  // Poll now instead of at the next interval (a finished job polls every 4 s):
  // blacklist edits from the popup should show at once.
  refresh() {
    if (this.active && this.jobId) this._schedulePoll(0, this.generation);
  }

  async setRomanize(on) {
    if (this.params) this.params.romanize = on;
    if (this.jobId) await SubtitleSession.api({ method: 'POST', path: `/jobs/${this.jobId}/options`, body: { romanize: on } });
  }

  // ── server ─────────────────────────────────────────────────────────────

  static api(request) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'API', request }, (resp) => {
        resolve(chrome.runtime.lastError ? { ok: false, status: 0, data: { error: chrome.runtime.lastError.message } } : resp);
      });
    });
  }

  static yume(cmd) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'YUME', cmd }, (resp) => {
        resolve(chrome.runtime.lastError || !resp ? { ok: false, unavailable: true } : resp);
      });
    });
  }

  // Wait until the server is ready. When it is not running at all, start it
  // through the native helper (one-click start) and wait for it to come up.
  async _waitForServer(aborted) {
    this._status('Checking server...', 'loading');
    const t0 = Date.now();
    let started = false;  // we asked the helper to start Yume
    let startedAt = 0;
    while (!aborted()) {
      const r = await SubtitleSession.api({ path: '/health', timeout: 5000 });
      const st = r.data?.status;
      if (r.ok && st === 'ready') return;
      if (st === 'error') throw new Error(`Whisper could not load its model: ${r.data.error || 'unknown error'}`);
      const waited = Math.round((Date.now() - (started ? startedAt : t0)) / 1000);
      if (st === 'loading') {
        if (waited > (started ? START_WAIT_S : HEALTH_WAIT_S)) throw new Error('Model loading timed out — restart Yume');
        this._status(`Loading Whisper model... (${waited}s)`, 'loading');
      } else if (r.status !== 0) {
        throw new Error(r.data?.error || `Server error ${r.status}`);
      } else if (!started) {
        this._status('Starting Yume...', 'loading');
        const s = await SubtitleSession.yume('start');
        if (aborted()) return;
        if (!s.ok) throw new Error(s.unavailable ? NOT_RUNNING : s.error || 'Yume could not start');
        started = true;
        startedAt = Date.now();
      } else {
        const s = await SubtitleSession.yume('status');
        if (aborted()) return;
        if (s.ok && s.state === 'stopped' && waited > START_GRACE_S) {
          throw new Error(s.last_error ? `Yume could not start: ${s.last_error}` : 'Yume stopped while starting — see logs/service.log');
        }
        if (waited > START_WAIT_S) throw new Error('Yume is taking too long to start — see logs/service.log');
        this._status(`${(s.message || 'Starting Yume…').replace(/…$/, '')}... (${waited}s)`, 'loading');
      }
      await new Promise((res) => setTimeout(res, 2000));
    }
  }

  async _createJob() {
    const r = await SubtitleSession.api({ method: 'POST', path: '/jobs', body: this.params, timeout: 20000 });
    if (!r.ok) throw new Error(r.data?.error || `Server error ${r.status}`);
    this.jobId = r.data.id;
    this.rev = 0;
    // A recreated job (after a 404) starts from scratch: drop the old cues even
    // if the new snapshot is empty, or stale lines would stay on screen.
    this.segments.clear();
    this.cues = [];
    this._apply(r.data);
    this._render(true);
  }

  _schedulePoll(delay, gen) {
    clearTimeout(this._pollTimer);
    this._pollTimer = setTimeout(() => this._poll(gen), delay);
  }

  async _poll(gen) {
    if (gen !== this.generation || !this.jobId) return;
    const t = this.video ? this.video.currentTime : 0;
    const r = await SubtitleSession.api({
      path: `/jobs/${this.jobId}`, query: { since: String(this.rev), t: t.toFixed(1) }, timeout: 10000,
    });
    if (gen !== this.generation) return;
    if (r.status === 404) {
      // Server restarted, cache cleared or Whisper model switched: start over
      // (the server reloads whatever it already has cached).
      try { await this._createJob(); } catch (e) { return this._fail(e.message); }
      this._unreachable = 0;
    } else if (r.ok) {
      this._unreachable = 0;
      this._apply(r.data);
      if (this.status === 'error') return this._fail(this.error);
    } else if (r.status === 0 && ++this._unreachable === 3) {
      // Keep polling: a restarted server answers 404 and the job is recreated
      this._status('Lost contact with the Yume server — retrying...', 'loading');
    }
    const delay = document.hidden ? POLL_HIDDEN_MS
      : (this.status === 'done' || !r.ok) ? POLL_IDLE_MS : POLL_ACTIVE_MS;
    this._schedulePoll(delay, gen);
  }

  _apply(snap) {
    this.status = snap.status;
    this.error = snap.error || '';
    this.progress = snap.progress;
    let changed = false;
    for (const seg of snap.segments || []) {
      if (seg.hidden) this.segments.delete(seg.id);
      else this.segments.set(seg.id, seg);
      changed = true;
    }
    this.rev = Math.max(this.rev, snap.rev || 0);
    if (changed) {
      this.cues = [...this.segments.values()].sort((a, b) => a.start - b.start);
      if (!this._readySignaled && this.cues.length) {
        this._readySignaled = true;
        window.dispatchEvent(new CustomEvent('prefetch-ready'));
      }
      this._render(true);
    }
    const p = snap.progress || {};
    window.dispatchEvent(new CustomEvent('chunk-progress', {
      detail: {
        done: p.regions_done || 0, total: p.regions_total || 0, complete: snap.status === 'done',
        lines: p.lines || 0, translated: p.translated || 0, status: snap.status,
      },
    }));
    if (!this.cues.length) {
      const allFailed = snap.status === 'done' && p.regions_total > 0 && p.regions_failed === p.regions_total;
      const msg = allFailed
        ? 'Transcription failed — see Diagnostics in the popup; press Enable again to retry'
        : {
          starting: 'Starting...', downloading: 'Downloading audio...',
          transcribing: 'Transcribing...', translating: 'Translating...',
          done: 'No vocals detected in this video',
        }[snap.status];
      if (msg) this._status(msg, allFailed ? 'error' : snap.status === 'done' ? 'info' : 'loading');
    }
  }

  _fail(message) {
    this.status = 'error';
    window.dispatchEvent(new CustomEvent('display-error', { detail: { message } }));
  }

  _status(message, type) {
    window.dispatchEvent(new CustomEvent('display-status', { detail: { message, type } }));
  }

  // ── rendering ──────────────────────────────────────────────────────────

  // Last cue that started at or before t (cues are sorted by start).
  static cueAt(cues, t) {
    let lo = 0, hi = cues.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best >= 0 ? cues[best] : null;
  }

  _render(force = false) {
    if (!this.video || !this.active) return;
    // + = later: a +1 s offset shows each line 1 s after Whisper's timestamp,
    // so the lookup happens 1 s back in the track
    const t = this.video.currentTime - this.timingOffset;
    const cue = SubtitleSession.cueAt(this.cues, t);
    // Show a cue only within its own span; the newest started cue wins, so a
    // long Whisper segment is cut off when the next line starts.
    let show = cue && t < cue.end ? cue : null;
    // Short grace after a line ends so it stays readable — only while playback
    // is still inside/just after it (not after a seek away) and it still exists.
    const prev = this.current && this.segments.get(this.current.id);
    if (!show && prev && t >= prev.start && t < prev.end + 1.0) show = prev;
    if (!force && show === this.current) return;
    this.current = show;
    window.dispatchEvent(new CustomEvent('display-subtitle', {
      detail: show
        ? { original: show.text, english: show.translation, romaji: show.romaji, confidence: show.confidence }
        : { original: '', english: '', romaji: '' },
    }));
  }

  // ── popup helpers ──────────────────────────────────────────────────────

  getCurrentSubtitle() {
    const c = this.current;
    return c ? { original: c.text, english: c.translation, romaji: c.romaji, start: c.start, end: c.end } : null;
  }

  async exportSubtitles(format) {
    if (!this.jobId) return { success: false, error: 'No active session' };
    const r = await SubtitleSession.api({ path: `/jobs/${this.jobId}/export`, query: { format }, timeout: 15000 });
    return r.ok ? { success: true, ...r.data } : { success: false, error: r.data?.error || 'Export failed' };
  }

  async getDiagnostics() {
    let events = [];
    if (this.jobId) {
      const r = await SubtitleSession.api({ path: `/jobs/${this.jobId}`, query: { since: String(this.rev), events: '1' } });
      if (r.ok) { events = r.data.events || []; this._apply(r.data); }
    }
    return {
      active: this.active, videoId: this.videoId, jobId: this.jobId, status: this.status, error: this.error,
      progress: this.progress, lines: this.cues.length, events,
    };
  }

  // Stable across reloads and identical for the same video on every visit.
  // YouTube: the video id (only on YouTube hosts — other sites use ?v= for
  // unrelated things). Elsewhere: the PAGE identity — host + path + the query
  // parameters that select the content (tracking/position ones dropped) — plus
  // the duration. Never the media src: CDN URLs carry per-load tokens and
  // blob:/MediaSource URLs change on every load, which made every visit
  // re-download and re-transcribe.
  static videoId(video) {
    const loc = window.location;
    const host = loc.hostname.replace(/^(www|m|music)\./, '');
    const params = new URLSearchParams(loc.search);
    if (host === 'youtube.com' || host === 'youtube-nocookie.com') {
      if (params.get('v')) return params.get('v');
    }
    for (const k of [...params.keys()]) {
      if (/^(utm_.*|t|time|start|ref|si|feature|fbclid|gclid|list|index|pp)$/.test(k)) params.delete(k);
    }
    params.sort();
    const query = params.toString();
    const v = video || document.querySelector('video');
    const dur = Number.isFinite(v?.duration) ? Math.round(v.duration) : '';
    return `${loc.hostname}${loc.pathname}${query ? '?' + query : ''}#${dur}`.slice(0, 200);
  }
}

if (typeof window !== 'undefined') { window.SubtitleSession = SubtitleSession; }
