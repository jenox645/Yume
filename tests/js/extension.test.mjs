// Unit tests for the extension's logic. The extension has no build step, so
// each script is evaluated as-is in a VM context with minimal browser stubs.
// Run: npm test

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../extension/js/', import.meta.url);
const noop = () => {};
const listener = { addListener: noop, removeListener: noop };

function load(file, { location = 'https://www.youtube.com/watch?v=abc123', video = null, title = '' } = {}) {
  const url = new URL(location);
  const events = [];
  const ctx = vm.createContext({
    console: { log: noop, warn: noop, error: noop },
    chrome: {
      runtime: { id: 'self', onMessage: listener, onInstalled: listener, sendMessage: noop, getManifest: () => ({ version: '0' }) },
      storage: { onChanged: listener, local: { get: async () => ({}), set: noop, remove: noop }, session: { get: async () => ({}), set: noop } },
      commands: { onCommand: listener },
      tabs: { query: async () => [], sendMessage: async () => ({}) },
    },
    setTimeout, clearTimeout, URLSearchParams, URL, AbortController, fetch: async () => { throw new Error('offline'); },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    document: { hidden: false, title, querySelector: () => video },
  });
  ctx.window = ctx;
  ctx.window.location = { href: url.href, hostname: url.hostname, pathname: url.pathname, search: url.search };
  ctx.window.dispatchEvent = (e) => events.push(e);
  vm.runInContext(readFileSync(new URL(file, ROOT), 'utf8'), ctx);
  return { ctx, events };
}

// ── session.js ─────────────────────────────────────────────────────────────

test('cueAt finds the last cue that started', () => {
  const { ctx } = load('session.js');
  const cues = [{ start: 1, end: 3 }, { start: 5, end: 9 }, { start: 8, end: 10 }];
  const at = (t) => ctx.SubtitleSession.cueAt(cues, t);
  assert.equal(at(0.5), null);
  assert.equal(at(2), cues[0]);
  assert.equal(at(4), cues[0]);  // started, caller checks the end
  assert.equal(at(8.5), cues[2]);  // newest started cue wins over a long one
});

test('videoId: YouTube id only on YouTube hosts; page identity elsewhere', () => {
  const id = (location, video = null) => load('session.js', { location, video }).ctx.SubtitleSession.videoId(video);
  assert.equal(id('https://www.youtube.com/watch?v=abc123&t=42s'), 'abc123');
  assert.equal(id('https://music.youtube.com/watch?v=xyz'), 'xyz');
  // Shorts, live and embeds are the same video as watch?v= (one cache entry)
  assert.equal(id('https://www.youtube.com/shorts/jdL68J2oprw', { duration: 59 }), 'jdL68J2oprw');
  assert.equal(id('https://www.youtube.com/live/6PNb0Jv6qbU?si=x'), '6PNb0Jv6qbU');
  assert.equal(id('https://www.youtube-nocookie.com/embed/0OIVIkmBdL8'), '0OIVIkmBdL8');
  assert.equal(id('https://youtu.be/0OIVIkmBdL8?t=30'), '0OIVIkmBdL8');
  // ?v= elsewhere is not a video id, and two sites never share an id
  assert.notEqual(id('https://site-a.com/watch?v=1'), '1');
  assert.notEqual(id('https://site-a.com/watch?v=1'), id('https://site-b.com/watch?v=1'));
  // content-selecting params matter, tracking/position params don't
  assert.notEqual(id('https://s.com/watch?id=1'), id('https://s.com/watch?id=2'));
  assert.equal(id('https://s.com/watch?id=1&utm_source=x&t=30'), id('https://s.com/watch?id=1'));
  // the media src (CDN tokens, blob: URLs) must not change the id
  assert.equal(id('https://s.com/v/9', { duration: 61.2, currentSrc: 'https://cdn/x.mp4?token=a' }),
               id('https://s.com/v/9', { duration: 61.2, currentSrc: 'blob:https://s.com/123' }));
});

