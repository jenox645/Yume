// ============================================================================
// AUDIO CAPTURE v3.8.0 - Download-once + parallel pipeline
// Translation and romanization are SEPARATE API calls
// ============================================================================

class AudioCapture {
  constructor() {
    DEBUG.functionStart('AudioCapture', 'constructor');

    this.isCapturing    = false;
    this.videoUrl       = null;
    this.videoId        = null;
    this.sourceLanguage = 'ja';

    // Chunk geometry
    this.chunkDuration  = 30;
    this.stepSize       = 25;

    // Cached segments per chunk
    this.subtitleChunks = {};
    this.fetchedChunks  = new Set();
    this.fetchingChunks = new Set();

    // Pipeline control
    this.totalChunks     = 0;
    this.pipelineRunning = false;
    this.priorityChunk   = -1;

    // Generation counter — incremented on stop, used to kill stale promises
    this.generation = 0;

    // Playback tracking
    this.video              = null;
    this.timeUpdateHandler  = null;
    this.lastShownSegment   = null;

    // State
    this.prepared        = false;
    this.videoDuration   = 0;
    this.emptyChunkCount = 0;
    this.maxEmptyChunks  = 8;  // v3.9: 8 (was 4) — many songs have 60-90s instrumental bridges
    this.fetchingStopped = false;
    this.timingOffset    = 0;   // v3.9: subtitle timing offset in seconds (from slider)

    // User-reported hallucination blacklist (loaded from storage)
    this.userBlacklist = [];
    // Server-authoritative built-in patterns (fetched at start, hardcoded fallback)
    this.serverPatterns = null;  // null = not yet fetched
    this._loadUserBlacklist();

    // Listen for blacklist changes — purge matching cached segments immediately
    chrome.storage.onChanged.addListener((changes) => {
      if (changes.hallucinationBlacklist) {
        this.userBlacklist = changes.hallucinationBlacklist.newValue || [];
        DEBUG.info('AudioCapture', `User blacklist updated: ${this.userBlacklist.length} items`);
        this._purgeBlacklistedFromCache();
      }
      if (changes.settings) {
        const s = changes.settings.newValue || {};
        this.timingOffset = (s.timingOffset || 0) / 10;
        // If language changed while pipeline is running, restart
        const newLang = s.sourceLanguage || 'ja';
        if (this.isCapturing && newLang !== this.sourceLanguage) {
          console.log('[Yume] Language changed to', newLang, '— restarting pipeline');
          this.sourceLanguage = newLang;
          this.showRomaji = s.showRomaji || false;
          // Safe: JS is single-threaded, so rapid language changes fire sequentially.
          // Each generation++ causes prior pipelines to abort on their next gen check.
          this.generation++; // abort all in-flight chunks
          this.subtitleChunks = {}; this.fetchedChunks = new Set(); this.fetchingChunks = new Set();
          this.emptyChunkCount = 0; this.fetchingStopped = false;
          this.prepared = false;
          this._runPipeline().catch(e => console.warn('[Yume] Pipeline restart failed:', e.message));
        } else {
          this.sourceLanguage = newLang;
          this.showRomaji = s.showRomaji || false;
        }
      }
    });

    // Diagnostics log
    this.diagLog = [];
    this.maxDiagEntries = 50;

    DEBUG.functionEnd('AudioCapture', 'constructor');
  }

  _loadUserBlacklist() {
    try {
      chrome.storage.local.get(['hallucinationBlacklist'], (result) => {
        this.userBlacklist = result.hallucinationBlacklist || [];
      });
    } catch (e) { this.userBlacklist = []; }
  }

  // =========================================================================
  // START
  // =========================================================================

