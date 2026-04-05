// ============================================================================
// AUDIO CAPTURE v3.10.0 - Download-once + parallel pipeline (Yume v0.0.8)
// Translation and romanization are SEPARATE API calls
// ============================================================================

const _DEFAULT_WHISPER_URL = 'http://localhost:5001';

// Languages with instant deterministic romanization (no LLM needed).
// ja: WanaKana (client-side), ko: hangul decomposition (client-side),
// ru: cyrillic transliteration (client-side), zh: pypinyin (server-side).
const DETERMINISTIC_ROMA_LANGS = new Set(['ja', 'zh', 'ko', 'ru']);

// Cached result of deterministic romanization probe (null = not yet checked)
let _deterministicRomaAvailable = null;
let _deterministicRomaProbeTime = 0;  // timestamp of last probe (for re-probe on failure)

class AudioCapture {
  constructor() {
    DEBUG.functionStart('AudioCapture', 'constructor');

    this.isCapturing    = false;
    this.videoUrl       = null;
    this.videoId        = null;
    this.sourceLanguage = 'ja';
    this.showEnglish    = true;
    this.showRomaji     = false;

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
    this._readySignaled = false;

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

    // Listen for blacklist + settings changes (bound so we can remove on dispose)
    // NOTE: Language change restart is handled by content.js only — not here.
    // This listener only handles blacklist, timingOffset, and romaji state sync.
    this._onStorageChanged = (changes) => {
      if (changes.hallucinationBlacklist) {
        this.userBlacklist = changes.hallucinationBlacklist.newValue || [];
        DEBUG.info('AudioCapture', `User blacklist updated: ${this.userBlacklist.length} items`);
        this._purgeBlacklistedFromCache();
      }
      if (changes.settings) {
        const s = changes.settings.newValue || {};
        this.timingOffset = (s.timingOffset || 0) / 10;
        this.sourceLanguage = s.sourceLanguage || 'ja';
        this.showRomaji = s.showRomaji || false;
      }
    };
    chrome.storage.onChanged.addListener(this._onStorageChanged);

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
    this._readySignaled = false;

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

    // Pre-start health check — fail fast if server isn't running, wait if loading
    window.dispatchEvent(new CustomEvent('display-status', {
      detail: { message: 'Checking server...', type: 'loading' }
    }));
    const maxWait = 120;  // seconds to wait for model loading
    const startWait = Date.now();
    while (true) {
      try {
        const healthResp = await this._sendMessage({ type: 'CHECK_SERVER', isWhisper: true,
          url: ((await new Promise(r => chrome.storage.local.get(['settings'], r)))?.settings?.whisperUrl
            || _DEFAULT_WHISPER_URL) + '/health'
        });
        if (healthResp?.healthy) break;  // Server ready
        if (healthResp?.loading) {
          // Model is loading — show status and retry
          const elapsed = Math.round((Date.now() - startWait) / 1000);
          if (elapsed > maxWait) throw new Error('Model loading timed out — restart Yume');
          window.dispatchEvent(new CustomEvent('display-status', {
            detail: { message: `Loading model... (${elapsed}s)`, type: 'loading' }
          }));
          await this._sleep(2000);
          continue;
        }
        throw new Error('Whisper server not reachable — is Yume running?');
      } catch (e) {
        if (e.message.includes('not reachable') || e.message.includes('Yume running') || e.message.includes('timed out')) throw e;
        throw new Error('Whisper server not reachable — is Yume running?');
      }
    }

    // Probe deterministic romanization AFTER server is confirmed ready.
    // Must be here, not before health check — the server needs to be up
    // for the probe to reach pykakasi/pypinyin on the server.
    await this._probeDeterministicRoma();

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

    // Always fetch chunk 0 first — the video may have advanced during the
    // prepare phase (audio download takes 5-30s), causing _chunkForTime()
    // to return chunk 1+ and skip the beginning of the video entirely.
    if (startChunk > 0 && !this.fetchedChunks.has(0)) {
      DEBUG.info('AudioCapture', `Video advanced to chunk ${startChunk} during prepare — fetching chunk 0 first`);
      await this._fetchChunk(0);
    }
    await this._fetchChunk(startChunk);
    // Always signal ready so the pipeline + timeupdate handler can start.
    // If the first chunk(s) had no vocals (instrumental intro, silence, etc.),
    // show a different status so the user doesn't see "Ready ✓" with 0 subtitles.
    // NOTE: fetchedChunks includes empty chunks (0 segments), so we must check
    // whether any fetched chunk has actual subtitle content, not just chunk count.
    const hasSubtitles = Array.from(this.fetchedChunks).some(i =>
      this.subtitleChunks[i] && this.subtitleChunks[i].length > 0
    );
    if (hasSubtitles) {
      this._signalReady();
    } else {
      window.dispatchEvent(new CustomEvent('display-status', {
        detail: { message: 'Processing — no vocals detected yet', type: 'loading' }
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
  // CRITICAL: Only ONE translation runs at a time. The LLM is single-threaded.
  // Firing multiple concurrent translations causes timeouts and lost results.
  // =========================================================================

  async _runPipeline() {
    if (this.pipelineRunning) return;
    this.pipelineRunning = true;
    const gen = this.generation;
    let pendingTranslation = null;  // Promise for the current in-flight translation

    while (this.isCapturing && !this.fetchingStopped && gen === this.generation) {
      const next = this._nextChunkToFetch();
      if (next === -1) {
        // All chunks are either fetched or in-flight. Wait for the last translation.
        if (pendingTranslation) {
          await pendingTranslation;
          pendingTranslation = null;
          // The translation might have FAILED, leaving the chunk in limbo
          // (not in fetchedChunks, not in fetchingChunks). Loop back to check
          // if there are now retryable chunks instead of breaking immediately.
          if (this.fetchedChunks.size < this.totalChunks) continue;
        }
        const nonEmpty = Array.from(this.fetchedChunks).filter(i => 
          this.subtitleChunks[i] && this.subtitleChunks[i].length > 0
        ).length;
        const total = this.fetchedChunks.size;
        if (nonEmpty === 0 && total > 0) {
          this._addDiag(-1, 'stopped', 0, 0, `All ${total} chunks empty — try Clear Cache and restart`);
        } else {
          this._addDiag(-1, 'ok', 0, 0, `Pipeline complete: ${nonEmpty} with subtitles / ${total} total`);
        }
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

      // Wait for previous translation to finish before starting next transcription.
      // This ensures at most 1 translation is in flight at any time.
      // The overlap is: transcribe(N) happens while translate(N-1) finishes.
      if (pendingTranslation) {
        await pendingTranslation;
        pendingTranslation = null;
      }

      // Phase 1: Transcribe this chunk (Whisper server)
      const transcribeResult = await this._transcribeOnly(next);
      if (gen !== this.generation) break; // video changed, abort
      if (!transcribeResult) continue; // error or empty, already handled

      // Phase 2: Fire translation in background — don't await!
      // Next loop iteration will await it before starting the NEXT transcription.
      pendingTranslation = this._translateAndFinalize(next, transcribeResult.segments, transcribeResult.whisperTime, transcribeResult.t0, gen, transcribeResult.wasCached);
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
      // If pipeline stopped due to empty streak but user seeked to a new region,
      // clear the stop flag — the new region may have audio
      if (this.fetchingStopped) {
        this.fetchingStopped = false;
        this.emptyChunkCount = 0;
        DEBUG.info('AudioCapture', `User seeked to unfetched chunk ${currentChunk} — resuming pipeline`);
      }
      if (!this.pipelineRunning) this._runPipeline();
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
  // SHARED: Transcribe + filter hallucinations + handle empty chunks
  // Used by both _fetchChunk (synchronous first chunk) and _transcribeOnly (pipeline)
  // Returns { segments, whisperTime, t0 } or null if empty/error.
  // =========================================================================

  async _transcribeAndFilter(chunkIndex) {
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
      const wasCached = transcription?.cached === true;

      const cleanSegments = (transcription?.segments || [])
        .filter(seg => !this._isHallucination(seg.text));

      if (cleanSegments.length === 0) {
        const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
        const rawCount = transcription?.segments?.length || 0;
        const cacheTag = wasCached ? ' (server cache)' : '';
        this._addDiag(chunkIndex, 'empty', 0, elapsed, `${rawCount} raw -> 0 clean${cacheTag}`);
        this.subtitleChunks[chunkIndex] = [];
        this.fetchedChunks.add(chunkIndex);
        this.fetchingChunks.delete(chunkIndex);
        this._dispatchProgress();

        this.emptyChunkCount++;
        // Only stop the pipeline if many chunks remain unfetched.
        // When only a few chunks are left (end-of-song outros), keep going
        // so backward-ripple stragglers aren't abandoned as "not processed".
        const unfetchedRemaining = this.totalChunks - this.fetchedChunks.size;
        if (this.emptyChunkCount >= this.maxEmptyChunks && unfetchedRemaining > 3) {
          this.fetchingStopped = true;
          this._addDiag(chunkIndex, 'stopped', 0, 0, 'Too many empty chunks — try Clear Cache if subtitles are missing');
        }
        return null;
      }

      this.emptyChunkCount = 0;
      if (this.fetchingStopped) { this.fetchingStopped = false; }

      return { segments: cleanSegments, whisperTime, t0, wasCached };

    } catch (error) {
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      this._addDiag(chunkIndex, 'error', 0, elapsed, error.message);
      this.fetchingChunks.delete(chunkIndex);
      this.fetchedChunks.add(chunkIndex);
      this.subtitleChunks[chunkIndex] = [];
      this._dispatchProgress();
      if (chunkIndex === 0) {
        window.dispatchEvent(new CustomEvent('display-error', {
          detail: { message: `Failed: ${error.message}` }
        }));
      }
      return null;
    }
  }

  // =========================================================================
  // FETCH A CHUNK (transcribe + translate + optionally romanize)
  // Used for first chunk (must complete fully before playback starts)
  // Pipeline chunks use _transcribeOnly + _translateAndFinalize for overlap
  // =========================================================================

  async _fetchChunk(chunkIndex) {
    const result = await this._transcribeAndFilter(chunkIndex);
    if (!result) return;

    const { segments: cleanSegments, whisperTime, t0, wasCached } = result;

    // Store source-only placeholders IMMEDIATELY so original text is visible
    // even if translation fails (graceful degradation)
    const placeholders = cleanSegments.map(seg => ({
      start: seg.start, end: seg.end,
      original: seg.text, english: '', romaji: '',
      confidence: seg.confidence || 0
    }));
    this.subtitleChunks[chunkIndex] = placeholders;

    try {
      // 2. Translate (skip entirely if translation is disabled)
      const tTrans = performance.now();
      const { settings: transSettings } = await new Promise(r => chrome.storage.local.get(['settings'], r));
      const srcLang = transSettings?.sourceLanguage || this.sourceLanguage || 'ja';
      const isDeterministic = DETERMINISTIC_ROMA_LANGS.has(srcLang);

      // Decide whether to romanize:
      // - Deterministic languages (ja/zh/ko): ALWAYS (speculative — nearly free, <100ms)
      // - LLM languages (ru/ar): only if user enabled showRomaji
      const shouldRomanize = isDeterministic || transSettings?.showRomaji;

      let translated;
      if (transSettings?.showEnglish === false) {
        translated = placeholders;
      } else {
        // Romanization strategy depends on whether deterministic (instant) is available:
        // - Deterministic (pykakasi/pypinyin): parallel with translation (free, <100ms)
        // - LLM-only: AFTER translation (they share the same LLM, can't parallelize)
        const canParallelRoma = shouldRomanize && _deterministicRomaAvailable === true;

        if (canParallelRoma) {
          // Parallel: translation + romanization fire simultaneously
          const origTexts = cleanSegments.map(seg => seg.text);
          const [batchResult, romaResult] = await Promise.all([
            this._translateBatch(cleanSegments),
            this._romanizeTexts(origTexts, srcLang)
          ]);
          translated = batchResult;
          if (romaResult) {
            for (let i = 0; i < translated.length && i < romaResult.length; i++) {
              if (romaResult[i]) translated[i].romaji = romaResult[i];
            }
          }
        } else {
          // Sequential: translate first, then romanize (LLM-only or no romaji)
          translated = await this._translateBatch(cleanSegments);
          if (shouldRomanize) {
            try {
              await this._romanizeBatch(translated);
            } catch (e) { /* romanization is optional */ }
          }
        }
      }
      const transTime = ((performance.now() - tTrans) / 1000).toFixed(1);

      // 4. Store fully translated segments (replaces source-only placeholders)
      this.subtitleChunks[chunkIndex] = translated;
      this.fetchedChunks.add(chunkIndex);
      this.fetchingChunks.delete(chunkIndex);

      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      const preview = translated[0]?.original?.substring(0, 20) || '';
      const progress = `[${this.fetchedChunks.size}/${this.totalChunks}]`;
      const transOk = translated.filter(s => s.english && s.english.length > 0).length;
      const transTotal = translated.length;
      DEBUG.success('AudioCapture', `Chunk ${chunkIndex} ready: ${transTotal} segs (${transOk} translated) in ${elapsed}s ${progress}`);
      const cacheTag = wasCached ? ' [cached]' : '';
      const transTag = transOk < transTotal ? ` [${transOk}/${transTotal} translated]` : '';
      this._addDiag(chunkIndex, 'ok', translated.length, elapsed,
        `w:${whisperTime}s t+r:${transTime}s "${preview}"${cacheTag}${transTag}`);
      this._dispatchProgress();
      this._checkAndSignalReady(chunkIndex);

    } catch (error) {
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      this._addDiag(chunkIndex, 'error', 0, elapsed, error.message);
      // Graceful degradation: mark chunk as fetched with source-only placeholders
      // so original text is still visible and the pipeline doesn't retry endlessly
      this.fetchedChunks.add(chunkIndex);
      this.fetchingChunks.delete(chunkIndex);
      this._dispatchProgress();
      if (chunkIndex === 0) {
        window.dispatchEvent(new CustomEvent('display-error', {
          detail: { message: `Translation failed: ${error.message}. Showing original text only.` }
        }));
      }
    }
  }

  // Phase 1: Transcribe only (Whisper server). Returns segments or null.
  // Uses shared _transcribeAndFilter — no duplicated logic.
  async _transcribeOnly(chunkIndex) {
    return this._transcribeAndFilter(chunkIndex);
  }

  // Phase 2: Translate + romanize + store. Runs concurrently with next transcription.
  // gen parameter ensures stale promises from old videos are silently dropped.
  // Translation and romanization run IN PARALLEL — romaji only needs original text.
  async _translateAndFinalize(chunkIndex, cleanSegments, whisperTime, t0, gen, wasCached = false) {
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
      const srcLang = pSettings?.sourceLanguage || this.sourceLanguage || 'ja';
      const isDeterministic = DETERMINISTIC_ROMA_LANGS.has(srcLang);
      const shouldRomanize = isDeterministic || pSettings?.showRomaji;

      let translated;
      if (pSettings?.showEnglish === false) {
        translated = placeholders;  // Already in correct format
      } else {
        const canParallelRoma = shouldRomanize && _deterministicRomaAvailable === true;

        if (canParallelRoma) {
          const origTexts = cleanSegments.map(seg => seg.text);
          const [batchResult, romaResult] = await Promise.all([
            this._translateBatch(cleanSegments),
            this._romanizeTexts(origTexts, srcLang)
          ]);
          translated = batchResult;
          if (romaResult) {
            for (let i = 0; i < translated.length && i < romaResult.length; i++) {
              if (romaResult[i]) translated[i].romaji = romaResult[i];
            }
          }
        } else {
          translated = await this._translateBatch(cleanSegments);
          if (shouldRomanize) {
            try {
              await this._romanizeBatch(translated);
            } catch (e) { /* romanization is optional */ }
          }
        }
      }
      const transTime = ((performance.now() - tTrans) / 1000).toFixed(1);

      // Re-check generation after async translation
      if (gen !== this.generation) { this.fetchingChunks.delete(chunkIndex); return; }

      // Also romanize placeholders-only path (translation disabled but romaji enabled)
      if (pSettings?.showEnglish === false && shouldRomanize) {
        try {
          await this._romanizeBatch(translated);
        } catch (e) { /* optional */ }
      }

      // Final generation check before storing
      if (gen !== this.generation) { this.fetchingChunks.delete(chunkIndex); return; }

      // Update with full translated segments (replaces source-only placeholders)
      this.subtitleChunks[chunkIndex] = translated;
      this.fetchedChunks.add(chunkIndex);
      this.fetchingChunks.delete(chunkIndex);

      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      const preview = translated[0]?.original?.substring(0, 20) || '';
      const progress = `[${this.fetchedChunks.size}/${this.totalChunks}]`;
      const transOk = translated.filter(s => s.english && s.english.length > 0).length;
      const transTotal = translated.length;
      DEBUG.success('AudioCapture', `Chunk ${chunkIndex} ready: ${transTotal} segs (${transOk} translated) in ${elapsed}s ${progress} [parallel]`);
      const cacheTag = wasCached ? ' [cached]' : '';
      const transTag = transOk < transTotal ? ` [${transOk}/${transTotal} translated]` : '';
      this._addDiag(chunkIndex, 'ok', translated.length, elapsed,
        `w:${whisperTime}s t+r:${transTime}s "${preview}"${cacheTag}${transTag}`);
      this._dispatchProgress();
      this._checkAndSignalReady(chunkIndex);

    } catch (error) {
      if (gen !== this.generation) return; // stale, ignore

      // Translation-only retry: don't re-transcribe, just retry the translation.
      // The chunk stays in fetchingChunks during retries so the pipeline doesn't
      // waste a Whisper inference re-transcribing it.
      if (!this._retryCount) this._retryCount = {};
      this._retryCount[chunkIndex] = (this._retryCount[chunkIndex] || 0) + 1;

      if (this._retryCount[chunkIndex] < 3) {
        const attempt = this._retryCount[chunkIndex];
        const delay = 2000 * attempt;
        const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
        this._addDiag(chunkIndex, 'retry', 0, elapsed,
          `Translate retry ${attempt}/3 in ${delay / 1000}s: ${error.message}`);
        // Keep in fetchingChunks — retry translation only (no re-transcription)
        setTimeout(() => {
          this._translateAndFinalize(chunkIndex, cleanSegments, whisperTime, t0, gen, wasCached);
        }, delay);
      } else {
        // All retries exhausted — store source-only segments so pipeline completes
        const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
        const sourceOnly = cleanSegments.map(seg => ({
          start: seg.start, end: seg.end,
          original: seg.text, english: '', romaji: '',
          confidence: seg.confidence || 0
        }));
        this.subtitleChunks[chunkIndex] = sourceOnly;
        this.fetchedChunks.add(chunkIndex);
        this.fetchingChunks.delete(chunkIndex);
        delete this._retryCount[chunkIndex];
        this._addDiag(chunkIndex, 'error', cleanSegments.length, elapsed,
          `Translation failed after 3 retries: ${error.message} — keeping originals`);
        this._dispatchProgress();
        this._checkAndSignalReady(chunkIndex);
      }
    }
  }

  _checkAndSignalReady(_chunkIndex) {
    // Signal ready once any chunk with actual subtitles is stored.
    // This covers the case where the first chunk was empty (instrumental intro).
    // Must check for real subtitle content — fetchedChunks includes empty chunks.
    if (!this._readySignaled) {
      const hasSubtitles = Array.from(this.fetchedChunks).some(i =>
        this.subtitleChunks[i] && this.subtitleChunks[i].length > 0
      );
      if (hasSubtitles) this._signalReady();
    }
  }

  _signalReady() {
    if (this._readySignaled) return;
    this._readySignaled = true;
    window.dispatchEvent(new CustomEvent('prefetch-ready', { detail: { chunkIndex: 0 } }));
  }

  _dispatchProgress() {
    // Count fully processed chunks and romanized chunks
    // Only count chunks in fetchedChunks (not placeholders)
    let translatedCount = 0;
    let romanizedCount = 0;
    for (const idx of this.fetchedChunks) {
      const segments = this.subtitleChunks[idx];
      if (!segments || segments.length === 0) continue;
      if (segments.some(seg => seg.english && seg.english.length > 0)) {
        translatedCount++;
      }
      if (segments.some(seg => seg.romaji && seg.romaji.length > 0)) {
        romanizedCount++;
      }
    }
    window.dispatchEvent(new CustomEvent('chunk-progress', {
      detail: {
        fetched: this.fetchedChunks.size,
        total: this.totalChunks,
        complete: this.fetchedChunks.size >= this.totalChunks,
        translated: translatedCount,
        romanized: romanizedCount
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
      // Only save chunks that are fully processed (have translations if they have text)
      // This prevents saving placeholders with english='' before translation completes
      const cleanChunks = {};
      for (const [idx, segments] of Object.entries(this.subtitleChunks)) {
        if (!segments || segments.length === 0) {
          cleanChunks[idx] = segments; // empty chunks are fine to save
          continue;
        }
        // Check if any segment has original text but no translation — that's a placeholder
        const hasUntranslated = segments.some(seg => seg.original && !seg.english);
        if (!hasUntranslated) {
          cleanChunks[idx] = segments;
        }
        // Skip placeholder chunks — they'll be re-processed on restore
      }

      const data = {
        subtitleChunks: cleanChunks,
        fetchedChunks: [...this.fetchedChunks].filter(i => cleanChunks[i] !== undefined),
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

      // Reject sessions where all chunks are empty — these are stale from
      // bad cache hits and should be re-transcribed fresh
      const nonEmptyChunks = data.fetchedChunks.filter(i => {
        const segs = data.subtitleChunks[i];
        return segs && segs.length > 0;
      });
      if (nonEmptyChunks.length === 0 && data.fetchedChunks.length > 2) {
        DEBUG.warn('AudioCapture', `Session has ${data.fetchedChunks.length} chunks but all empty — discarding`);
        chrome.storage.session.remove(sessionKey).catch(() => {});
        return false;
      }

      // Reject sessions where chunks have text but no translations — these
      // are placeholders saved before translation completed
      const hasUntranslated = nonEmptyChunks.some(i => {
        const segs = data.subtitleChunks[i];
        return segs.some(seg => seg.original && !seg.english);
      });
      if (hasUntranslated) {
        DEBUG.warn('AudioCapture', 'Session has untranslated segments — discarding to re-process');
        chrome.storage.session.remove(sessionKey).catch(() => {});
        return false;
      }

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
    for (const [_idx, segments] of Object.entries(this.subtitleChunks)) {
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

    // If batch returned too few OR has empty slots, fill gaps
    const hasGaps = translations.length < segments.length ||
                    translations.some((t, i) => i < segments.length && (!t || t.length === 0));
    if (hasGaps) {
      const filled = translations.filter(t => t && t.length > 0).length;
      const gaps = segments.length - filled;
      DEBUG.info('AudioCapture', `Batch got ${filled}/${segments.length} translations, ${gaps} gaps`);

      // Only do individual calls if there are 1-3 gaps (partial batch success).
      // If the batch completely failed (0 results) or has many gaps, the LLM is
      // struggling — individual calls will likely fail too. Accept partial results.
      if (gaps <= 3 && filled > 0) {
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

  // Probe whether the Whisper server has deterministic romanization libraries installed.
  // Called once at startup. Result cached in module-level _deterministicRomaAvailable.
  async _probeDeterministicRoma() {
    // If probe succeeded, keep result forever. If it failed, re-probe after 30s
    // (server may have finished loading since the initial probe).
    if (_deterministicRomaAvailable === true) return;
    if (_deterministicRomaAvailable === false && Date.now() - _deterministicRomaProbeTime < 30000) return;
    const srcLang = this.sourceLanguage || 'ja';

    // Korean, Russian: always client-side
    if (srcLang === 'ko' || srcLang === 'ru') {
      _deterministicRomaAvailable = true;
      const engines = { ko: 'Hangul decomposition', ru: 'Cyrillic table' };
      DEBUG.info('AudioCapture', `Romanization: ${engines[srcLang]} (client-side, instant)`);
      return;
    }

    // Japanese: WanaKana handles kana, but kanji needs server (pykakasi).
    // Test with a kanji-containing string to see if server can resolve it.
    if (srcLang === 'ja') {
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH', texts: ['東京'], sourceLang: 'ja'
        });
        if (response?.success && response.romanizations?.[0] &&
            !this._hasKanji(response.romanizations[0])) {
          _deterministicRomaAvailable = true;
          DEBUG.info('AudioCapture', 'Romanization: WanaKana + pykakasi (instant, full kanji support)');
        } else {
          // Server can't handle kanji — need LLM for kanji parts (sequential after translation)
          _deterministicRomaAvailable = false; _deterministicRomaProbeTime = Date.now();
          DEBUG.info('AudioCapture', 'Romanization: WanaKana (kana only) + LLM for kanji (after translation)');
        }
      } catch (e) {
        _deterministicRomaAvailable = false; _deterministicRomaProbeTime = Date.now();
        DEBUG.info('AudioCapture', 'Romanization: WanaKana (kana only) + LLM for kanji (after translation)');
      }
      return;
    }

    // Chinese: needs server pypinyin
    if (srcLang === 'zh') {
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH', texts: ['测试'], sourceLang: 'zh'
        });
        _deterministicRomaAvailable = !!(response?.success &&
          response.romanizations?.[0]?.length > 0);
        DEBUG.info('AudioCapture', `Pinyin: ${_deterministicRomaAvailable ? 'available (pypinyin)' : 'LLM fallback'}`);
      } catch (e) {
        _deterministicRomaAvailable = false; _deterministicRomaProbeTime = Date.now();
      }
      return;
    }

    // Arabic or other: LLM only
    _deterministicRomaAvailable = false; _deterministicRomaProbeTime = Date.now();
  }

  // Check if text contains CJK kanji characters (U+4E00-U+9FFF, U+3400-U+4DBF)
  _hasKanji(text) {
    return /[\u4e00-\u9fff\u3400-\u4dbf]/.test(text);
  }

  // Client-side Japanese romanization using WanaKana (bundled, instant, no server).
  // Converts kana (hiragana/katakana) to romaji. Kanji left as-is.
  // Returns array of { romaji, complete } objects.
  _romanizeJapaneseLocal(texts) {
    if (typeof wanakana === 'undefined' || !wanakana.toRomaji) {
      DEBUG.warn('AudioCapture', 'WanaKana not loaded');
      return texts.map(() => ({ romaji: '', complete: false }));
    }
    return texts.map(text => {
      try {
        const result = wanakana.toRomaji(text);
        return { romaji: result, complete: !this._hasKanji(result) };
      } catch (e) {
        return { romaji: '', complete: false };
      }
    });
  }

  // Client-side Korean romanization using Unicode Hangul decomposition.
  // Implements Revised Romanization (official South Korean standard).
  // Instant (<1ms), no server call needed.
  _romanizeKoreanLocal(texts) {
    // Revised Romanization mapping tables
    const INITIALS = ['g','kk','n','d','tt','r','m','b','pp','s','ss','','j','jj','ch','k','t','p','h'];
    const MEDIALS = ['a','ae','ya','yae','eo','e','yeo','ye','o','wa','wae','oe','yo','u','wo','we','wi','yu','eu','ui','i'];
    const FINALS = ['','k','k','k','n','n','n','t','l','l','l','l','l','l','l','l','m','p','p','t','t','ng','t','t','k','t','p','t'];

    return texts.map(text => {
      const result = [];
      for (const ch of text) {
        const code = ch.charCodeAt(0);
        if (code >= 0xAC00 && code <= 0xD7A3) {
          const offset = code - 0xAC00;
          const initial = Math.floor(offset / (21 * 28));
          const medial = Math.floor((offset % (21 * 28)) / 28);
          const final = offset % 28;
          result.push(INITIALS[initial] + MEDIALS[medial] + FINALS[final]);
        } else {
          result.push(ch);
        }
      }
      return result.join('');
    });
  }

  // Client-side Russian transliteration (BGN/PCGN standard).
  // Simple 1:1 Cyrillic → Latin mapping. Instant, no server call.
  _romanizeRussianLocal(texts) {
    const MAP = {
      'а':'a','б':'b','в':'v','г':'g','д':'d','е':'ye','ё':'yo','ж':'zh','з':'z','и':'i',
      'й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t',
      'у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'',
      'э':'e','ю':'yu','я':'ya',
      'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'Ye','Ё':'Yo','Ж':'Zh','З':'Z','И':'I',
      'Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R','С':'S','Т':'T',
      'У':'U','Ф':'F','Х':'Kh','Ц':'Ts','Ч':'Ch','Ш':'Sh','Щ':'Shch','Ъ':'','Ы':'Y','Ь':'',
      'Э':'E','Ю':'Yu','Я':'Ya'
    };
    return texts.map(text => {
      return [...text].map(ch => MAP[ch] !== undefined ? MAP[ch] : ch).join('');
    });
  }

  // Romanize an array of texts — returns an array of romaji strings (or empty strings).
  // For Japanese: uses WanaKana client-side (instant, <1ms).
  // For Korean: uses Hangul decomposition client-side (instant).
  // For Russian: uses Cyrillic transliteration client-side (instant).
  // For zh: uses server deterministic endpoint (pypinyin).
  // For ar or fallback: returns empty (caller should use _romanizeBatch after translation).
  async _romanizeTexts(texts, sourceLang) {
    if (!texts || texts.length === 0) return [];
    const lang = sourceLang || this.sourceLanguage || 'ja';

    // Japanese: WanaKana first (instant), then server/LLM for remaining kanji
    if (lang === 'ja') {
      const wkResults = this._romanizeJapaneseLocal(texts);
      const results = wkResults.map(r => r.romaji);
      const hasIncomplete = wkResults.some(r => !r.complete);

      if (!hasIncomplete) return results; // Pure kana — done instantly

      // Some texts have kanji — try server (pykakasi) for full romanization
      const kanjiIndices = [];
      const kanjiTexts = [];
      for (let i = 0; i < wkResults.length; i++) {
        if (!wkResults[i].complete) {
          kanjiIndices.push(i);
          kanjiTexts.push(texts[i]);
        }
      }
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH', texts: kanjiTexts, sourceLang: 'ja'
        });
        if (response?.success && response.romanizations) {
          for (let j = 0; j < kanjiIndices.length; j++) {
            const serverRoma = (response.romanizations[j] || '').trim();
            if (serverRoma && !this._hasKanji(serverRoma)) {
              results[kanjiIndices[j]] = serverRoma;
            }
          }
        }
      } catch (e) {
        // Server unavailable (no pykakasi) — WanaKana partials remain
        // _romanizeBatch after translation will do LLM refinement for these
      }
      return results;
    }

    // Korean: Hangul decomposition client-side — instant (<1ms)
    if (lang === 'ko') return this._romanizeKoreanLocal(texts);

    // Russian: Cyrillic transliteration client-side — instant (<1ms)
    if (lang === 'ru') return this._romanizeRussianLocal(texts);

    // Chinese: server deterministic endpoint (pypinyin)
    if (lang === 'zh') {
      const results = new Array(texts.length).fill('');
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH', texts, sourceLang: lang
        });
        if (response?.success && response.romanizations) {
          for (let i = 0; i < texts.length && i < response.romanizations.length; i++) {
            results[i] = (response.romanizations[i] || '').trim();
          }
        }
      } catch (err) {
        DEBUG.warn('AudioCapture', 'Chinese romanization failed', { error: err.message });
      }
      return results;
    }

    // Arabic or other: return empty — caller uses _romanizeBatch (LLM) after translation
    return new Array(texts.length).fill('');
  }

  // Separate romanization call — mutates segments[i].romaji in place.
  // ja: WanaKana + server/LLM fallback for kanji. ko/ru: client-side. zh: server. ar: LLM.
  async _romanizeBatch(segments) {
    if (segments.length === 0) return;
    const lang = this.sourceLanguage || 'ja';
    const texts = segments.map(seg => seg.original || '');

    // Japanese: WanaKana first, then batch server/LLM for kanji
    if (lang === 'ja') {
      const wkResults = this._romanizeJapaneseLocal(texts);
      const needsServer = [];
      const needsServerIdx = [];
      for (let i = 0; i < segments.length; i++) {
        if (wkResults[i].complete) {
          segments[i].romaji = wkResults[i].romaji;
        } else {
          needsServer.push(texts[i]);
          needsServerIdx.push(i);
        }
      }
      if (needsServer.length === 0) return;

      // Single ROMANIZE_BATCH call handles both deterministic (pykakasi) and
      // LLM fallback. background.js tries server first, falls back to batch LLM.
      // This replaces the old N sequential individual ROMANIZE calls.
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH', texts: needsServer, sourceLang: 'ja'
        });
        if (response?.success && response.romanizations) {
          for (let j = 0; j < needsServerIdx.length; j++) {
            const roma = (response.romanizations[j] || '').trim();
            if (roma && !this._hasKanji(roma)) {
              segments[needsServerIdx[j]].romaji = roma;
            } else {
              // Keep WanaKana partial as last resort
              segments[needsServerIdx[j]].romaji = wkResults[needsServerIdx[j]].romaji;
            }
          }
          return;
        }
      } catch (e) {
        // Server + LLM both failed — use WanaKana partials
        DEBUG.warn('AudioCapture', 'Romanization batch failed', { error: e.message });
      }

      // Absolute fallback: WanaKana partials for everything
      for (let k = 0; k < needsServerIdx.length; k++) {
        segments[needsServerIdx[k]].romaji = wkResults[needsServerIdx[k]].romaji;
      }
      return;
    }

    // Korean / Russian: client-side instant
    if (lang === 'ko' || lang === 'ru') {
      const fn = lang === 'ko' ? this._romanizeKoreanLocal : this._romanizeRussianLocal;
      const results = fn.call(this, texts);
      for (let i = 0; i < segments.length; i++) {
        if (results[i]) segments[i].romaji = results[i];
      }
      return;
    }

    // Other languages (zh/ar/etc): single ROMANIZE_BATCH call
    // background.js handles deterministic vs LLM fallback
    try {
      const response = await this._sendMessage({
        type: 'ROMANIZE_BATCH',
        texts,
        sourceLang: lang
      });
      if (response?.success && response.romanizations) {
        for (let i = 0; i < segments.length; i++) {
          const roma = (response.romanizations[i] || '').trim();
          if (roma) segments[i].romaji = roma;
        }
        return;
      }
    } catch (err) {
      DEBUG.warn('AudioCapture', 'Batch romanization failed, falling back to sequential');
    }

    // Fallback: sequential LLM (for ru/ar, or if server batch fails)
    for (const seg of segments) {
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE',
          text: seg.original,
          sourceLang: lang
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
  // For Japanese: uses WanaKana client-side (instant for all chunks).
  // For others: uses batch server endpoint per chunk.
  async reRomanizeCachedChunks() {
    DEBUG.info('AudioCapture', 'Re-romanizing cached chunks');
    const lang = this.sourceLanguage || 'ja';

    for (const [idx, segments] of Object.entries(this.subtitleChunks)) {
      const needsRoma = [];
      const needsIndices = [];
      for (let i = 0; i < segments.length; i++) {
        if (!segments[i].romaji && segments[i].original) {
          needsRoma.push(segments[i].original);
          needsIndices.push(i);
        }
      }
      if (needsRoma.length === 0) continue;

      // Japanese: WanaKana for kana, then server/LLM for kanji
      if (lang === 'ja') {
        const wkResults = this._romanizeJapaneseLocal(needsRoma);
        const stillNeeds = [];
        const stillNeedsIdx = [];
        for (let j = 0; j < needsIndices.length; j++) {
          if (wkResults[j].complete) {
            segments[needsIndices[j]].romaji = wkResults[j].romaji;
          } else {
            stillNeeds.push(needsRoma[j]);
            stillNeedsIdx.push(needsIndices[j]);
            // Set WanaKana partial as temporary (better than nothing while LLM works)
            segments[needsIndices[j]].romaji = wkResults[j].romaji;
          }
        }
        // Try server then LLM for kanji segments
        if (stillNeeds.length > 0) {
          try {
            const response = await this._sendMessage({
              type: 'ROMANIZE_BATCH', texts: stillNeeds, sourceLang: 'ja'
            });
            if (response?.success && response.romanizations) {
              for (let k = 0; k < stillNeedsIdx.length; k++) {
                const roma = (response.romanizations[k] || '').trim();
                if (roma && !this._hasKanji(roma)) {
                  segments[stillNeedsIdx[k]].romaji = roma;
                }
              }
            }
          } catch (e) { /* server unavailable */ }
        }
        continue;
      }

      // Korean / Russian: client-side instant
      if (lang === 'ko' || lang === 'ru') {
        const fn = lang === 'ko' ? this._romanizeKoreanLocal : this._romanizeRussianLocal;
        const results = fn.call(this, needsRoma);
        for (let j = 0; j < needsIndices.length; j++) {
          if (results[j]) segments[needsIndices[j]].romaji = results[j];
        }
        continue;
      }

      // Other languages: server batch
      try {
        const response = await this._sendMessage({
          type: 'ROMANIZE_BATCH',
          texts: needsRoma,
          sourceLang: lang
        });
        if (response?.success && response.romanizations) {
          for (let j = 0; j < needsIndices.length; j++) {
            const roma = (response.romanizations[j] || '').trim();
            if (roma) segments[needsIndices[j]].romaji = roma;
          }
        }
      } catch (e) {
        DEBUG.warn('AudioCapture', `Re-romanize chunk ${idx} failed: ${e.message}`);
      }
    }
    // Force refresh current display — must clear lastShownSegment so the guard
    // in _showSubtitleAt doesn't skip the re-dispatch (same object ref, but romaji changed)
    this.lastShownSegment = null;
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

    // Remove storage listener to prevent leaks when AudioCapture is recreated
    if (this._onStorageChanged) {
      chrome.storage.onChanged.removeListener(this._onStorageChanged);
      this._onStorageChanged = null;
    }

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
    // Clear session storage so stale data doesn't restore on page reload
    try {
      if (this.videoId) {
        chrome.storage.session.remove(`yume_${this.videoId}`);
      }
    } catch (e) { /* session storage may not be available */ }
    // Clear displayed subtitle
    window.dispatchEvent(new CustomEvent('display-subtitle', {
      detail: { original: '', english: '', romaji: '' }
    }));
    console.log(`[AudioCapture] Content cache cleared (${chunkCount} chunks + session storage)`);
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