test('the job carries the page title, without the YouTube count and suffix', () => {
  const title = (t) => load('session.js', { title: t }).ctx.SubtitleSession.pageTitle();
  assert.equal(title('(3) 周杰倫 Jay Chou【等你下課】 - YouTube'), '周杰倫 Jay Chou【等你下課】');
  assert.equal(title('(99+) Kun Anta - YouTube Music'), 'Kun Anta');
  assert.equal(title('Episode 4 - My Site'), 'Episode 4 - My Site');  // other sites: as is
  assert.equal(title(''), '');
});

test('snapshots merge, update and remove (hidden) segments', () => {
  const video = { currentTime: 2, duration: 100 };
  const { ctx, events } = load('session.js', { video });
  const s = new ctx.SubtitleSession();
  s.video = video;
  s.active = true;
  const seg = (id, start, end, text, extra = {}) => ({ id, start, end, text, translation: '', romaji: '', hidden: false, ...extra });
  s._apply({ status: 'transcribing', rev: 2, progress: { regions_done: 1, regions_total: 4 }, segments: [seg(2, 5, 7, 'b'), seg(1, 1, 3, 'a')] });
  assert.deepEqual([...s.cues].map((c) => c.text), ['a', 'b']);
  const shown = events.filter((e) => e.type === 'display-subtitle').pop();
  assert.equal(shown.detail.original, 'a');

  // translation arrives for the line on screen -> re-rendered
  s._apply({ status: 'translating', rev: 3, progress: {}, segments: [seg(1, 1, 3, 'a', { translation: 'A' })] });
  assert.equal(events.filter((e) => e.type === 'display-subtitle').pop().detail.english, 'A');

  // blacklisted -> removed and cleared from screen
  s._apply({ status: 'done', rev: 4, progress: {}, segments: [seg(1, 1, 3, 'a', { hidden: true })] });
  assert.deepEqual([...s.cues].map((c) => c.text), ['b']);
  assert.equal(events.filter((e) => e.type === 'display-subtitle').pop().detail.original, '');
  assert.equal(s.rev, 4);
});

test('a line stays readable for a short grace after it ends, not after a seek', () => {
  const video = { currentTime: 2 };
  const { ctx, events } = load('session.js', { video });
  const s = new ctx.SubtitleSession();
  s.video = video;
  s.active = true;
  s._apply({ status: 'done', rev: 1, progress: {}, segments: [{ id: 1, start: 1, end: 3, text: 'a', hidden: false }] });
  video.currentTime = 3.5;  // ended 0.5 s ago: keep it
  s._render();
  assert.equal(events.filter((e) => e.type === 'display-subtitle').pop().detail.original, 'a');
  video.currentTime = 0.2;  // seeked before it: clear
  s._render();
  assert.equal(events.filter((e) => e.type === 'display-subtitle').pop().detail.original, '');
});

// ── background.js ──────────────────────────────────────────────────────────

test('background only proxies known server paths', () => {
  const { ctx } = load('background.js');
  // top-level const is lexical, not a property of the context: evaluate it there
  const re = vm.runInContext('_ALLOWED_PATH', ctx);
  const ok = (p) => re.test(p);
  for (const p of ['/health', '/jobs', `/jobs/${'a'.repeat(32)}`, `/jobs/${'b'.repeat(32)}/export`,
    '/library', '/library/export', '/blacklist/add', '/model/switch', '/translation/health', '/cache/clear']) {
    assert.ok(ok(p), p);
  }
  for (const p of ['/', '/jobs/../health', '/jobs/xyz', '/admin', 'http://evil/x', '/health?x=1', '/blacklist/']) {
    assert.ok(!ok(p), p);
  }
});

