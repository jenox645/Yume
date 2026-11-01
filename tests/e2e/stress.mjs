// Stress test of the whole of Yume, the way people really use it: jumping between
// videos, going back, skipping around, switching things on and off while it works.
// The real extension in a real browser, the real server, Whisper on the GPU, the
// local LLM. Not part of CI. It uses the GPU for several minutes.
//
// Run: node tests/e2e/stress.mjs <out-dir> <lang> <id> <id> <id> [<id> <id> <id>]
//      The first three are talk, the first one long; three more, songs, for
//      `rapid-new-videos`, which needs them not yet in the cache.
//      HEADED=1 shows the browser; BROWSER=<path>, default Brave (see live.mjs);
//      ONLY=name,name runs some scenarios.
//
// YouTube's own player stops after ~45 s in an automated, signed-out browser, so
// the videos play on a stand-in page: it changes the URL without loading a page,
// as YouTube does, with the real URLs and titles and silent video of the same
// length. The server downloads and transcribes the real audio. The scenario
// `real-youtube` hops between real YouTube pages for as long as the player lets it.
//
// After every step it checks what must always hold: the line on screen belongs to
// the video in the address bar and to the moment it is at, and nothing crashed.

/* global location, URLSearchParams, window -- inside page.evaluate() */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const LOG = path.join(ROOT, 'logs', 'whisper_server.log');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const [outDir, lang, ...ids] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });
const only = process.env.ONLY ? process.env.ONLY.split(',') : null;

let context, worker, page;
const problems = [];
const notes = [];
const results = [];
let scenario = '';

const problem = (msg) => { problems.push(`[${scenario}] ${msg}`); console.log(`  PROBLEM ${msg}`); };
const note = (msg) => { notes.push(`[${scenario}] ${msg}`); console.log(`  ${msg}`); };

// ── the browser ───────────────────────────────────────────────────────────────

const toTab = async (message, tab = page) => {
  await tab.bringToFront();
  return worker.evaluate(async (message) => {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return chrome.tabs.sendMessage(t.id, message);
  }, message);
};
const server = (method, apiPath, body, timeout = 20000) => worker.evaluate(
  // eslint-disable-next-line no-undef
  (r) => serverRequest(r), { method, path: apiPath, body, timeout });
const setSettings = (changes) => worker.evaluate(async (changes) => {
  const { settings } = await chrome.storage.local.get('settings');
  await chrome.storage.local.set({ settings: { ...settings, ...changes } });
}, changes);

// The overlay is in a closed shadow root: read it through the DevTools protocol
async function overlay(tab = page) {
  const cdp = await context.newCDPSession(tab);
  const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
  await cdp.detach();
  const out = { windows: 0 };
  const walk = (n) => {
    const a = n.attributes || [];
    const attr = (name) => (a.indexOf(name) >= 0 && a.indexOf(name) % 2 === 0 ? a[a.indexOf(name) + 1] : '');
    const classes = attr('class').split(' ');
    if (classes.includes('subtitle-window')) out.windows++;
    const cls = classes.find((c) => /^(subtitle-(original|english|romaji|notice|status)|chunk-badge)$/.test(c));
    if (cls) {
      const text = (n.children || []).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join('');
      out[cls.replace('subtitle-', '').replace('chunk-', '')] = /display:\s*none/.test(attr('style')) ? '' : text;
    }
    for (const c of [...(n.children || []), ...(n.shadowRoots || []), ...(n.contentDocument ? [n.contentDocument] : [])]) walk(c);
  };
  walk(root);
  return out;
}

const player = (tab = page) => tab.evaluate(() => {
  const v = document.querySelector('video');
  return v ? { t: v.currentTime, d: v.duration, paused: v.paused, id: new URLSearchParams(location.search).get('v') } : null;
});
const seek = (t, tab = page) => tab.evaluate((t) => { document.querySelector('video').currentTime = t; }, t);

// ── the stand-in for YouTube's page ───────────────────────────────────────────

const videos = {};

function silentWav(seconds) {
  const n = Math.ceil(seconds) * 8000;
  const b = Buffer.alloc(44 + n, 128);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n, 4); b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(8000, 24); b.writeUInt32LE(8000, 28); b.writeUInt16LE(1, 32); b.writeUInt16LE(8, 34);
  b.write('data', 36); b.writeUInt32LE(n, 40);
  return b;
}