  async startCapture() {
    DEBUG.functionStart('AudioCapture', 'startCapture');

    const video = document.querySelector('video');
    if (!video) throw new Error('No video element found on page');

    this.video       = video;
    this.videoId     = this._getVideoId();
    this.videoUrl    = window.location.href;
    this.isCapturing = true;

    // Read settings
    try {
      const { settings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
      this.sourceLanguage = settings?.sourceLanguage || 'ja';
      this.showRomaji = settings?.showRomaji || false;
      this.timingOffset = (settings?.timingOffset || 0) / 10;  // stored as ticks, convert to seconds
    } catch (e) { this.sourceLanguage = 'ja'; this.showRomaji = false; this.timingOffset = 0; }

    // Reload user blacklist
    this._loadUserBlacklist();
    // Fetch server-authoritative hallucination patterns (async, non-blocking)
    this._fetchServerPatterns();

    // Pre-start health check — fail fast if server isn't running
    window.dispatchEvent(new CustomEvent('display-status', {
      detail: { message: 'Checking server...', type: 'loading' }
    }));
    try {
      const healthResp = await this._sendMessage({ type: 'CHECK_SERVER',
        url: (await new Promise(r => chrome.storage.local.get(['settings'], r)))?.settings?.whisperUrl + '/health'
          || 'http://localhost:5001/health'
      });
      if (!healthResp || !healthResp.healthy) {
        throw new Error('Whisper server not reachable — is Yume running?');
      }
    } catch (e) {
      if (e.message.includes('not reachable') || e.message.includes('Yume running')) throw e;
      throw new Error('Whisper server not reachable — is Yume running?');
    }

    window.dispatchEvent(new CustomEvent('display-status', {
      detail: { message: 'Downloading audio...', type: 'loading' }
    }));

    await this._prepareVideo();
    DEBUG.success('AudioCapture', `Full audio ready: ${this.videoDuration.toFixed(0)}s`);

    this.totalChunks = Math.ceil(this.videoDuration / this.stepSize);
    this._dispatchProgress();

    // Try restoring subtitles from session (survives page reload)
    const restored = await this._restoreFromSession();
    if (restored && this.fetchedChunks.size >= this.totalChunks) {
      // All chunks already cached — go straight to playback
      DEBUG.success('AudioCapture', 'All chunks restored from session — skipping transcription');
      window.dispatchEvent(new CustomEvent('display-status', {
        detail: { message: 'Restored from cache \u2713', type: 'success' }
      }));
      this._signalReady();
      this.timeUpdateHandler = () => this._onTimeUpdate();
      video.addEventListener('timeupdate', this.timeUpdateHandler);
      DEBUG.success('AudioCapture', 'Session restore complete — playback ready');
      return true;
    }

    const startChunk = this._chunkForTime(video.currentTime);

    // If partially restored, skip already-fetched chunks
    if (restored && this.fetchedChunks.size > 0) {
      DEBUG.info('AudioCapture', `Partial restore: ${this.fetchedChunks.size}/${this.totalChunks} from session`);
    }

    window.dispatchEvent(new CustomEvent('display-status', {
      detail: { message: 'Transcribing...', type: 'loading' }
    }));

    await this._fetchChunk(startChunk);
    // Only signal Ready if the chunk at current playback time has segments
    const currentChunk = this._chunkForTime(this.video?.currentTime || 0);
    if (this.subtitleChunks[currentChunk]?.length > 0) {
      this._signalReady();
    } else {
      window.dispatchEvent(new CustomEvent('display-status', {
        detail: { message: 'Listening... (no speech yet)', type: 'info' }
      }));
    }

    this.timeUpdateHandler = () => this._onTimeUpdate();
    video.addEventListener('timeupdate', this.timeUpdateHandler);

    this._runPipeline();

    DEBUG.success('AudioCapture', 'Pipeline started');
    return true;
  }

  // =========================================================================
  // PREPARE VIDEO
  // =========================================================================

  async _prepareVideo() {
    const t0 = performance.now();

    // Check if user set a custom stream URL (m3u8, direct media)
    let customUrl = null;
    try {
      const { customStreamUrl } = await new Promise(r =>
        chrome.storage.local.get(['customStreamUrl'], r)
      );
      customUrl = customStreamUrl || null;
    } catch (e) { console.warn('[Yume] storage read failed:', e.message || e); }

    if (customUrl) {
      this._addDiag(-1, 'prepare', 0, 0, 'Downloading from stream URL...');
      const response = await this._sendMessage({
        type: 'PREPARE_DIRECT',
        stream_url: customUrl,
        video_id: this.videoId
      });
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      if (!response || !response.success) {
        this._addDiag(-1, 'error', 0, elapsed, response?.error || 'Direct prepare failed');
        throw new Error(response?.error || 'Failed to download from stream URL');
      }
      this.prepared = true;
      this.videoDuration = response.duration || 0;
      this._addDiag(-1, 'ok', 0, elapsed, `Audio ready (direct): ${this.videoDuration.toFixed(0)}s`);
      // Clear the custom URL after successful use
      chrome.storage.local.remove('customStreamUrl');
      return;
    }

    // Normal mode: page URL → yt-dlp
    this._addDiag(-1, 'prepare', 0, 0, 'Downloading full audio...');

    const response = await this._sendMessage({
      type: 'PREPARE_VIDEO',
      url: this.videoUrl,
      video_id: this.videoId
    });

    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);

    if (!response || !response.success) {
      this._addDiag(-1, 'error', 0, elapsed, response?.error || 'Prepare failed');
      throw new Error(response?.error || 'Failed to download audio');
    }

    this.prepared = true;
    this.videoDuration = response.duration || 0;
    const cached = response.cached ? ' (cached)' : '';
    this._addDiag(-1, 'ok', 0, elapsed, `Audio ready: ${this.videoDuration.toFixed(0)}s${cached}`);
  }

  // =========================================================================
  // EAGER PIPELINE (parallel: transcribe N+1 while translating N)
  // =========================================================================