test('a recreated job (after a 404) clears the old cues', async () => {
  const video = { currentTime: 2 };
  const { ctx, events } = load('session.js', { video });
  let body = null;
  ctx.chrome.runtime.sendMessage = (msg, cb) => {
    body = msg.request.body;
    cb({ ok: true, status: 200, data: { id: 'new', status: 'downloading', rev: 0, progress: {}, segments: [] } });
  };
  const s = new ctx.SubtitleSession();
  s.video = video;
  s.active = true;
  s.params = { video_id: 'abc123' };
  s._apply({ status: 'done', rev: 5, progress: {}, segments: [{ id: 1, start: 1, end: 3, text: 'old', hidden: false }] });
  await s._createJob();
  assert.equal(s.cues.length, 0);
  assert.equal(s.jobId, 'new');
  // where playback is, so a video resumed at 40:00 is transcribed from there first
  assert.equal(body.t, 2);
  assert.equal(body.video_id, 'abc123');
  assert.equal(events.filter((e) => e.type === 'display-subtitle').pop().detail.original, '');
});

// ── session.js: one-click start ──────────────────────────────────────────────

// A session whose server calls and helper commands come from `script`, with a
// fake clock (each 2 s wait advances it) so start-up timeouts run instantly.
function startupSession(script) {
  const { ctx, events } = load('session.js');
  vm.runInContext('globalThis.__now = 0; Date.now = () => globalThis.__now;', ctx);
  ctx.setTimeout = (fn, ms) => { ctx.__now += ms || 0; fn(); };
  const sent = [];
  ctx.chrome.runtime.sendMessage = (msg, cb) => {
    sent.push(msg.type === 'YUME' ? `YUME:${msg.cmd}` : `API:${msg.request.path}`);
    cb(script(msg, sent));
  };
  const s = new ctx.SubtitleSession();
  return { s, sent, events };
}
const offline = { ok: false, status: 0, data: { error: 'Yume server not reachable' } };

test('enable starts Yume through the helper and waits for it', async () => {
  let health = 0;
  const { s, sent, events } = startupSession((msg) => {
    if (msg.type === 'YUME') return msg.cmd === 'start' ? { ok: true, state: 'starting' } : { ok: true, state: 'starting', message: 'Loading the speech model…' };
    health++;
    if (health <= 3) return offline;
    if (health === 4) return { ok: true, status: 200, data: { status: 'loading' } };
    return { ok: true, status: 200, data: { status: 'ready' } };
  });
  await s._waitForServer(() => false);
  assert.deepEqual(sent, ['API:/health', 'YUME:start', 'API:/health', 'YUME:status', 'API:/health', 'YUME:status', 'API:/health', 'API:/health']);
  const msgs = events.filter((e) => e.type === 'display-status').map((e) => e.detail.message);
  assert.ok(msgs.includes('Starting Yume...'));
  assert.ok(msgs.some((m) => m.startsWith('Loading the speech model... (')));
});

test('without the helper, enable explains how to start Yume', async () => {
  const { s } = startupSession((msg) => (msg.type === 'YUME' ? { ok: false, unavailable: true } : offline));
  await assert.rejects(s._waitForServer(() => false), /START_YUME.*autostart on/);
});

test('a failed background start surfaces its error', async () => {
  const { s } = startupSession((msg) => {
    if (msg.type !== 'YUME') return offline;
    return msg.cmd === 'start' ? { ok: true, state: 'starting' } : { ok: true, state: 'stopped', last_error: 'no .gguf translation model' };
  });
  await assert.rejects(s._waitForServer(() => false), /Yume could not start: no \.gguf translation model/);
});

test('a server error other than "unreachable" is not masked by a start attempt', async () => {
  const { s, sent } = startupSession(() => ({ ok: false, status: 500, data: { error: 'boom' } }));
  await assert.rejects(s._waitForServer(() => false), /boom/);
  assert.ok(!sent.includes('YUME:start'));
});

test('polling tells the user when the server stops answering, and keeps polling', async () => {
  const { ctx, events } = load('session.js', { video: { currentTime: 0, duration: 60 } });
  ctx.chrome.runtime.sendMessage = (_msg, cb) => cb({ ok: false, status: 0, data: { error: 'not reachable' } });
  const s = new ctx.SubtitleSession();
  s.jobId = 'a'.repeat(32);
  let scheduled = 0;
  s._schedulePoll = () => { scheduled++; };
  for (let i = 0; i < 4; i++) await s._poll(s.generation);
  const msgs = events.filter((e) => e.type === 'display-status').map((e) => e.detail.message);
  assert.equal(msgs.filter((m) => /Lost contact/.test(m)).length, 1);  // said once, not every poll
  assert.equal(scheduled, 4);
});

