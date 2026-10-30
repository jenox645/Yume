// The whole of Yume, nothing faked: the real extension in a real browser on real
// video pages, the real server, Whisper on the GPU, the local LLM, the one-click
// start. Not part of CI (it needs all of that, and the network). It plays each
// video, records what the overlay shows every second, and writes a report.
//
// Run: node tests/e2e/live.mjs <out-dir> <lang>:<url>[@start] [<lang>:<url>[@start] ...]
//      WATCH=60 how long each video plays (s); HEADED=1 shows the browser;
//      BROWSER=<path to a Chromium browser>, default Brave if installed. Chrome
//      itself cannot run this: it ignores --load-extension since version 137.
//      MIRROR=1 plays long tests on a stand-in for YouTube's page: YouTube's own
//      player stops after ~45 s in an automated, signed-out browser, with or
//      without Yume. The stand-in keeps the real URL and title and a video of the
//      same length (silent); the extension and the server do everything for real,
//      including the server's own download of the real audio from YouTube.

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const WATCH_S = Number(process.env.WATCH || 60);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const [outDir, ...specs] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });

function parseSpec(spec) {
  const m = /^([a-z]{2,3}):(.+?)(?:@(\d+))?$/.exec(spec);
  if (!m) throw new Error(`bad video spec: ${spec}`);
  return { lang: m[1], url: m[2], start: Number(m[3] || 0) };
}

let context, worker;

// The extension has no "tabs" permission, so tab URLs are not readable: the page
// under test is brought to the front and addressed as the active tab
const toTab = async (page, message) => {
  await page.bringToFront();
  return worker.evaluate(async (message) => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return chrome.tabs.sendMessage(tab.id, message);
  }, message);
};

const setSettings = (changes) => worker.evaluate(async (changes) => {
  const { settings } = await chrome.storage.local.get('settings');
  await chrome.storage.local.set({ settings: { ...settings, ...changes } });
}, changes);

// The overlay is in a closed shadow root: read it through the DevTools protocol
async function overlay(page) {
  const cdp = await context.newCDPSession(page);
  const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
  await cdp.detach();
  const out = {};
  const walk = (n) => {
    const a = n.attributes || [];
    const attr = (name) => (a.indexOf(name) >= 0 && a.indexOf(name) % 2 === 0 ? a[a.indexOf(name) + 1] : '');
    const cls = attr('class').split(' ').find((c) => /^(subtitle-(original|english|romaji|notice|status)|chunk-badge)$/.test(c));
    if (cls) {
      const text = (n.children || []).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join('');
      out[cls.replace('subtitle-', '').replace('chunk-', '')] = /display:\s*none/.test(attr('style')) ? '' : text;
    }
    for (const c of [...(n.children || []), ...(n.shadowRoots || []), ...(n.contentDocument ? [n.contentDocument] : [])]) walk(c);
  };
  walk(root);
  return out;
}

const videoState = (page) => page.evaluate(() => {
  const v = [...document.querySelectorAll('video')].sort((a, b) => b.clientWidth - a.clientWidth)[0];
  const ad = !!document.querySelector('.ad-showing, .ytp-ad-player-overlay, .ytp-ad-module *');
  return v ? { t: v.currentTime, d: v.duration, paused: v.paused, ad, title: document.title } : null;
});

// Consent walls, in a new test profile: always the "reject" answer (the profile
// is kept between runs, so YouTube asks once)
async function dismissConsent(page) {
  // By visible text: the button's accessible name is a longer sentence
  const b = page.locator('button:visible', { hasText: /^\s*(Reject all|Tout refuser|Alle ablehnen)\s*$/ }).first();
  if (!(await b.count().catch(() => 0))) return false;
  await b.click({ timeout: 4000 }).catch(() => {});
  await sleep(2000);
  return true;
}

// A real click: YouTube ignores video.play() without a user gesture
async function play(page, start) {
  if (process.env.MIRROR) await page.evaluate(() => document.querySelector('video')?.play().catch(() => {}));
  else if (await page.evaluate(() => document.querySelector('video')?.paused)) await page.mouse.click(500, 300).catch(() => {});
  await sleep(500);
  await page.evaluate((start) => {
    const v = [...document.querySelectorAll('video')].sort((a, b) => b.clientWidth - a.clientWidth)[0];
    if (!v) return;
    v.muted = true;
    if (start && Math.abs(v.currentTime - start) > 2) v.currentTime = start;
  }, start);
}

function silentWav(seconds) {
  const n = Math.ceil(seconds) * 8000;
  const b = Buffer.alloc(44 + n, 128);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n, 4); b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(8000, 24); b.writeUInt32LE(8000, 28); b.writeUInt16LE(1, 32); b.writeUInt16LE(8, 34);
  b.write('data', 36); b.writeUInt32LE(n, 40);
  return b;
}