  async _runPipeline() {
    if (this.pipelineRunning) return;
    this.pipelineRunning = true;
    const gen = this.generation;

    while (this.isCapturing && !this.fetchingStopped && gen === this.generation) {
      const next = this._nextChunkToFetch();
      if (next === -1) {
        this._addDiag(-1, 'ok', 0, 0, `Pipeline complete: ${this.fetchedChunks.size}/${this.totalChunks} chunks`);
        this._dispatchProgress();
        break;
      }

      // Horizon: pause if too far ahead of playback (saves GPU for unwatched content)
      const currentChunk = this._chunkForTime(this.video?.currentTime || 0);
      if (next > currentChunk + 10 && next !== this.priorityChunk) {
        await this._sleep(2000);
        continue;
      }

      if (this.fetchingChunks.has(next)) { await this._sleep(500); continue; }

      // Phase 1: Transcribe this chunk (Whisper server)
      const transcribeResult = await this._transcribeOnly(next);
      if (gen !== this.generation) break; // video changed, abort
      if (!transcribeResult) continue; // error or empty, already handled

      // Phase 2: Fire translation in background (LLM server) — don't await!
      // This frees the Whisper server to handle the next chunk immediately
      this._translateAndFinalize(next, transcribeResult.segments, transcribeResult.whisperTime, transcribeResult.t0, gen);
      // No await ^ — loop immediately to transcribe next chunk
      // The translation promise runs concurrently with next transcription
    }

    this.pipelineRunning = false;
  }

  _nextChunkToFetch() {
    // Priority chunk (user seeked to unfetched region)
    if (this.priorityChunk >= 0 &&
        !this.fetchedChunks.has(this.priorityChunk) &&
        !this.fetchingChunks.has(this.priorityChunk)) {
      const p = this.priorityChunk;
      this.priorityChunk = -1;
      return p;
    }
    this.priorityChunk = -1;

    // Ripple outward from current playback position (covers both directions)
    const currentChunk = this._chunkForTime(this.video?.currentTime || 0);
    for (let offset = 0; offset < this.totalChunks; offset++) {
      // Check forward first, then backward at each distance
      const forward = currentChunk + offset;
      if (forward < this.totalChunks &&
          !this.fetchedChunks.has(forward) && !this.fetchingChunks.has(forward)) {
        return forward;
      }
      if (offset > 0) {
        const backward = currentChunk - offset;
        if (backward >= 0 &&
            !this.fetchedChunks.has(backward) && !this.fetchingChunks.has(backward)) {
          return backward;
        }
      }
    }
    return -1;
  }

  // =========================================================================
  // TIME UPDATE
  // =========================================================================

  _onTimeUpdate() {
    if (!this.isCapturing) return;
    const t = this.video.currentTime;
    const currentChunk = this._chunkForTime(t);

    if (!this.fetchedChunks.has(currentChunk) && !this.fetchingChunks.has(currentChunk)) {
      this.priorityChunk = currentChunk;
      if (!this.pipelineRunning && !this.fetchingStopped) this._runPipeline();
    }

    this._showSubtitleAt(t);
  }

  // =========================================================================
  // SUBTITLE DISPLAY
  // =========================================================================

  _showSubtitleAt(currentTime) {
    // Apply user timing offset (negative = earlier, positive = later)
    const adjustedTime = currentTime + this.timingOffset;
    const chunkIdx = this._chunkForTime(adjustedTime);
    // Check prev/current/next chunks to cover the 5s overlap window
    for (const idx of [chunkIdx - 1, chunkIdx, chunkIdx + 1]) {
      if (idx < 0) continue;
      const segments = this.subtitleChunks[idx];
      if (!segments) continue;
      for (const seg of segments) {
        if (adjustedTime >= seg.start && adjustedTime < seg.end) {
          if (this.lastShownSegment === seg) return;
          // Re-check blacklist at display time only if not already verified
          if (!seg._checked) {
            if (this._isHallucination(seg.original || seg.text || '')) {
              seg._checked = true; seg._blocked = true;
              continue;
            }
            seg._checked = true; seg._blocked = false;
          }
          if (seg._blocked) continue;
          this.lastShownSegment = seg;
          window.dispatchEvent(new CustomEvent('display-subtitle', {
            detail: {
              original: seg.original, english: seg.english, romaji: seg.romaji || '',
              confidence: seg.confidence || 0
            }
          }));
          return;
        }
      }
    }

    if (this.fetchedChunks.has(chunkIdx) && this.lastShownSegment !== null) {
      if (adjustedTime >= this.lastShownSegment.end + 1.0) {
        this.lastShownSegment = null;
        window.dispatchEvent(new CustomEvent('display-subtitle', {
          detail: { original: '', english: '', romaji: '' }
        }));
      }
    }
  }

  // =========================================================================
  // FETCH A CHUNK (transcribe + translate + optionally romanize)
  // Used for first chunk (must complete fully before playback starts)
  // Pipeline chunks use _transcribeOnly + _translateAndFinalize for overlap
  // =========================================================================