test('a job whose every region failed is reported, not shown as "no vocals"', () => {
  const { ctx, events } = load('session.js', { video: { currentTime: 0, duration: 60 } });
  const s = new ctx.SubtitleSession();
  s._apply({ status: 'done', rev: 1, progress: { regions_total: 3, regions_done: 3, regions_failed: 3 }, segments: [] });
  const last = events.filter((e) => e.type === 'display-status').pop().detail;
  assert.match(last.message, /Transcription failed/);
  assert.equal(last.type, 'error');
});

test('a positive timing offset shows lines LATER (as the popup says)', () => {
  const video = { currentTime: 10.5, duration: 60 };
  const { ctx, events } = load('session.js', { video });
  const s = new ctx.SubtitleSession();
  s.video = video;
  s.active = true;
  const seg = (id, start, end, text) => ({ id, start, end, text, translation: '', romaji: '', hidden: false });
  s._apply({ status: 'done', rev: 2, progress: {}, segments: [seg(1, 5, 10, 'first'), seg(2, 10, 15, 'second')] });
  const shown = () => events.filter((e) => e.type === 'display-subtitle').pop().detail.original;
  assert.equal(shown(), 'second');
  s.timingOffset = 1;  // +1 s = later: at 10.5 s the line due at 10 s is still 0.5 s away
  s._render(true);
  assert.equal(shown(), 'first');
});

test('refresh() polls right away (blacklist edits show at once)', () => {
  const { ctx } = load('session.js');
  const s = new ctx.SubtitleSession();
  const polls = [];
  s._schedulePoll = (delay) => polls.push(delay);
  s.refresh();
  assert.deepEqual(polls, []);  // not running: nothing to refresh
  s.active = true;
  s.jobId = 'a'.repeat(32);
  s.refresh();
  assert.deepEqual(polls, [0]);
});

// ── content.js: start / stop ordering ────────────────────────────────────────

test('Enable pressed twice while the video loads runs ONE session; Disable during the wait wins', async () => {
  const sessions = [];
  const onMessage = [];
  let videos = [];  // what document.querySelectorAll('video') returns
  const video = { readyState: 4, getBoundingClientRect: () => ({ width: 640, height: 360 }) };
  const timers = [];
  const ctx = vm.createContext({
    console: { log: noop, warn: noop, error: noop },
    chrome: {
      runtime: { id: 'self', onMessage: { addListener: (f) => onMessage.push(f) } },
      storage: { onChanged: listener },
    },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    setInterval: noop, clearTimeout: noop, URL,
    SubtitleWindow: class { resetProgress() {} updateStatus() {} showError() {} close() {} },
    SubtitleSession: class {
      constructor() { this.stopped = false; sessions.push(this); }
      start() { return new Promise(noop); }  // still waiting for the server
      stop() { this.stopped = true; }
    },
    document: { querySelectorAll: () => videos, contains: (v) => videos.includes(v), addEventListener: noop },
  });
  ctx.window = ctx;
  ctx.window.location = { href: 'https://www.youtube.com/watch?v=a' };
  ctx.window.addEventListener = noop;
  vm.runInContext(readFileSync(new URL('content.js', ROOT), 'utf8'), ctx);
  const send = (action) => new Promise((resolve) => { onMessage[0]({ action }, {}, resolve); });
  const flushTimers = async () => { for (let i = 0; i < 5; i++) { const t = timers.splice(0); t.forEach((f) => f()); await new Promise((r) => setImmediate(r)); } };

  // 1) two starts overlap while no video is ready yet
  const first = send('TOGGLE_SUBTITLES');
  await new Promise((r) => setImmediate(r));
  videos = [video];
  await flushTimers();
  await first;
  const live = sessions.filter((s) => !s.stopped);
  assert.equal(live.length, 1);

  // 2) Disable while a start waits for the video: nothing starts afterwards
  await send('TOGGLE_SUBTITLES');  // stop the running one
  videos = [];  // the page swaps its player: the next start has to wait for one
  const before = sessions.length;
  const pending = send('TOGGLE_SUBTITLES');  // start: waiting for a video
  await new Promise((r) => setImmediate(r));
  await send('TOGGLE_SUBTITLES');  // the user changes their mind
  videos = [video];
  await flushTimers();
  await Promise.race([pending, new Promise((r) => setTimeout(r, 50))]);
  assert.equal(sessions.filter((s) => !s.stopped).length, 0);
  assert.equal(sessions.length, before);  // the cancelled start never created a session
});

