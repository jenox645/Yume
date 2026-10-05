// Unit tests for the extension's logic. The extension has no build step, so
// each script is evaluated as-is in a VM context with minimal browser stubs.
// Run: npm test   (node --test tests/js/)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../extension/js/', import.meta.url);
const noop = () => {};
const listener = { addListener: noop, removeListener: noop };

function load(file, { location = 'https://www.youtube.com/watch?v=abc123', video = null } = {}) {
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
    document: { hidden: false, querySelector: () => video },
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
  ctx.chrome.runtime.sendMessage = (_msg, cb) => cb({ ok: true, status: 200, data: { id: 'new', status: 'downloading', rev: 0, progress: {}, segments: [] } });
  const s = new ctx.SubtitleSession();
  s.video = video;
  s.active = true;
  s.params = {};
  s._apply({ status: 'done', rev: 5, progress: {}, segments: [{ id: 1, start: 1, end: 3, text: 'old', hidden: false }] });
  await s._createJob();
  assert.equal(s.cues.length, 0);
  assert.equal(s.jobId, 'new');
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