  async _fetchChunk(chunkIndex) {
    if (this.fetchingChunks.has(chunkIndex) || this.fetchedChunks.has(chunkIndex)) return;
    this.fetchingChunks.add(chunkIndex);

    const chunkStart = chunkIndex * this.stepSize;
    const chunkEnd = chunkStart + this.chunkDuration;
    const t0 = performance.now();

    this._addDiag(chunkIndex, 'fetching', 0, 0, `${chunkStart}s-${chunkEnd}s`);

    try {
      // 1. Transcribe (with retry)
      const tWhisper = performance.now();
      const transcription = await this._retryAsync(() => this._requestTranscription(chunkIndex), 2);
      const whisperTime = ((performance.now() - tWhisper) / 1000).toFixed(1);

      // Filter hallucinations (built-in + user blacklist)
      const cleanSegments = (transcription?.segments || [])
        .filter(seg => !this._isHallucination(seg.text));

      if (cleanSegments.length === 0) {
        const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
        this._addDiag(chunkIndex, 'empty', 0, elapsed, `${transcription?.segments?.length || 0} raw -> 0 clean`);
        this.subtitleChunks[chunkIndex] = [];
        this.fetchedChunks.add(chunkIndex);
        this.fetchingChunks.delete(chunkIndex);
        this._dispatchProgress();

        this.emptyChunkCount++;
        if (this.emptyChunkCount >= this.maxEmptyChunks) {
          this.fetchingStopped = true;
          this._addDiag(chunkIndex, 'stopped', 0, 0, 'Too many empty chunks');
        }
        if (chunkIndex === this._chunkForTime(this.video?.currentTime || 0) && this.subtitleChunks[chunkIndex]?.length > 0) this._signalReady();
        return;
      }

      this.emptyChunkCount = 0;
      if (this.fetchingStopped) { this.fetchingStopped = false; }

      // 2. Translate (skip entirely if translation is disabled — saves full LLM inference)
      const tTrans = performance.now();
      const { settings: transSettings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
      let translated;
      if (transSettings?.showEnglish === false) {
        // No translation — store source-language-only segments
        translated = cleanSegments.map(seg => ({
          start: seg.start, end: seg.end,
          original: seg.text, english: '', romaji: '',
          confidence: seg.confidence || 0
        }));
      } else {
        translated = await this._translateBatch(cleanSegments);
      }
      const transTime = ((performance.now() - tTrans) / 1000).toFixed(1);

      // 3. Romanize if enabled (completely separate call)
      let romaTime = '0';
      try {
        if (transSettings?.showRomaji) {
          const tRoma = performance.now();
          await this._romanizeBatch(translated);
          romaTime = ((performance.now() - tRoma) / 1000).toFixed(1);
        }
      } catch (e) { /* romanization is optional */ }

      // 4. Store in cache
      this.subtitleChunks[chunkIndex] = translated;
      this.fetchedChunks.add(chunkIndex);
      this.fetchingChunks.delete(chunkIndex);

      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      const preview = translated[0]?.original?.substring(0, 20) || '';
      const progress = `[${this.fetchedChunks.size}/${this.totalChunks}]`;
      DEBUG.success('AudioCapture', `Chunk ${chunkIndex} ready: ${translated.length} segs in ${elapsed}s ${progress}`);
      this._addDiag(chunkIndex, 'ok', translated.length, elapsed,
        `w:${whisperTime}s t:${transTime}s r:${romaTime}s "${preview}"`);
      this._dispatchProgress();

      if (chunkIndex === this._chunkForTime(this.video?.currentTime || 0) && this.subtitleChunks[chunkIndex]?.length > 0) this._signalReady();

    } catch (error) {
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      this._addDiag(chunkIndex, 'error', 0, elapsed, error.message);
      this.fetchingChunks.delete(chunkIndex);
      if (chunkIndex === 0) {
        window.dispatchEvent(new CustomEvent('display-error', {
          detail: { message: `Failed: ${error.message}` }
        }));
      }
    }
  }

  // Phase 1: Transcribe only (Whisper server). Returns segments or null.
  async _transcribeOnly(chunkIndex) {
    if (this.fetchingChunks.has(chunkIndex) || this.fetchedChunks.has(chunkIndex)) return null;
    this.fetchingChunks.add(chunkIndex);

    const chunkStart = chunkIndex * this.stepSize;
    const chunkEnd = chunkStart + this.chunkDuration;
    const t0 = performance.now();

    this._addDiag(chunkIndex, 'fetching', 0, 0, `${chunkStart}s-${chunkEnd}s`);

    try {
      const tWhisper = performance.now();
      const transcription = await this._retryAsync(() => this._requestTranscription(chunkIndex), 2);
      const whisperTime = ((performance.now() - tWhisper) / 1000).toFixed(1);

      const cleanSegments = (transcription?.segments || [])
        .filter(seg => !this._isHallucination(seg.text));

      if (cleanSegments.length === 0) {
        const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
        this._addDiag(chunkIndex, 'empty', 0, elapsed, `${transcription?.segments?.length || 0} raw -> 0 clean`);
        this.subtitleChunks[chunkIndex] = [];
        this.fetchedChunks.add(chunkIndex);
        this.fetchingChunks.delete(chunkIndex);
        this._dispatchProgress();

        this.emptyChunkCount++;
        if (this.emptyChunkCount >= this.maxEmptyChunks) {
          this.fetchingStopped = true;
          this._addDiag(chunkIndex, 'stopped', 0, 0, 'Too many empty chunks');
        }
        if (chunkIndex === this._chunkForTime(this.video?.currentTime || 0) && this.subtitleChunks[chunkIndex]?.length > 0) this._signalReady();
        return null;
      }

      this.emptyChunkCount = 0;
      if (this.fetchingStopped) { this.fetchingStopped = false; }

      return { segments: cleanSegments, whisperTime, t0 };

    } catch (error) {
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      this._addDiag(chunkIndex, 'error', 0, elapsed, error.message);
      this.fetchingChunks.delete(chunkIndex);
      if (chunkIndex === 0) {
        window.dispatchEvent(new CustomEvent('display-error', {
          detail: { message: `Failed: ${error.message}` }
        }));
      }
      return null;
    }
  }

  // Phase 2: Translate + romanize + store. Runs concurrently with next transcription.
  // gen parameter ensures stale promises from old videos are silently dropped.
  async _translateAndFinalize(chunkIndex, cleanSegments, whisperTime, t0, gen) {
    try {
      // Check generation — if video changed, silently abort
      if (gen !== this.generation) { this.fetchingChunks.delete(chunkIndex); return; }

      // IMMEDIATELY store source-only segments so they display before translation
      const placeholders = cleanSegments.map(seg => ({
        start: seg.start, end: seg.end,
        original: seg.text, english: '', romaji: '',
        confidence: seg.confidence || 0
      }));
      this.subtitleChunks[chunkIndex] = placeholders;

      // Skip translation entirely if disabled — saves full LLM inference per chunk
      const tTrans = performance.now();
      const { settings: pSettings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
      let translated;
      if (pSettings?.showEnglish === false) {
        translated = placeholders;  // Already in correct format
      } else {
        translated = await this._translateBatch(cleanSegments);
      }
      const transTime = ((performance.now() - tTrans) / 1000).toFixed(1);

      // Re-check generation after async translation
      if (gen !== this.generation) { this.fetchingChunks.delete(chunkIndex); return; }

      let romaTime = '0';
      try {
        if (pSettings?.showRomaji) {
          const tRoma = performance.now();
          await this._romanizeBatch(translated);
          romaTime = ((performance.now() - tRoma) / 1000).toFixed(1);
        }
      } catch (e) { /* romanization is optional */ }

      // Final generation check before storing
      if (gen !== this.generation) { this.fetchingChunks.delete(chunkIndex); return; }

      // Update with full translated segments (replaces source-only placeholders)
      this.subtitleChunks[chunkIndex] = translated;
      this.fetchedChunks.add(chunkIndex);
      this.fetchingChunks.delete(chunkIndex);

      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      const preview = translated[0]?.original?.substring(0, 20) || '';
      const progress = `[${this.fetchedChunks.size}/${this.totalChunks}]`;
      DEBUG.success('AudioCapture', `Chunk ${chunkIndex} ready: ${translated.length} segs in ${elapsed}s ${progress} [parallel]`);
      this._addDiag(chunkIndex, 'ok', translated.length, elapsed,
        `w:${whisperTime}s t:${transTime}s r:${romaTime}s "${preview}"`);
      this._dispatchProgress();

      if (chunkIndex === this._chunkForTime(this.video?.currentTime || 0) && this.subtitleChunks[chunkIndex]?.length > 0) this._signalReady();

    } catch (error) {
      if (gen !== this.generation) return; // stale, ignore
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      this._addDiag(chunkIndex, 'error', 0, elapsed, 'Translate: ' + error.message);
      this.fetchingChunks.delete(chunkIndex);
    }
  }

  _signalReady() {
    window.dispatchEvent(new CustomEvent('prefetch-ready', { detail: { chunkIndex: 0 } }));
  }

  _dispatchProgress() {
    window.dispatchEvent(new CustomEvent('chunk-progress', {
      detail: {
        fetched: this.fetchedChunks.size,
        total: this.totalChunks,
        complete: this.fetchedChunks.size >= this.totalChunks
      }
    }));
    // Auto-save to session after each chunk (debounced)
    this._debounceSaveSession();
  }

  // =========================================================================
  // SESSION PERSISTENCE — survive page reloads without re-transcribing
  // Stores subtitleChunks in chrome.storage.session (cleared on browser close)
  // =========================================================================

  _debounceSaveSession() {
    if (this._saveTimer) clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._saveToSession(), 2000);
  }

  async _saveToSession() {
    if (!this.videoId || this.fetchedChunks.size === 0) return;
    try {
      const data = {
        subtitleChunks: this.subtitleChunks,
        fetchedChunks: [...this.fetchedChunks],
        totalChunks: this.totalChunks,
        videoDuration: this.videoDuration,
        timestamp: Date.now()
      };
      // Key by videoId, keep last 3 videos max
      const sessionKey = `yume_${this.videoId}`;
      const existing = await chrome.storage.session.get(null);
      const yumeKeys = Object.keys(existing).filter(k => k.startsWith('yume_')).sort((a, b) =>
        (existing[a]?.timestamp || 0) - (existing[b]?.timestamp || 0)
      );
      // Evict oldest if over limit
      if (yumeKeys.length >= 3 && !yumeKeys.includes(sessionKey)) {
        await chrome.storage.session.remove(yumeKeys[0]);
      }
      await chrome.storage.session.set({ [sessionKey]: data });
    } catch (e) {
      // Session storage can fail (quota, etc) — non-critical
    }
  }

  async _restoreFromSession() {
    if (!this.videoId) return false;
    try {
      const sessionKey = `yume_${this.videoId}`;
      const stored = await chrome.storage.session.get(sessionKey);
      const data = stored[sessionKey];
      if (!data || !data.subtitleChunks || !data.fetchedChunks) return false;
      // Only restore if reasonably fresh (< 30 min)
      if (Date.now() - (data.timestamp || 0) > 30 * 60 * 1000) return false;

      this.subtitleChunks = data.subtitleChunks;
      this.fetchedChunks = new Set(data.fetchedChunks);
      this.totalChunks = data.totalChunks || this.totalChunks;
      this.videoDuration = data.videoDuration || this.videoDuration;

      DEBUG.success('AudioCapture',
        `Restored ${this.fetchedChunks.size}/${this.totalChunks} chunks from session`);
      this._dispatchProgress();
      return true;
    } catch (e) {
      return false;
    }
  }

  // Returns the currently displayed subtitle text (for hallucination reporting)
  getCurrentSubtitle() {
    if (!this.lastShownSegment) return null;
    return {
      original: this.lastShownSegment.original || '',
      english: this.lastShownSegment.english || '',
      romaji: this.lastShownSegment.romaji || '',
      start: this.lastShownSegment.start,
      end: this.lastShownSegment.end
    };
  }

  // =========================================================================
  // EXPORT SUBTITLES (SRT format)
  // =========================================================================

  exportSRT() {
    const allSegments = [];
    for (const [idx, segments] of Object.entries(this.subtitleChunks)) {
      for (const seg of segments) {
        if (seg.original || seg.english) allSegments.push(seg);
      }
    }
    allSegments.sort((a, b) => a.start - b.start);

    // Deduplicate segments with same start time (overlap region)
    const deduped = [];
    for (const seg of allSegments) {
      const last = deduped[deduped.length - 1];
      if (last && Math.abs(last.start - seg.start) < 0.3 && last.original === seg.original) continue;
      deduped.push(seg);
    }

    const lines = [];
    deduped.forEach((seg, i) => {
      lines.push(`${i + 1}`);
      lines.push(`${this._fmtSrt(seg.start)} --> ${this._fmtSrt(seg.end)}`);
      const parts = [];
      if (seg.original) parts.push(seg.original);
      if (seg.english && seg.english !== seg.original) parts.push(seg.english);
      if (seg.romaji) parts.push(seg.romaji);
      lines.push(parts.join('\n'));
      lines.push('');
    });

    return { srt: lines.join('\n'), count: deduped.length, fetched: this.fetchedChunks.size, total: this.totalChunks };
  }

  _fmtSrt(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60), ms = Math.round((sec % 1) * 1000);
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')},${String(ms).padStart(3,'0')}`;
  }