// ── popup.js ───────────────────────────────────────────────────────────────

// The popup's settings form against fake controls (ids, min/max as in popup.html)
function loadPopup() {
  const controls = {};
  const control = (id, init = {}) => (controls[id] ??= { value: '', checked: false, min: '', max: '', style: {}, ...init });
  let stored = {};
  const ctx = vm.createContext({
    console: { log: noop, warn: noop, error: noop },
    chrome: {
      runtime: { sendMessage: noop, getManifest: () => ({ version: '0' }) },
      storage: { local: { get: async () => ({ settings: stored }), set: async (o) => { stored = o.settings; } } },
    },
    document: { addEventListener: noop, getElementById: (id) => control(id), querySelectorAll: () => [] },
    setInterval: noop, clearInterval: noop, setTimeout, clearTimeout,
  });
  vm.runInContext(readFileSync(new URL('../popup.js', ROOT), 'utf8'), ctx);
  control('glassBlur', { min: '0', max: '40' });
  control('glassRadius', { min: '0', max: '40' });
  control('originalFontSize', { min: '12', max: '36' });
  control('showRomaji', { closest: () => null });
  return { ctx, controls, stored: () => stored, setStored: (s) => { stored = s; } };
}

test('popup settings: a saved form loads back the same', async () => {
  const { ctx, controls, stored, setStored } = loadPopup();
  await ctx.loadSettings();  // nothing stored: the defaults
  assert.equal(controls.showEnglish.checked, true);
  assert.equal(controls.showRomaji.checked, false);
  assert.equal(controls.windowTitle.value, 'Yume');
  controls.glassBlur.value = '0';  // no blur: was saved as 18
  controls.glassRadius.value = '0';
  controls.showRomaji.checked = true;
  controls.windowTitle.value = '';  // cleared: back to the default
  await ctx.saveSettings(false);
  assert.equal(stored().glassBlur, 0);
  assert.equal(stored().glassRadius, 0);
  assert.equal(stored().showRomaji, true);
  assert.equal(stored().windowTitle, 'Yume');
  assert.equal(stored().whisperPort, undefined);  // the port is saved by savePorts
  setStored({ ...stored(), showEnglish: false });
  await ctx.loadSettings();
  assert.equal(controls.glassBlur.value, 0);
  assert.equal(controls.showEnglish.checked, false);
  assert.equal(controls.glassBlurVal.textContent, '0px');
});

test('popup settings: typed numbers are kept inside the control range', async () => {
  const { ctx, controls, stored } = loadPopup();
  await ctx.loadSettings();
  controls.originalFontSize.value = '0';
  await ctx.saveSettings(false);
  assert.equal(stored().originalFontSize, 12);
  controls.originalFontSize.value = '99';
  await ctx.saveSettings(false);
  assert.equal(stored().originalFontSize, 36);
  controls.originalFontSize.value = '';
  await ctx.saveSettings(false);
  assert.equal(stored().originalFontSize, 19);  // empty: the default
});

test('a server notice (wrong language picked) is sent once, and cleared', () => {
  const { ctx, events } = load('session.js');
  const s = new ctx.SubtitleSession();
  const snap = { status: 'transcribing', segments: [], progress: {}, rev: 1, notice: 'This video sounds like English' };
  s._apply(snap);
  const notices = () => events.filter((e) => e.type === 'display-notice').map((e) => e.detail.message);
  assert.deepEqual(notices(), ['This video sounds like English']);
  s._apply({ ...snap, rev: 2, segments: [{ id: 1, start: 0, end: 2, text: 'x' }] });
  assert.deepEqual(notices(), ['This video sounds like English']);  // not repeated once lines arrive
  s._apply({ ...snap, rev: 3, notice: '' });
  assert.deepEqual(notices(), ['This video sounds like English', '']);
});