function loadVideos() {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PATH: `${path.join(ROOT, 'tools')}${path.delimiter}${process.env.PATH}` };
  for (const id of ids) {
    const out = execFileSync('yt-dlp', ['--no-warnings', '--skip-download', '--print', '%(duration)s|%(title)s', '--', `https://www.youtube.com/watch?v=${id}`], { encoding: 'utf8', env });
    const [duration, ...title] = out.trim().split('|');
    videos[id] = { duration: Number(duration), title: title.join('|').replace(/[<&"\\]/g, ' '), wav: silentWav(Number(duration)) };
    console.log(`${id}: ${duration} s, ${videos[id].title.slice(0, 60)}`);
  }
}

// Like YouTube: the URL changes at once, the video source next, the title last
const STANDIN = (titles) => `<!doctype html><title>${titles[new Set(Object.keys(titles)).values().next().value]} - YouTube</title>
<video controls muted autoplay style="width:900px;height:506px;display:block"></video>
<script>
  const titles = ${JSON.stringify(titles)};
  const v = document.querySelector('video');
  function show(id) {
    v.src = '/yume-standin-' + id + '.wav';
    v.play().catch(() => {});
    setTimeout(() => { document.title = titles[id] + ' - YouTube'; }, 400);
  }
  window.spaGo = (id) => { history.pushState({}, '', '/watch?v=' + id); show(id); };
  addEventListener('popstate', () => show(new URLSearchParams(location.search).get('v')));
  show(new URLSearchParams(location.search).get('v'));
</script>`;

async function routeStandin(tab) {
  await tab.route('**/yume-standin-*.wav', (route) => {
    const id = /yume-standin-(.+)\.wav/.exec(route.request().url())[1];
    const { wav } = videos[id];
    const m = /bytes=(\d+)-(\d*)/.exec(route.request().headers().range || '');
    const [from, to] = m ? [Number(m[1]), m[2] ? Number(m[2]) : wav.length - 1] : [0, wav.length - 1];
    return route.fulfill({
      status: m ? 206 : 200, body: wav.subarray(from, to + 1),
      headers: { 'Content-Type': 'audio/wav', 'Accept-Ranges': 'bytes', ...(m ? { 'Content-Range': `bytes ${from}-${to}/${wav.length}` } : {}) },
    });
  });
  const titles = Object.fromEntries(ids.map((id) => [id, videos[id].title]));
  await tab.route('**/watch?v=*', (route) => route.fulfill({ contentType: 'text/html; charset=utf-8', body: STANDIN(titles) }));
}

async function openStandin(id) {
  const tab = await context.newPage();
  await routeStandin(tab);
  tab.on('pageerror', (e) => { if (/yume|subtitle|session/i.test(String(e.stack))) problem(`page error: ${e}`); });
  tab.on('console', (m) => { if (m.type() === 'error' && /yume|subtitle/i.test(m.text())) problem(`console error: ${m.text().slice(0, 160)}`); });
  await tab.goto(`https://www.youtube.com/watch?v=${id}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await tab.waitForFunction(() => document.querySelector('video')?.readyState >= 1, null, { timeout: 15000 });
  return tab;
}

const go = (id) => page.evaluate((id) => window.spaGo(id), id);

// ── what must always hold ─────────────────────────────────────────────────────

function parseSrt(text) {
  const time = (s) => { const [h, m, r] = s.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(r.replace(',', '.')); };
  return (text || '').split(/\r?\n\r?\n/).map((block) => {
    const lines = block.split(/\r?\n/);
    const m = /(\S+) --> (\S+)/.exec(lines[1] || '');
    return m ? { start: time(m[1]), end: time(m[2]), lines: lines.slice(2) } : null;
  }).filter(Boolean);
}

// Bleeding: every line and translation seen, by video and by target language.
// One text under two videos, or one translation under two target languages, came
// from the wrong place. A line both videos really say would show here too, so
// short ones are left out.
const seen = { byVideo: new Map(), byTarget: new Map() };
let target = 'English';
const LONG_ENOUGH = 18;
const SCRIPT = { Japanese: /[぀-ヿ一-鿿]/, Arabic: /[؀-ۿ]/, English: /^[^Ѐ-ӿ぀-鿿؀-ۿ]*$/, French: /^[^Ѐ-ӿ぀-鿿؀-ۿ]*$/ };

function ledger(map, text, owner, what) {
  if (!text || text.length < LONG_ENOUGH) return;
  const was = map.get(text);
  if (was && was !== owner) problem(`bleeding: ${what} "${text.slice(0, 50)}" seen under ${was}, now under ${owner}`);
  map.set(text, was || owner);
}

function checkBleeding(o, id) {
  ledger(seen.byVideo, o.original, id, 'line');
  ledger(seen.byVideo, o.english, id, 'translation');
  ledger(seen.byTarget, o.english, target, 'translation');
  if (o.english && SCRIPT[target] && !SCRIPT[target].test(o.english)) problem(`bleeding: target is ${target}, on screen "${o.english.slice(0, 50)}"`);
}

async function setTarget(language) {
  target = language;
  await setSettings({ targetLanguage: language });
  await sleep(900);  // the window drops the old job's line when the restart lands
}

// The line on screen is one of this video's, at this moment
async function verify(id, tab = page) {
  const [o, v] = [await overlay(tab), await player(tab)];
  if (v?.id && v.id !== id) problem(`the page is on ${v.id}, expected ${id}`);
  if (o.windows > 1) problem(`${o.windows} subtitle windows on the page`);
  if (!o.original) return { o, v };
  checkBleeding(o, id);
  const diag = (await toTab({ action: 'GET_DIAGNOSTICS' }, tab).catch(() => null))?.diagnostics;
  if (diag && diag.videoId !== id) problem(`session is on ${diag.videoId} while the page shows ${id}`);
  const exp = await toTab({ action: 'EXPORT_SRT' }, tab).catch(() => null);
  const cues = parseSrt(exp?.content);
  const line = o.original;
  const cue = cues.filter((c) => c.lines.includes(line));
  if (cues.length && !cue.length) problem(`on ${id} at ${v?.t?.toFixed(1)}: "${line.slice(0, 50)}" is not in this video's subtitles`);
  else if (cue.length && !cue.some((c) => v.t >= c.start - 1.5 && v.t <= c.end + 3)) {
    problem(`on ${id} at ${v.t.toFixed(1)}: "${line.slice(0, 40)}" belongs at ${cue[0].start}-${cue[0].end}`);
  }
  // Its translation is the one saved with that line, not a neighbour's
  if (cue.length && o.english && !cue.some((c) => c.lines.includes(o.english))) {
    problem(`on ${id}: "${line.slice(0, 30)}" shows "${o.english.slice(0, 40)}", which is not its translation`);
  }
  return { o, v, cues };
}

async function watch(id, seconds, tab = page) {
  const t0 = Date.now();
  let lines = 0, translated = 0, samples = 0;
  while (Date.now() - t0 < seconds * 1000) {
    const { o } = await verify(id, tab);
    samples++;
    if (o.original) lines++;
    if (o.english) translated++;
    await sleep(700);
  }
  return { lines, translated, samples };
}

async function firstLine(id, limit = 90, tab = page) {
  const t0 = Date.now();
  while (Date.now() - t0 < limit * 1000) {
    const { o } = await verify(id, tab);
    if (o.original) return (Date.now() - t0) / 1000;
    await sleep(400);
  }
  return null;
}

const logLines = () => (existsSync(LOG) ? readFileSync(LOG, 'utf8').split('\n') : []);
const tempDirs = () => readdirSync(os.tmpdir()).filter((f) => f.startsWith('yume_')).length;

async function enable(expect = true) {
  const r = await toTab({ action: 'TOGGLE_SUBTITLES' });
  if (r.active !== expect) problem(`Enable answered active=${r.active}, expected ${expect}`);
  return r;
}

async function active() {
  return (await toTab({ action: 'GET_STATUS' })).active;
}

// ── scenarios ─────────────────────────────────────────────────────────────────

const [A, B, C, D, E, F] = ids;

const scenarios = {
  async 'watch-then-next-then-back'() {
    page = await openStandin(A);
    await enable();
    const cold = await firstLine(A, 150);
    note(`first line on ${A}: ${cold?.toFixed(1)} s (Yume starting cold)`);
    if (cold === null) problem('no line on the first video');
    const w = await watch(A, 12);
    note(`played 12 s: ${w.lines}/${w.samples} samples with a line, ${w.translated} translated`);
    for (const [id, how] of [[B, 'next'], [C, 'next']]) {
      await go(id);
      const s = await firstLine(id, 90);
      note(`${how} to ${id}: first line in ${s?.toFixed(1)} s`);
      if (s === null) problem(`no line after moving to ${id}`);
      await watch(id, 8);
    }
    await page.goBack();
    let s = await firstLine(B, 40);
    note(`back to ${B}: first line in ${s?.toFixed(1)} s`);
    if (s === null) problem(`no line after going back to ${B}`);
    await watch(B, 6);
    await page.goBack();
    s = await firstLine(A, 40);
    note(`back to ${A}: first line in ${s?.toFixed(1)} s`);
    if (s === null) problem(`no line after going back to ${A}`);
    await watch(A, 6);
    await page.goForward();
    s = await firstLine(B, 40);
    note(`forward to ${B}: first line in ${s?.toFixed(1)} s`);
    await watch(B, 5);
  },

  async 'rapid-video-changes'() {
    const before = logLines().length;
    for (let round = 0; round < 12; round++) {
      await go(ids[round % 3]);
      await sleep(round % 2 ? 300 : 900);
    }
    const last = ids[11 % 3];
    await sleep(6000);
    const s = await firstLine(last, 60);
    note(`12 changes in ~7 s, ended on ${last}: first line ${s?.toFixed(1)} s after the last wait`);
    if (s === null) problem('no line after the rapid changes');
    await watch(last, 8);
    const diag = (await toTab({ action: 'GET_DIAGNOSTICS' })).diagnostics;
    if (diag.videoId !== last) problem(`session ended on ${diag.videoId}, not ${last}`);
    const added = logLines().slice(before);
    note(`server log: ${added.filter((l) => /Downloading audio/.test(l)).length} downloads for those changes`);
  },

  async 'skipping-around'() {
    await go(A);
    await firstLine(A, 60);
    const d = videos[A].duration;
    const spots = [10, 300, 30, d - 40, 5, 900, 0, 1200, 60, 600, 15, d - 5, 45, 450, 120];
    for (const t of spots) { await seek(Math.min(t, d - 1)); await sleep(250); }
    await sleep(300);
    note(`${spots.length} skips in ${(spots.length * 0.25).toFixed(0)} s, ended at 120 s`);
    const s = await firstLine(A, 60);
    note(`line after the skips: ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after skipping around');
    await watch(A, 6);
    for (const [t, label] of [[700, 'forward 10 min'], [710, '+10 s'], [700, '-10 s'], [640, '-1 min']]) {
      await seek(t);
      await sleep(300);  // the line under the old position stays until the page's seek lands
      const w = await firstLine(A, 45);
      note(`${label}: line in ${w?.toFixed(1)} s`);
      if (w === null) problem(`no line after skipping ${label}`);
      await sleep(1500);
      await verify(A);
    }
    // Paused: a skip must still update the line
    await page.evaluate(() => document.querySelector('video').pause());
    await seek(705);
    await sleep(1500);
    const paused = await verify(A);
    note(`paused and skipped: line "${(paused.o.original || '').slice(0, 40)}"`);
    await page.evaluate(() => { const v = document.querySelector('video'); v.playbackRate = 2; v.play(); });
    const fast = await watch(A, 6);
    note(`at 2x: ${fast.lines}/${fast.samples} samples with a line`);
    await page.evaluate(() => { document.querySelector('video').playbackRate = 1; });
    await seek(d + 100);
    await sleep(1500);
    await verify(A);
  },

  async 'enable-disable-spam'() {
    await go(B);
    await firstLine(B, 40);
    const spam = async (times) => {
      for (let i = 0; i < times; i++) { await toTab({ action: 'TOGGLE_SUBTITLES' }).catch(() => {}); await sleep(i % 2 ? 150 : 400); }
      await sleep(2500);
    };
    await spam(9);  // on, so an odd number ends off
    if (await active()) problem('9 toggles from on did not end off');
    if ((await overlay()).windows) problem('a window is left after ending off');
    await spam(10);
    if (await active()) problem('10 more toggles did not stay off');
    await spam(11);  // off, so it ends on
    if (!(await active())) problem('11 toggles from off did not end on');
    const s = await firstLine(B, 30);
    if (s === null) problem('no line after the toggles');
    if ((await overlay()).windows !== 1) problem('not exactly one window after the toggles');
  },

  async 'disable-then-change-video'() {
    if (await active()) await enable(false);
    await sleep(800);
    const off = await overlay();
    if (off.windows) problem(`window still there after Disable: ${off.windows}`);
    await go(C);
    await sleep(2500);
    if ((await overlay()).windows) problem('a window came back on its own after changing video while disabled');
    await enable();
    const s = await firstLine(C, 40);
    note(`enabled on ${C}: line in ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after enabling on a new video');
    // Disable while the download of a new video runs
    await enable(false);
    await go(A);
    await sleep(300);
    await enable();
    await sleep(900);
    await enable(false);
    await sleep(1500);
    if ((await overlay()).windows) problem('window left after disabling during a load');
    await enable();
    if ((await firstLine(A, 60)) === null) problem('no line after disable during a load');
  },

  async 'reload-the-page'() {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1, null, { timeout: 15000 });
    await sleep(1000);
    if ((await overlay()).windows) note('window came back after a reload');
    await enable();
    const s = await firstLine(A, 30);
    note(`after F5 and Enable: line in ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after reloading the page');
  },

  async 'settings-while-playing'() {
    await seek(100);
    await sleep(700);  // the line under the old position stays until the page's seek lands
    await firstLine(A, 40);
    const samples = [];
    await setSettings({ showRomaji: true });
    await sleep(3000);
    let { o } = await verify(A);
    samples.push(`romaji on: "${(o.romaji || '').slice(0, 30)}"`);
    if (o.original && !o.romaji) note('romaji not on screen 3 s after switching on');
    await setSettings({ showRomaji: false });
    await sleep(1500);
    ({ o } = await verify(A));
    if (o.romaji) problem('romaji still shown after switching off');
    await setTarget('French');
    const s = await firstLine(A, 45);
    note(`target → French: line in ${s?.toFixed(1)} s`);
    const t0 = Date.now();
    const french = () => /[éèêàçùâ]|\b(le|la|les|des|une|un|et|du|de)\b/i.test(o.english || '');
    do { await sleep(500); ({ o } = await verify(A)); } while (Date.now() - t0 < 25000 && !french());
    samples.push(`French after ${((Date.now() - t0) / 1000).toFixed(0)} s: "${(o.english || '').slice(0, 40)}"`);
    if (!french()) problem('no French line 25 s after changing the target language');
    await setTarget('English');
    await firstLine(A, 45);
    await setSettings({ timingOffset: 20 });
    await sleep(1500);
    await setSettings({ timingOffset: 0 });
    note(samples.join('; '));
  },

  // What changes the job itself, while it plays and while it works
  async 'change-languages-while-playing'() {
    const settled = async (label, wantTranslation = true) => {
      await sleep(700);
      const s = await firstLine(A, 90);
      // An empty slot proves nothing about bleeding: wait for a translated line
      const t0 = Date.now();
      let { o } = await verify(A);
      while (wantTranslation && !o.english && Date.now() - t0 < 30000) { await sleep(400); ({ o } = await verify(A)); }
      if (wantTranslation && !o.english) problem(`no translation on screen 30 s after ${label}`);
      const diag = (await toTab({ action: 'GET_DIAGNOSTICS' })).diagnostics;
      note(`${label}: line in ${s?.toFixed(1)} s, "${(o.original || '').slice(0, 34)}" | "${(o.english || '').slice(0, 34)}" | notice "${(o.notice || '').slice(0, 40)}"`);
      if (s === null) problem(`no line after ${label}`);
      if ((await overlay()).windows !== 1) problem(`not exactly one window after ${label}`);
      return { o, diag };
    };
    if (!(await active())) await enable();
    await go(A);
    await sleep(1200);
    await seek(300);
    await sleep(700);
    await firstLine(A, 60);
    await setSettings({ sourceLanguage: lang === 'ja' ? 'ko' : 'ja' });
    const wrong = await settled('source → a language the video is not in');
    if (!wrong.o.notice) note('no wrong-language notice yet');
    await setSettings({ sourceLanguage: 'auto' });
    await settled('source → auto-detect');
    await setSettings({ sourceLanguage: lang });
    await settled('source → back to the right language');
    await setTarget('Japanese');
    await settled('target → Japanese');
    await setTarget('Arabic');
    await settled('target → Arabic (right to left)');
    await setSettings({ showEnglish: false });
    const off = await settled('translation off', false);
    if (off.o.english) problem('a translation is on screen with translation off');
    await setSettings({ showEnglish: true });
    await setTarget('English');
    await settled('translation back on, English');
    // Changed again before the last change has settled
    for (const [i, language] of ['ko', 'ja', 'zh', lang, 'ar', lang].entries()) {
      await setSettings({ sourceLanguage: language, targetLanguage: i % 2 ? 'English' : 'French' });
      await sleep(350);
    }
    await setTarget('English');
    await settled('six quick changes of language');
    const diag = (await toTab({ action: 'GET_DIAGNOSTICS' })).diagnostics;
    if (diag.videoId !== A) problem(`session on ${diag.videoId} after the changes`);
    // While the job is downloading: a video not seen yet
    await go(B);
    await sleep(600);
    await setTarget('French');
    await setTarget('English');
    const s = await firstLine(B, 60);
    note(`language changed while loading ${B}: line in ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after changing language during a load');
    await verify(B);
    await go(A);
    await firstLine(A, 40);
  },

  // A switch drops every job while Whisper is busy; pollers then recreate theirs
  async 'change-models-while-playing'() {
    const CONFIG = path.join(ROOT, 'config', 'yume_config.json');
    const switchTo = async (model) => {
      const t0 = Date.now();
      const r = await server('POST', '/model/switch', { model }, 180000);
      return { ...r, seconds: (Date.now() - t0) / 1000 };
    };
    const settled = async (label, limit = 120) => {
      await sleep(1500);
      const s = await firstLine(A, limit);
      note(`${label}: line in ${s?.toFixed(1)} s`);
      if (s === null) problem(`no line after ${label}`);
      await watch(A, 4);
      if ((await overlay()).windows !== 1) problem(`not exactly one window after ${label}`);
    };
    if (!(await active())) await enable();
    await go(A);
    await sleep(1200);
    await seek(40);
    await firstLine(A, 60);
    // Fractions, not seconds: a jump past the end of a short video has no line to wait for
    const length = await page.evaluate(() => window.document.querySelector('video').duration);

    let r = await switchTo('tiny');
    note(`switch to tiny while playing: ${r.status} in ${r.seconds.toFixed(1)} s, ${JSON.stringify(r.data).slice(0, 80)}`);
    if (!r.ok) problem(`switch to tiny failed: ${JSON.stringify(r.data).slice(0, 120)}`);
    await settled('tiny while playing');

    await seek(length * 0.5);  // not transcribed yet: Whisper is working when the switch comes
    await sleep(1200);
    r = await switchTo('base');
    note(`switch to base mid-transcription: ${r.status} in ${r.seconds.toFixed(1)} s`);
    if (!r.ok) problem(`switch to base failed: ${JSON.stringify(r.data).slice(0, 120)}`);
    await settled('base mid-transcription');

    const [one, two] = await Promise.all([switchTo('tiny'), switchTo('base')]);
    note(`two switches at once: ${one.status} ${JSON.stringify(one.data).slice(0, 60)} / ${two.status} ${JSON.stringify(two.data).slice(0, 60)}`);
    await settled('two switches at once');

    r = await switchTo('no-such-model');
    if (r.ok || r.status !== 400) problem(`a bad model answered ${r.status}`);
    r = await switchTo('../../etc');
    if (r.ok) problem('a path outside any model folder was accepted');
    await verify(A);

    await go(C);  // the switch lands during the load of a video
    await sleep(500);
    r = await switchTo('tiny');
    note(`switch during a video change: ${r.status}`);
    await sleep(1500);
    let s = await firstLine(C, 90);
    note(`line on ${C} after that: ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after a switch during a video change');
    await verify(C);

    await go(A);
    r = await switchTo('large-v3-turbo');
    if (!r.ok) problem('switch back to turbo failed');
    await settled('back to turbo');
    const stats = (await server('GET', '/stats')).data;
    if (stats.model !== 'large-v3-turbo') problem(`server on ${stats.model} after switching back`);
    if (JSON.parse(readFileSync(CONFIG, 'utf8')).whisper_model !== 'large-v3-turbo') problem('the config does not have the model switched to');

    // Settings the server re-reads from the config file while it runs
    const edit = (changes) => {
      const cfg = JSON.parse(readFileSync(CONFIG, 'utf8'));
      writeFileSync(CONFIG, JSON.stringify({ ...cfg, ...changes }, null, 2));
    };
    const original = readFileSync(CONFIG, 'utf8');
    try {
      edit({ translation_prompt: 'Translate each line into plain, simple English.' });
      await seek(length * 0.7);
      await settled('custom translation prompt while playing');
      edit({ translation_model: 'some-other-name' });
      await seek(length * 0.8);
      await settled('translation model name changed while playing');
      edit({ vocal_isolation: false });
      await go(B);
      s = await firstLine(B, 60);
      note(`vocal isolation off, next video: line in ${s?.toFixed(1)} s`);
      if (s === null) problem('no line with vocal isolation switched off');
      edit({ translation_host: '127.0.0.1', translation_port: 5999 });  // nothing listens there
      await seek(100);
      await sleep(14000);
      const dead = await verify(B);
      note(`translator address wrong: line "${(dead.o.original || '').slice(0, 30)}", translation "${(dead.o.english || '').slice(0, 30)}"`);
    } finally {
      writeFileSync(CONFIG, original);
    }
    await sleep(2000);
    await go(A);
    await settled('config back as it was');
  },

  async 'blacklist'() {
    await seek(200);
    await sleep(700);  // the line under the old position stays until the page's seek lands
    await firstLine(A, 40);
    const exportBefore = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
    let shown = '';
    for (let i = 0; i < 40 && !shown; i++) { shown = (await overlay()).original; await sleep(250); }
    note(`blacklisting the line on screen: "${shown.slice(0, 50)}"`);
    const v0 = await player();
    await page.evaluate(() => document.querySelector('video').pause());
    await server('POST', '/blacklist/add', { text: shown });
    await toTab({ action: 'REFRESH_SUBTITLES' });
    await sleep(1200);
    const o = await overlay();
    if (o.original === shown) problem('a blacklisted line stayed on screen');
    const after = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
    if (after.some((c) => c.lines.includes(shown))) problem('a blacklisted line is still in the export');
    note(`export: ${exportBefore.length} → ${after.length} lines`);
    await server('POST', '/blacklist/add', { text: shown.toUpperCase() });
    const dup = (await server('GET', '/blacklist')).data;
    note(`same line in capitals: ${dup.count} item(s)`);
    await server('POST', '/blacklist/remove', { text: shown });
    await toTab({ action: 'REFRESH_SUBTITLES' });
    await sleep(1200);
    await seek(v0.t);
    await sleep(1200);
    const back = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
    if (!back.some((c) => c.lines.includes(shown))) problem('a line removed from the blacklist did not come back');
    // The job keeps transcribing: counts are only compared within one pause
    await sleep(600);
    const ready = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
    const word = ready.flatMap((c) => c.lines[0].split(/\s+/)).find((w) => /^[а-яё]{5,}$/i.test(w));
    for (const item of ['а', 'не', ' ', word]) {
      const before = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
      const r = await server('POST', '/blacklist/add', { text: item });
      await sleep(600);
      const after2 = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
      note(`blacklist item ${JSON.stringify(item)}: answered ${r.status}, export ${before.length} → ${after2.length} lines`);
      if (['а', 'не'].includes(item) && after2.length < before.length - 3) problem(`"${item}" hid ${before.length - after2.length} lines`);
      if (item === word && after2.some((c) => c.lines[0].split(/\s+/).includes(word))) problem(`"${word}" is still in the export`);
      await server('POST', '/blacklist/remove', { text: item });
      await sleep(400);
    }
    const hostile = '<img src=x onerror=alert(1)>';
    await server('POST', '/blacklist/add', { text: hostile });
    await server('POST', '/blacklist/remove', { text: hostile });
    await server('POST', '/blacklist/update', { blacklist: [] });
    await sleep(800);
    const final = parseSrt((await toTab({ action: 'EXPORT_SRT' })).content);
    if (final.length < back.length) problem(`export ${final.length} lines after clearing the list, had ${back.length}`);
    if ((await server('GET', '/blacklist')).data.count !== 0) problem('the blacklist is not empty after clearing it');
    await page.evaluate(() => document.querySelector('video').play());
  },

  async 'two-tabs'() {
    const second = await openStandin(B);
    await toTab({ action: 'TOGGLE_SUBTITLES' }, second);
    const s = await firstLine(B, 40, second);
    note(`second tab on ${B}: line in ${s?.toFixed(1)} s`);
    for (let i = 0; i < 6; i++) {
      await second.bringToFront();
      await verify(B, second);
      await page.bringToFront();
      await verify(A, page);
      await sleep(600);
    }
    await second.close();
    await sleep(1500);
    await verify(A);
  },

  // New songs, left before they are ready: what is left must not hold up the last
  async 'rapid-new-videos'() {
    if (!D) { note('no songs given: skipped'); return; }
    if (!(await active())) await enable();
    const t0 = Date.now();
    await go(D);
    await sleep(2500);
    await go(E);
    await sleep(2500);
    await go(F);
    const s = await firstLine(F, 150);
    note(`three new songs in 5 s: line on the last ${s?.toFixed(1)} s after arriving (${((Date.now() - t0) / 1000).toFixed(0)} s in all)`);
    if (s === null) problem('no line on the last of the new videos');
    await watch(F, 6);
    await go(A);
    await firstLine(A, 40);
  },

  async 'server-restarts-while-playing'() {
    if (!(await active())) await enable();
    await firstLine(A, 40);
    await server('POST', '/blacklist/add', { text: 'yume stress test item' });
    const py = process.env.PYTHON || 'python';
    execFileSync(py, ['pocket_yume.py', 'stop'], { cwd: ROOT, encoding: 'utf8' });
    note('Yume stopped while playing');
    const t0 = Date.now();
    let said = '';
    while (Date.now() - t0 < 25000 && !said) {
      const o = await overlay();
      if (/stopped|Lost contact/i.test(o.status || '')) said = o.status;
      await sleep(500);
    }
    note(`after ${((Date.now() - t0) / 1000).toFixed(1)} s the window said: "${said}"`);
    if (!said) problem('nothing on screen says Yume stopped');
    const on = await active();
    if (on) problem('still active after Yume stopped');
    await enable();
    const s = await firstLine(A, 120);
    note(`Enable started it again: line in ${s?.toFixed(1)} s`);
    if (s === null) problem('no line after starting again');
    const kept = (await server('GET', '/blacklist')).data.blacklist || [];
    if (!kept.includes('yume stress test item')) problem('the blacklist did not survive the restart');
    await server('POST', '/blacklist/remove', { text: 'yume stress test item' });
  },

  async 'real-youtube'() {
    const tab = await context.newPage();
    await tab.goto(`https://www.youtube.com/watch?v=${B}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3000);
    const reject = tab.locator('button:visible', { hasText: /^\s*(Reject all|Tout refuser|Alle ablehnen)\s*$/ }).first();
    if (await reject.count().catch(() => 0)) { await reject.click({ timeout: 4000 }).catch(() => {}); await sleep(2500); }
    await tab.waitForSelector('video', { timeout: 30000 }).catch(() => {});
    await tab.evaluate(() => { const v = document.querySelector('video'); if (v) v.muted = true; });
    if (await tab.evaluate(() => document.querySelector('video')?.paused)) await tab.mouse.click(500, 300).catch(() => {});
    page = tab;
    await enable();
    const s = await firstLine(B, 40, tab);
    note(`real page ${B}: line in ${s?.toFixed(1)} s`);
    // A real in-page link, as a click on a recommendation would be
    const href = await tab.evaluate(() => [...document.querySelectorAll('a[href^="/watch?v="]')].map((a) => a.getAttribute('href')).find((h) => !location.href.includes(h.slice(9, 20))));
    if (!href) { note('no recommendation on the page: stopped here'); return; }
    await tab.evaluate((h) => document.querySelector(`a[href="${h}"]`).click(), href);
    await sleep(3500);
    const o = await overlay();
    const id = new URL(tab.url()).searchParams.get('v');
    note(`clicked a recommendation: now ${id}, window says "${o.status || o.original || ''}"`.slice(0, 120));
    await sleep(6000);
    const diag = (await toTab({ action: 'GET_DIAGNOSTICS' })).diagnostics;
    if (diag && diag.videoId !== id) problem(`after a real in-page click the session is on ${diag.videoId}, the page on ${id}`);
    await tab.goBack();
    await sleep(2500);
    const back = new URL(tab.url()).searchParams.get('v');
    await sleep(6000);
    const diag2 = (await toTab({ action: 'GET_DIAGNOSTICS' })).diagnostics;
    note(`back to ${back}: session on ${diag2?.videoId}`);
    if (diag2 && diag2.videoId !== back) problem(`after a real back the session is on ${diag2.videoId}, the page on ${back}`);
  },
};

// ── run ───────────────────────────────────────────────────────────────────────

function browser() {
  const brave = 'C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe';
  const exe = process.env.BROWSER || (existsSync(brave) ? brave : '');
  return exe ? { executablePath: exe } : { channel: 'chromium' };
}

loadVideos();
const logStart = logLines().length;
const tempStart = tempDirs();
const ext = path.join(ROOT, 'extension');
context = await chromium.launchPersistentContext(path.join(os.tmpdir(), 'yume-live-profile'), {
  ...browser(),
  headless: !process.env.HEADED,
  viewport: { width: 1280, height: 800 },
  args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`, '--autoplay-policy=no-user-gesture-required'],
});
try {
  worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  for (const p of context.pages().slice(1)) await p.close();
  for (let i = 0; i < 40 && !(await worker.evaluate(async () => !!(await chrome.storage.local.get('settings')).settings)); i++) await sleep(250);
  await setSettings({ sourceLanguage: lang, targetLanguage: 'English', showRomaji: false, showEnglish: true, timingOffset: 0 });
  for (const [name, run] of Object.entries(scenarios)) {
    if (only && !only.includes(name)) continue;
    scenario = name;
    console.log(`\n=== ${name}`);
    const before = problems.length;
    const t0 = Date.now();
    try {
      await run();
    } catch (e) {
      problem(`scenario crashed: ${String(e).slice(0, 200)}`);
    }
    results.push({ name, seconds: Math.round((Date.now() - t0) / 1000), problems: problems.length - before });
  }
} finally {
  await context.close().catch(() => {});
}

const added = logLines().slice(logStart);
const tracebacks = added.filter((l) => /Traceback/.test(l)).length;
const failed = added.filter((l) => /\bfailed\b/i.test(l) && !/Could not fetch|ffmpeg failed|ffmpeg also/.test(l));
const report = {
  results, problems, notes, tracebacks,
  linesLedger: seen.byVideo.size, translationsLedger: seen.byTarget.size,
  downloads: added.filter((l) => /Downloading audio/.test(l)).length,
  loggedFailures: failed.slice(0, 20).map((l) => l.slice(0, 160)),
  tempDirsLeft: tempDirs() - tempStart,
};
writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report, null, 1));
console.log('\n=== summary');
for (const r of results) console.log(`${r.problems ? 'FAIL' : 'ok  '} ${r.name} (${r.seconds} s) ${r.problems || ''}`);
console.log(`${problems.length} problems, ${tracebacks} tracebacks, ${report.downloads} downloads, ${report.tempDirsLeft} temp dirs left`);
console.log(`bleeding check saw ${seen.byVideo.size} lines and translations by video, ${seen.byTarget.size} translations by target`);
process.exit(problems.length ? 1 : 0);