  // =========================================================================
  // SERVER CALLS
  // =========================================================================

  async _requestTranscription(chunkIndex) {
    const response = await this._sendMessage({
      type: 'TRANSCRIBE_URL',
      url: this.videoUrl,
      video_id: this.videoId,
      chunk_index: chunkIndex,
      chunk_duration: this.chunkDuration,
      step_size: this.stepSize,
      language: this.sourceLanguage || 'ja'
    });
    if (!response || !response.success) throw new Error(response?.error || 'Transcription failed');
    return response.result;
  }

  // Translation — batch mode: all segments in a single LLM call
  async _translateBatch(segments) {
    if (segments.length === 0) return [];

    // Build numbered batch for single API call
    const batchText = segments.map((seg, i) => `[${i + 1}] ${seg.text}`).join('\n');

    let translations = [];
    try {
      const response = await this._sendMessage({
        type: 'TRANSLATE_BATCH',
        text: batchText,
        count: segments.length
      });
      if (response?.success && response.translations) {
        translations = response.translations;
      }
    } catch (err) {
      DEBUG.warn('AudioCapture', 'Batch translation failed, falling back', { error: err.message });
    }

    // If batch returned too few, fill gaps with sequential calls
    if (translations.length < segments.length) {
      DEBUG.info('AudioCapture', `Batch got ${translations.length}/${segments.length}, filling gaps`);
      for (let i = 0; i < segments.length; i++) {
        if (translations[i] && translations[i].length > 0) continue;
        try {
          const response = await this._sendMessage({
            type: 'TRANSLATE',
            text: segments[i].text,
            sourceLang: this.sourceLanguage || 'ja'
          });
          if (response?.success && response.translation) {
            if (!translations[i]) translations[i] = response.translation.trim();
          }
        } catch (e) { /* keep original */ }
      }
    }

    return segments.map((seg, i) => ({
      start:      seg.start,
      end:        seg.end,
      original:   seg.text,
      english:    (translations[i] || '').trim(),
      romaji:     '',
      confidence: seg.confidence || 0
    }));
  }