test('the notice stays on screen while lines play (a status is hidden then)', () => {
  const { ctx } = load('subtitle-window.js');
  const els = {};
  const el = (sel) => (els[sel] ??= { style: {}, textContent: '', className: '', classList: { remove: noop } });
  const w = Object.assign(Object.create(ctx.SubtitleWindow.prototype), {
    settings: {}, isPlaying: false, element: { style: {}, querySelector: el },
  });
  w.updateSubtitle('原文', 'Line', '');
  w.showNotice('This video sounds like English');
  w.updateSubtitle('次', 'Next line', '');
  assert.equal(el('.subtitle-notice').style.display, 'block');
  assert.equal(el('.subtitle-notice').textContent, 'This video sounds like English');
  w.resetProgress();  // the next video
  assert.equal(el('.subtitle-notice').style.display, 'none');
});

// ── subtitle-window.js ─────────────────────────────────────────────────────

test('window colours keep a 00 channel (pure black was drawn as #08080c)', () => {
  const { ctx } = load('subtitle-window.js');
  assert.equal(ctx.windowStyle({ windowBgColor: '#000000', windowOpacity: 100 }).background, 'rgba(0, 0, 0, 1)');
  assert.equal(ctx.windowStyle({ windowBgColor: '#ff8000', glassEnabled: true }).background, 'rgba(255, 128, 0, 0.45)');
  assert.equal(ctx.windowStyle({}).background, 'rgba(8, 8, 12, 0.88)');  // the default
  assert.equal(ctx.windowStyle({ windowBgColor: 'nonsense' }).background, 'rgba(8, 8, 12, 0.88)');
});

test('a less sure line is drawn fainter, only when asked', () => {
  const { ctx } = load('subtitle-window.js');
  const at = (c, show = true) => ctx.confidenceOpacity(show, c);
  assert.deepEqual([at(-0.1), at(-0.5), at(-0.9), at(null), at(0.1), at(-0.9, false)], [1, 0.85, 0.65, 1, 1, 1]);
});

test('popup: a destructive button acts on the second click only', () => {
  const { ctx } = loadPopup();
  const btn = { textContent: 'Clear', dataset: {} };
  assert.equal(ctx.confirmedClick(btn, 'Click again to clear all'), false);  // one click wiped the blacklist
  assert.equal(btn.textContent, 'Click again to clear all');
  assert.equal(ctx.confirmedClick(btn, 'Click again to clear all'), true);
  assert.equal(btn.textContent, 'Clear');
  assert.equal(ctx.confirmedClick(btn, 'Click again to clear all'), false);  // armed again from scratch
});

test('popup: exports are named after the video', () => {
  const { ctx } = loadPopup();
  assert.equal(ctx.exportName('周杰倫 Jay Chou【等你下課】', 'srt'), 'yume-周杰倫_Jay_Chou【等你下課】.srt');
  assert.equal(ctx.exportName('a/b: c?', 'vtt'), 'yume-a_b_c.vtt');
  assert.match(ctx.exportName('', 'srt'), /^yume-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}\.srt$/);  // no title: the date
});

test('popup: a History/blacklist row action that fails says so', async () => {
  const { ctx } = loadPopup();
  const toasts = [];
  ctx.showToast = (message, type) => toasts.push([message, type]);
  ctx.chrome.runtime.sendMessage = (_msg, cb) => cb({ ok: false, status: 0, data: { error: 'Yume server not reachable' } });
  assert.equal(await ctx.serverAction({ method: 'POST', path: '/library/delete', body: {} }, 'Could not delete'), false);
  assert.deepEqual(toasts, [['Yume server not reachable', 'error']]);
});

