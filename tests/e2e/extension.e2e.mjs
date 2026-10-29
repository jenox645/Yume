// The real extension in a real Chromium, against the real server code with
// Whisper, the LLM and the download faked (fake_server.py). Unit tests check
// each side alone; the bugs this catches lived between them: the title the
// extension never sent, the notice the window never showed.
//
// Run: npm run e2e   (once: npx playwright install chromium; HEADED=1 to watch;
//      needs Python with the server's packages)

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const TITLE = '(2) Test Song【MV】 - YouTube';  // the extension sends "Test Song【MV】"

let server, serverPort, requests = [], pages, pagePort, context, worker, page, userDir;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(what, fn, timeout = 15000) {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < timeout) {
    last = await fn();
    if (last) return last;
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what} (last: ${JSON.stringify(last)})`);
}

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

// A silent 8 kHz WAV: something a <video> element loads, with a duration
function silentWav(seconds) {
  const n = seconds * 8000;
  const b = Buffer.alloc(44 + n, 128);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n, 4); b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(8000, 24); b.writeUInt32LE(8000, 28); b.writeUInt16LE(1, 32); b.writeUInt16LE(8, 34);
  b.write('data', 36); b.writeUInt32LE(n, 40);
  return b;
}

function startPages() {
  const wav = silentWav(120);
  const html = (title) => `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head>
    <body><video src="/media.wav" muted style="width:640px;height:360px;display:block"></video></body></html>`;
  pages = http.createServer((req, res) => {
    if (req.url.startsWith('/media.wav')) {
      // Range requests: without them the video cannot seek until fully loaded
      const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || '');
      const [from, to] = m ? [Number(m[1]), m[2] ? Number(m[2]) : wav.length - 1] : [0, wav.length - 1];
      res.writeHead(m ? 206 : 200, {
        'Content-Type': 'audio/wav', 'Accept-Ranges': 'bytes', 'Content-Length': to - from + 1,
        ...(m ? { 'Content-Range': `bytes ${from}-${to}/${wav.length}` } : {}),
      });
      res.end(wav.subarray(from, to + 1));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html(TITLE));
    }
  });
  return new Promise((resolve) => pages.listen(pagePort, '127.0.0.1', resolve));
}

function startServer() {
  server = spawn(PYTHON, [path.join(ROOT, 'tests', 'e2e', 'fake_server.py'), String(serverPort)],
    { cwd: ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let log = '';
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fake server did not start:\n' + log)), 60000);
    server.stdout.on('data', (d) => {
      for (const line of d.toString('utf8').split('\n')) {
        if (line.startsWith('{"req"')) requests.push(JSON.parse(line));
        if (line.startsWith('{"ready"')) { clearTimeout(timer); resolve(); }
      }
      log += d;
    });
    server.stderr.on('data', (d) => { log += d; });
    server.on('exit', (code) => reject(new Error(`fake server exited (${code}):\n${log}`)));
  });
}

// Run in the extension's service worker: talk to the page's content script
const toTab = (message) => worker.evaluate(async ({ url, message }) => {
  const tab = (await chrome.tabs.query({})).find((t) => t.url?.startsWith(url));
  return chrome.tabs.sendMessage(tab.id, message);
}, { url: `http://127.0.0.1:${pagePort}/`, message });

const setSettings = (changes) => worker.evaluate(async (changes) => {
  const { settings } = await chrome.storage.local.get('settings');
  await chrome.storage.local.set({ settings: { ...settings, ...changes } });
}, changes);

// The overlay lives in a closed shadow root: read it through the DevTools protocol
async function overlay() {
  const cdp = await context.newCDPSession(page);
  const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
  await cdp.detach();
  const out = {};
  const walk = (n) => {
    const a = n.attributes || [];
    const attr = (name) => (a.indexOf(name) >= 0 && a.indexOf(name) % 2 === 0 ? a[a.indexOf(name) + 1] : '');
    const cls = attr('class').split(' ').find((c) => /^subtitle-(original|english|romaji|notice|status|title)$/.test(c));
    if (cls) {
      const text = (n.children || []).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join('');
      out[cls.replace('subtitle-', '')] = /display:\s*none/.test(attr('style')) ? '' : text;
    }
    for (const c of [...(n.children || []), ...(n.shadowRoots || [])]) walk(c);
  };
  walk(root);
  return out;
}