async function mirror(page, spec) {
  const [duration, ...title] = execFileSync('yt-dlp', ['--no-warnings', '--skip-download', '--print', '%(duration)s|%(title)s', '--', spec.url],
    { encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }).trim().split('|');
  const wav = silentWav(Number(duration));
  const safe = title.join('|').replace(/[<&]/g, ' ');
  // Same origin, answered here: a page reaching 127.0.0.1 makes the browser ask
  // for local-network permission, and the run waits for an answer forever
  await page.route('**/yume-standin.wav', (route) => {
    const m = /bytes=(\d+)-(\d*)/.exec(route.request().headers().range || '');
    const [from, to] = m ? [Number(m[1]), m[2] ? Number(m[2]) : wav.length - 1] : [0, wav.length - 1];
    return route.fulfill({
      status: m ? 206 : 200, body: wav.subarray(from, to + 1),
      headers: { 'Content-Type': 'audio/wav', 'Accept-Ranges': 'bytes', ...(m ? { 'Content-Range': `bytes ${from}-${to}/${wav.length}` } : {}) },
    });
  });
  await page.route('**/watch?v=*', (route) => route.fulfill({
    contentType: 'text/html; charset=utf-8',
    body: `<!doctype html><title>${safe} - YouTube</title><video src="/yume-standin.wav" controls muted style="width:900px;height:506px;display:block"></video>`,
  }));
}

async function runVideo(spec, index) {
  const page = await context.newPage();
  if (process.env.MIRROR) await mirror(page, spec);
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error' && /yume/i.test(m.text())) errors.push(m.text()); });
  page.on('pageerror', (e) => { if (/yume|subtitle/i.test(String(e.stack))) errors.push(String(e)); });
  await setSettings({ sourceLanguage: spec.lang, targetLanguage: 'English', showRomaji: true, showEnglish: true });
  await page.goto(spec.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3000);
  await dismissConsent(page);
  await page.waitForSelector('video', { timeout: 30000 }).catch(() => {});
  await play(page, spec.start);
  await sleep(2000);
  const report = { spec, url: page.url(), samples: [], errors, statuses: [] };
  const t0 = Date.now();
  report.enable = await toTab(page, { action: 'TOGGLE_SUBTITLES' }).catch((e) => ({ error: e.message }));
  let lastStatus = '';
  let playTries = 0;
  while (Date.now() - t0 < (WATCH_S + 240) * 1000) {
    await dismissConsent(page);  // the wall can appear late, and holds the video behind it
    const [o, v] = await Promise.all([overlay(page), videoState(page)]);
    const el = (Date.now() - t0) / 1000;
    const s = { el: Math.round(el * 10) / 10, ...v, ...o };
    report.samples.push(s);
    if (o.status && o.status !== lastStatus) { lastStatus = o.status; report.statuses.push([s.el, o.status]); }
    if (report.firstLine === undefined && o.original) {
      report.firstLine = s.el;
      report.firstLineAt = v?.t;
    }
    if (report.firstTranslation === undefined && o.english) report.firstTranslation = s.el;
    if (v?.paused && playTries < 3) { playTries++; await play(page, 0); }
    const watched = report.firstLine !== undefined ? el - report.firstLine : 0;
    if (watched >= WATCH_S) break;
    await sleep(1000);
  }
  // Jump far ahead, like a viewer: how long until a line shows there?
  const d = (await videoState(page))?.d;
  if (d > 60) {
    const target = Math.round(d * 0.7);
    await play(page, target);
    const t1 = Date.now();
    while (Date.now() - t1 < 60000) {
      const o = await overlay(page);
      if (o.original) { report.seek = { target, seconds: (Date.now() - t1) / 1000, line: o.original, english: o.english }; break; }
      await sleep(500);
    }
    report.seek ??= { target, seconds: null };
  }
  await page.screenshot({ path: path.join(outDir, `v${index}.png`) }).catch(() => {});
  report.diagnostics = await toTab(page, { action: 'GET_DIAGNOSTICS' }).catch((e) => ({ error: e.message }));
  report.export = await toTab(page, { action: 'EXPORT_SRT' }).catch((e) => ({ error: e.message }));
  report.disable = await toTab(page, { action: 'TOGGLE_SUBTITLES' }).catch((e) => ({ error: e.message }));
  await page.close();
  writeFileSync(path.join(outDir, `v${index}.json`), JSON.stringify(report, null, 1));
  const lines = report.samples.filter((s) => s.original).length;
  const untranslated = report.samples.filter((s) => s.original && !s.english).length;
  console.log(`v${index} ${spec.lang} first line ${report.firstLine}s, translated ${report.firstTranslation}s, ` +
    `${lines} samples with a line, ${untranslated} without translation, ${errors.length} errors, ` +
    `seek ${report.seek?.target}s: ${report.seek?.seconds}s, status: ${report.diagnostics?.diagnostics?.status}`);
}

function browser() {
  const brave = 'C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe';
  const path_ = process.env.BROWSER || (existsSync(brave) ? brave : '');
  return path_ ? { executablePath: path_ } : { channel: 'chromium' };
}

const userDir = path.join(os.tmpdir(), 'yume-live-profile');
const ext = path.join(ROOT, 'extension');
context = await chromium.launchPersistentContext(userDir, {
  ...browser(),
  headless: !process.env.HEADED,
  viewport: { width: 1280, height: 800 },
  args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`, '--autoplay-policy=no-user-gesture-required'],
});
try {
  worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  for (const p of context.pages().slice(1)) await p.close();  // tabs restored from an earlier run
  for (let i = 0; i < 40 && !(await worker.evaluate(async () => !!(await chrome.storage.local.get('settings')).settings)); i++) await sleep(250);
  for (const [i, s] of specs.entries()) await runVideo(parseSpec(s), i);
} finally {
  await context.close();
}