test('popup: "Set & Enable" sets the stream URL and (re)starts the subtitles', async () => {
  for (const [active, expected] of [[false, ['GET_STATUS', 'start']], [true, ['GET_STATUS', 'TOGGLE_SUBTITLES', 'start']]]) {
    const { ctx } = loadPopup();
    const sent = [];
    ctx.sendToTab = async (m) => { sent.push(m.action); return { success: true, active }; };
    ctx.toggleSubtitles = async () => { sent.push('start'); };
    ctx.document.getElementById('customStreamUrl').value = ' https://cdn.example/a.m3u8 ';
    await ctx.setCustomStreamUrl();
    assert.deepEqual(sent, expected);
  }
});

test('popup: Switch Model stays disabled until the switch answers', async () => {
  const { ctx, controls } = loadPopup();
  let answer;
  ctx.chrome.runtime.sendMessage = (_msg, cb) => { answer = cb; };
  ctx.document.getElementById('whisperModelSelect').value = 'small';
  const pending = ctx.switchWhisperModel();
  assert.equal(controls.switchWhisperModel.disabled, true);  // a second click sent a second switch
  answer({ ok: true, status: 200, data: { status: 'switched', model: 'small' } });
  await pending;
  assert.equal(controls.switchWhisperModel.disabled, false);
});

test('popup: export on a tab without Yume says to reload it', async () => {
  const { ctx, controls } = loadPopup();
  ctx.sendToTab = async () => { throw new Error('Could not establish connection. Receiving end does not exist.'); };
  await ctx.exportSubtitles('srt');
  assert.match(controls.exportStatus.textContent, /reload the page/);
});

test('a session stopped while it reads its settings leaves no storage listener behind', async () => {
  const { ctx } = load('session.js');
  const live = new Set();
  ctx.chrome.storage.onChanged = { addListener: (f) => live.add(f), removeListener: (f) => live.delete(f) };
  let release;
  ctx.chrome.storage.local.get = () => new Promise((r) => { release = () => r({}); });
  const s = new ctx.SubtitleSession();
  const started = s.start({ duration: 60, currentTime: 0, addEventListener: noop, removeEventListener: noop });
  s.stop();  // Disable (or a navigation) while start() still waits for storage
  release();
  assert.equal(await started, false);
  assert.equal(live.size, 0);  // it was added after stop() removed it, and never removed again
});

test('a finished run fading its badge does not hide the next run\'s badge', () => {
  const { ctx } = load('subtitle-window.js');
  const timers = [];
  ctx.setTimeout = (fn) => timers.push(fn);
  const els = {};
  const el = (sel) => (els[sel] ??= {
    style: {}, textContent: '', className: '',
    classList: {
      contains: (c) => el(sel).className.split(' ').includes(c),
      add: (c) => { el(sel).className += ` ${c}`; },
      remove: noop,
    },
  });
  const w = Object.assign(Object.create(ctx.SubtitleWindow.prototype), {
    settings: {}, isPlaying: false, element: { style: {}, querySelector: el },
  });
  const badge = el('.chunk-badge');
  w.updateChunkProgress({ complete: true });
  timers.shift()();  // 3 s later: starts fading
  w.resetProgress();  // a new run starts (next video) before the fade ends
  timers.shift()();  // the fade's end
  assert.equal(badge.style.display, '');
  assert.equal(badge.className, 'chunk-badge active');
});

test('the badge says what is happening, not "Listening 1/1", while the preview waits for the rest', () => {
  const { ctx } = load('subtitle-window.js');
  const badge = { style: {}, textContent: '', title: '', className: '', classList: { contains: () => false } };
  const w = Object.assign(Object.create(ctx.SubtitleWindow.prototype), {
    settings: {}, element: { querySelector: () => badge },
  });
  // The stream preview installs one provisional region before the plan exists
  w.updateChunkProgress({ done: 1, total: 1, status: 'separating' });
  assert.equal(badge.textContent, 'Separating…');
  w.updateChunkProgress({ done: 1, total: 1, status: 'downloading' });
  assert.equal(badge.textContent, 'Downloading…');
  w.updateChunkProgress({ done: 2, total: 8, status: 'transcribing' });
  assert.equal(badge.textContent, 'Listening 2/8');
});