const seek = (t) => page.evaluate((t) => { document.querySelector('video').currentTime = t; }, t);
const jobsCreated = () => requests.filter((r) => r.req === '/jobs' && r.method === 'POST').map((r) => r.body);

before(async () => {
  [serverPort, pagePort] = [await freePort(), await freePort()];
  await Promise.all([startServer(), startPages()]);
  userDir = mkdtempSync(path.join(os.tmpdir(), 'yume-e2e-'));
  const ext = path.join(ROOT, 'extension');
  context = await chromium.launchPersistentContext(userDir, {
    // Playwright's Chromium in the new headless mode, which runs extensions. On
    // Windows it may not start ("side-by-side configuration is incorrect"), so
    // there Edge, which every Windows 11 has. BROWSER=chromium|msedge|chrome to pick.
    channel: process.env.BROWSER || (process.platform === 'win32' ? 'msedge' : 'chromium'),
    headless: !process.env.HEADED,
    args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`],
  });
  worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
  await until('the extension settings', () => worker.evaluate(async () => !!(await chrome.storage.local.get('settings')).settings));
  await setSettings({ whisperUrl: `http://127.0.0.1:${serverPort}`, sourceLanguage: 'ja', showRomaji: true, showEnglish: true });
  page = await context.newPage();
  await page.goto(`http://127.0.0.1:${pagePort}/watch?v=one`);
  await page.waitForFunction(() => document.querySelector('video').readyState >= 1);
});

after(async () => {
  await context?.close();
  server?.kill();
  pages?.close();
  if (userDir) rmSync(userDir, { recursive: true, force: true });
});

test('Enable creates a job with the page title, language and duration', async () => {
  const r = await toTab({ action: 'TOGGLE_SUBTITLES' });
  assert.equal(r.success, true, JSON.stringify(r));
  const job = await until('POST /jobs', () => jobsCreated()[0]);
  assert.equal(job.title, 'Test Song【MV】');
  assert.equal(job.language, 'ja');
  assert.equal(job.target, 'English');
  assert.equal(job.romanize, true);
  assert.equal(Math.round(job.duration), 120);
  assert.match(job.video_id, /^127\.0\.0\.1\/watch\?v=one#120$/);
});

test('the line under the playhead is drawn: original, romaji, translation', async () => {
  await seek(9.5);  // line 2 is 9.0-11.5 s
  const shown = await until('line 2 on screen', async () => {
    const o = await overlay();
    return o.original === '第2行です' && o.english === 'Line 2' ? o : null;
  });
  assert.match(shown.romaji, /gyou desu/i);
  await seek(62);  // line 15: 61-63.5 s
  await until('line 15 after a seek', async () => (await overlay()).english === 'Line 15');
  await seek(60);  // between lines, no grace after a seek
  await until('nothing between lines', async () => (await overlay()).english === '');
});

test('lines follow playback', async () => {
  await seek(16.6);  // line 4 starts at 17 s
  await page.evaluate(() => document.querySelector('video').play());
  await until('line 4 while playing', async () => (await overlay()).english === 'Line 4', 8000);
  await page.evaluate(() => document.querySelector('video').pause());
});

test('a blacklisted line disappears at once', async () => {
  await seek(13.5);  // line 3
  await until('line 3', async () => (await overlay()).english === 'Line 3');
  const popup = await context.newPage();
  const id = new URL(worker.url()).host;
  await popup.goto(`chrome-extension://${id}/popup.html`);
  const r = await popup.evaluate(() => new Promise((res) => chrome.runtime.sendMessage(
    { type: 'API', request: { method: 'POST', path: '/blacklist/add', body: { text: '第3行です' } } }, res)));
  assert.equal(r.ok, true, JSON.stringify(r));
  await popup.close();
  await toTab({ action: 'REFRESH_SUBTITLES' });
  await until('line 3 hidden', async () => (await overlay()).original === '');
});

test('the popup shows both servers up', async () => {
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
  await popup.waitForFunction(() => document.getElementById('whisperStatus').className.includes('connected'));
  assert.equal(await popup.textContent('#whisperInfo'), 'cpu | fake-whisper');
  await popup.waitForFunction(() => document.getElementById('translationStatus').className.includes('connected'));
  assert.equal(await popup.isVisible('#serverHint'), false);
  await popup.close();
});

test('popup: every control has a name, and sections open from the keyboard', async () => {
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
  // What a screen reader would announce: aria-label, a <label>, or a button's own text
  const unnamed = await popup.evaluate(() => [...document.querySelectorAll('input, select, button, [role="button"]')]
    .filter((el) => !(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') ||
      (el.id && document.querySelector(`label[for="${el.id}"]`)) ||
      ((el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') && /\w/.test(el.textContent))))
    .map((el) => el.id || el.outerHTML.slice(0, 80)));
  assert.deepEqual(unnamed, []);

  const toggle = '[data-section="history"] .section-toggle';
  const state = () => popup.evaluate((sel) => {
    const t = document.querySelector(sel);
    return { tabIndex: t.tabIndex, expanded: t.getAttribute('aria-expanded'), open: !t.closest('.yume-section').classList.contains('collapsed') };
  }, toggle);
  assert.deepEqual(await state(), { tabIndex: 0, expanded: 'false', open: false });
  await popup.focus(toggle);
  await popup.keyboard.press('Enter');
  assert.deepEqual(await state(), { tabIndex: 0, expanded: 'true', open: true });
  await popup.keyboard.press(' ');
  assert.deepEqual(await state(), { tabIndex: 0, expanded: 'false', open: false });
  await popup.close();
});

test('export gives every line with its translation', async () => {
  const r = await toTab({ action: 'EXPORT_SRT' });
  assert.equal(r.success, true, JSON.stringify(r));
  assert.match(r.content, /第0行です\r?\nLine 0/);
  assert.equal(r.count, 29);  // 30 lines, one blacklisted
  assert.equal(r.title, 'Test Song【MV】');  // names the file
});

test('the popup clears the blacklist only on a second click', async () => {
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
  const blacklist = () => popup.evaluate(() => new Promise((res) => chrome.runtime.sendMessage(
    { type: 'API', request: { path: '/blacklist' } }, (r) => res(r.data.blacklist))));
  assert.deepEqual(await blacklist(), ['第3行です']);
  await popup.evaluate(() => document.getElementById('clearBlacklist').click());
  await sleep(500);
  assert.deepEqual(await blacklist(), ['第3行です']);  // one click: still there
  await popup.evaluate(() => document.getElementById('clearBlacklist').click());
  await until('the blacklist cleared', async () => (await blacklist()).length === 0);
  await popup.close();
  await toTab({ action: 'REFRESH_SUBTITLES' });
  await seek(13.5);
  await until('line 3 back', async () => (await overlay()).english === 'Line 3');
});

test('the wrong-language notice stays on screen while lines play', async () => {
  const before = jobsCreated().length;
  await setSettings({ sourceLanguage: 'ko' });  // the fake Whisper hears Japanese
  await until('a new job for Korean', () => jobsCreated().length > before && jobsCreated().at(-1).language === 'ko');
  await seek(21.5);  // line 5
  const o = await until('the notice', async () => {
    const v = await overlay();
    return v.notice && v.original ? v : null;
  });
  assert.match(o.notice, /sounds like Japanese, but Yume is set to Korean/);
  const korean = jobsCreated().length;
  await setSettings({ sourceLanguage: 'ja' });
  await until('a new job for Japanese', () => jobsCreated().length > korean && jobsCreated().at(-1).language === 'ja');
  await until('the notice cleared', async () => (await overlay()).notice === '');
});

test('in-page navigation starts a job for the new video', async () => {
  await page.evaluate(() => { history.pushState({}, '', '/watch?v=two'); document.title = 'Second Song - YouTube'; });
  const job = await until('POST /jobs for the new page', () => jobsCreated().find((j) => /v=two/.test(j.video_id)));
  assert.equal(job.title, 'Second Song');
});

test('the new video\'s title is used even when the page sets it after the URL', async () => {
  // YouTube: the URL changes first, the title follows once the page data arrives
  await page.evaluate(() => {
    history.pushState({}, '', '/watch?v=three');
    setTimeout(() => { document.title = 'Third Song - YouTube'; }, 2000);
  });
  const job = await until('POST /jobs for the third page', () => jobsCreated().find((j) => /v=three/.test(j.video_id)));
  assert.equal(job.title, 'Third Song');
});

test('after lines played, the window says at once that the next video is loading', async () => {
  await seek(5.5);  // line 1
  await until('line 1 on screen', async () => (await overlay()).english === 'Line 1');
  // The title stays the same: the restart waits for it 2.5 s more (4 s after the URL change)
  await page.evaluate(() => history.pushState({}, '', '/watch?v=four'));
  await until('the loading message', async () => /next|new video/.test((await overlay()).status || ''), 3500);
  await until('POST /jobs for the fourth page', () => jobsCreated().find((j) => /v=four/.test(j.video_id)));
});

test('romaji switched off disappears from the line on screen', async () => {
  await seek(25.5);  // line 6
  await until('line 6 with romaji', async () => /gyou/i.test((await overlay()).romaji || ''));
  await setSettings({ showRomaji: false });
  await until('no romaji', async () => (await overlay()).romaji === '');
  assert.equal((await overlay()).english, 'Line 6');
  await setSettings({ showRomaji: true });
});

test('a restarted server (new token, jobs gone) is picked up again', async () => {
  server.kill();
  await new Promise((r) => server.once('exit', r));
  const before = jobsCreated().length;
  await startServer();  // same port, new API token, empty job list
  await until('the job created again', () => jobsCreated().length > before, 30000);
  await seek(29.5);  // line 7
  await until('line 7 from the new server', async () => (await overlay()).english === 'Line 7', 20000);
});

test('Disable stops the session', async () => {
  const r = await toTab({ action: 'TOGGLE_SUBTITLES' });
  assert.equal(r.active, false);
  assert.equal((await toTab({ action: 'GET_STATUS' })).active, false);
});

test('Enable with Yume not running says how to start it', async () => {
  await setSettings({ whisperUrl: `http://127.0.0.1:${await freePort()}` });  // nothing listens there
  await toTab({ action: 'TOGGLE_SUBTITLES' });
  // No native helper in this browser: one-click start is unavailable
  const o = await until('the explanation', async () => {
    const v = await overlay();
    return /not running/i.test(v.status || '') ? v : null;
  }, 20000);
  assert.match(o.status, /START_YUME|autostart on/);
  await setSettings({ whisperUrl: `http://127.0.0.1:${serverPort}` });
});

test('a custom window title shows as soon as the window opens', async () => {
  // Close the window: a failed start (the last test) keeps it open to show its error
  if (!(await toTab({ action: 'GET_STATUS' })).active) await toTab({ action: 'TOGGLE_SUBTITLES' });
  await toTab({ action: 'TOGGLE_SUBTITLES' });
  assert.equal((await overlay()).title, undefined);
  await setSettings({ windowTitle: 'My Subs' });
  await toTab({ action: 'TOGGLE_SUBTITLES' });
  await sleep(1000);  // no settings change after the window opened
  assert.equal((await overlay()).title, 'My Subs');
  await toTab({ action: 'TOGGLE_SUBTITLES' });
  await setSettings({ windowTitle: 'Yume' });
});