  // Separate romanization call — only runs if showRomaji is on
  async _romanizeBatch(segments) {
    for (const seg of segments) {
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE',
          text: seg.original,
          sourceLang: this.sourceLanguage || 'ja'
        });
        if (response?.success && response.romanization) {
          seg.romaji = response.romanization.trim();
        }
      } catch (err) {
        DEBUG.warn('AudioCapture', 'Romanization failed', { text: seg.original });
      }
    }
  }

  // Re-romanize already cached chunks (when user toggles romaji ON mid-video)
  async reRomanizeCachedChunks() {
    DEBUG.info('AudioCapture', 'Re-romanizing cached chunks');
    for (const [idx, segments] of Object.entries(this.subtitleChunks)) {
      for (const seg of segments) {
        if (!seg.romaji) {
          try {
            const response = await this._sendMessage({
              type: 'ROMANIZE',
              text: seg.original,
              sourceLang: this.sourceLanguage || 'ja'
            });
            if (response?.success && response.romanization) {
              seg.romaji = response.romanization.trim();
            }
          } catch (e) { /* skip */ }
        }
      }
    }
    // Force refresh current display
    if (this.video) this._showSubtitleAt(this.video.currentTime);
    DEBUG.success('AudioCapture', 'Re-romanization complete');
  }

  // Remove blacklisted segments from cache and clear display if current one was blacklisted
  _purgeBlacklistedFromCache() {
    let purged = 0;
    for (const [idx, segments] of Object.entries(this.subtitleChunks)) {
      // Reset check flags so updated blacklist is applied
      for (const seg of segments) { seg._checked = false; seg._blocked = false; }
      const before = segments.length;
      this.subtitleChunks[idx] = segments.filter(seg =>
        !this._isHallucination(seg.original || seg.text || '')
      );
      purged += before - this.subtitleChunks[idx].length;
    }
    if (purged > 0) {
      DEBUG.info('AudioCapture', `Purged ${purged} blacklisted segments from cache`);
      if (this.lastShownSegment && this._isHallucination(
        this.lastShownSegment.original || this.lastShownSegment.text || ''
      )) {
        this.lastShownSegment = null;
        window.dispatchEvent(new CustomEvent('display-subtitle', {
          detail: { original: '', english: '', romaji: '' }
        }));
      }
    }
  }

  // Fetch hallucination patterns from server (single source of truth)
  async _fetchServerPatterns() {
    try {
      const response = await this._sendMessage({ type: 'FETCH_PATTERNS' });
      if (response?.success && response.data) {
        const d = response.data;
        this.serverPatterns = {
          builtin: (d.builtin || []).map(p => p.toLowerCase()),
          credits: (d.credits || []).map(p => p.toLowerCase()),
          singleWords: (d.single_word_blocklist || []).map(p => p.toLowerCase()),
          repeatThreshold: d.repeat_threshold || 6,
          concatMinLen: d.concat_min_len || 4,
          concatCoverage: d.concat_coverage || 0.8,
        };
        const total = this.serverPatterns.builtin.length + this.serverPatterns.credits.length;
        DEBUG.info('AudioCapture', `Loaded ${total} patterns from server`);
      }
    } catch (e) {
      DEBUG.warn('AudioCapture', 'Could not fetch server patterns, using fallback');
    }
  }

  // Client-side hallucination guard (server patterns + user blacklist)
  // Server is authoritative for built-in patterns — client only maintains fallback
  _isHallucination(text) {
    const t = (text || '').trim().toLowerCase();
    if (!t) return true;

    // Built-in patterns: prefer server-synced, fallback to minimal hardcoded set
    const builtinPatterns = this.serverPatterns?.builtin || [
      'sound hodori', '\ud638\ub3cc\uc774', '\u30db\u30c9\u30ea',
      '\u304a\u75b2\u308c\u69d8', '\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046',
      'thank you for watching', '[music]', '(music)', 'subscribe',
    ];
    for (const b of builtinPatterns) {
      if (t.includes(b)) return true;
    }

    // Credits patterns (from server)
    if (this.serverPatterns?.credits) {
      for (const c of this.serverPatterns.credits) {
        if (t.includes(c)) return true;
      }
    }

    // User-reported blacklist
    for (const bl of this.userBlacklist) {
      if (bl && t.includes(bl.toLowerCase())) return true;
    }

    // Repeated word spam
    const repeatThreshold = this.serverPatterns?.repeatThreshold || 6;
    const words = t.split(/\s+/);
    if (words.length >= repeatThreshold) {
      const unique = new Set(words.map(w => w.replace(/[^\w]/g, '')));
      if (unique.size <= 2) return true;
    }

    // Concatenated repetition
    const concatMinLen = this.serverPatterns?.concatMinLen || 4;
    const concatCoverage = this.serverPatterns?.concatCoverage || 0.8;
    const clean = t.replace(/\s/g, '');
    if (clean.length >= concatMinLen) {
      for (let len = 2; len <= Math.min(8, clean.length / 2); len++) {
        const sub = clean.substring(0, len);
        const repeats = Math.floor(clean.length / len);
        if (repeats >= 2 && sub.repeat(repeats) === clean.substring(0, len * repeats)) {
          if (len * repeats >= clean.length * concatCoverage) return true;
        }
      }
    }

    // Single word blocklist
    const singleWords = this.serverPatterns?.singleWords || ['music', 'la', 'na', 'da', 'oh', 'ah', 'mm', 'hmm'];
    if (singleWords.includes(t)) return true;

    return false;
  }

  // =========================================================================
  // RETRY HELPER — retries async fn with exponential backoff
  // =========================================================================

  async _retryAsync(fn, maxRetries = 2) {
    let lastError;
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await fn();
      } catch (e) {
        lastError = e;
        if (attempt < maxRetries) {
          const delay = 1000 * Math.pow(2, attempt);
          DEBUG.warn('AudioCapture', `Retry ${attempt + 1}/${maxRetries} after ${delay}ms: ${e.message}`);
          await this._sleep(delay);
        }
      }
    }
    throw lastError;
  }

  // =========================================================================
  // STOP — increments generation to kill all stale parallel promises
  // =========================================================================

  stopCapture() {
    this.generation++; // Kill all in-flight _translateAndFinalize promises
    this.isCapturing = false;
    if (this.video && this.timeUpdateHandler) {
      this.video.removeEventListener('timeupdate', this.timeUpdateHandler);
      this.timeUpdateHandler = null;
    }
    this.subtitleChunks = {}; this.fetchedChunks = new Set(); this.fetchingChunks = new Set();
    this.lastShownSegment = null; this.emptyChunkCount = 0; this.fetchingStopped = false;
    this.prepared = false; this.pipelineRunning = false; this.priorityChunk = -1;

    // Clear server subtitle cache (fire and forget)
    this._sendMessage({ type: 'CLEAR_SERVER_CACHE' }).catch(e => console.warn('[Yume]', e.message));
  }

  // Clear cached subtitles without stopping capture — for "Clear Cache" button
  clearContentCache() {
    const chunkCount = Object.keys(this.subtitleChunks).length;
    this.subtitleChunks = {};
    this.fetchedChunks  = new Set();
    this.fetchingChunks = new Set();
    this.lastShownSegment = null;
    this.emptyChunkCount  = 0;
    this.fetchingStopped  = false;
    // Clear displayed subtitle
    window.dispatchEvent(new CustomEvent('display-subtitle', {
      detail: { original: '', english: '', romaji: '' }
    }));
    console.log(`[AudioCapture] Content cache cleared (${chunkCount} chunks)`);
    return chunkCount;
  }

  isActive() { return this.isCapturing; }

  setChunkDuration(sec) {
    this.chunkDuration = Math.max(10, Math.min(60, sec));
    this.stepSize = Math.max(5, this.chunkDuration - 5);
  }

  _chunkForTime(time) { return Math.max(0, Math.floor(time / this.stepSize)); }
  _sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

  // =========================================================================
  // DIAGNOSTICS
  // =========================================================================

  _addDiag(chunk, status, segments, elapsed, details) {
    const time = new Date().toLocaleTimeString('en-GB', { hour12: false });
    this.diagLog.push({ time, chunk, status, segments, elapsed, details });
    if (this.diagLog.length > this.maxDiagEntries) this.diagLog.shift();
  }

  getDiagnostics() {
    return {
      active: this.isCapturing, videoId: this.videoId,
      chunkDuration: this.chunkDuration, stepSize: this.stepSize,
      prepared: this.prepared, videoDuration: this.videoDuration,
      totalChunks: this.totalChunks,
      fetchedCount: this.fetchedChunks.size, fetchingCount: this.fetchingChunks.size,
      emptyStreak: this.emptyChunkCount, stopped: this.fetchingStopped,
      pipelineRunning: this.pipelineRunning, generation: this.generation, log: this.diagLog
    };
  }

  _getVideoId() {
    const params = new URLSearchParams(window.location.search);
    const ytId = params.get('v');
    if (ytId) return ytId;
    // Non-YouTube: include video src snippet for uniqueness across videos on same page
    const video = document.querySelector('video');
    const srcHint = (video?.src || video?.currentSrc || '').slice(-20).replace(/\W/g, '');
    return window.location.pathname.replace(/\W/g, '-') + '-' + srcHint + '-' + Date.now();
  }

  _sendMessage(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(response);
      });
    });
  }
}

if (typeof window !== 'undefined') { window.AudioCapture = AudioCapture; }
