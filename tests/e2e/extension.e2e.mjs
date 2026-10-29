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
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
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
    const cls = attr('class').split(' ').find((c) => /^subtitle-(original|english|romaji|notice|status)$/.test(c));
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

// Every element of the overlay by class: its attributes, own text and (boxes)
// its border box in page pixels — what the mouse needs to drag it
async function overlayParts(boxes = false) {
  const cdp = await context.newCDPSession(page);
  try {
    const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
    const parts = {};
    const walk = (n) => {
      const a = n.attributes || [];
      const attrs = {};
      for (let i = 0; i < a.length; i += 2) attrs[a[i]] = a[i + 1];
      const text = (n.children || []).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join('');
      for (const c of (attrs.class || '').split(' ').filter(Boolean)) parts[c] ??= { nodeId: n.nodeId, attrs, text };
      for (const c of [...(n.children || []), ...(n.shadowRoots || [])]) walk(c);
    };
    walk(root);
    if (boxes) {
      for (const p of Object.values(parts)) {
        try {
          const q = (await cdp.send('DOM.getBoxModel', { nodeId: p.nodeId })).model.border;
          p.box = { x: q[0], y: q[1], w: q[2] - q[0], h: q[5] - q[1] };
        } catch { p.box = null; }  // not rendered (display: none)
      }
    }
    return parts;
  } finally { await cdp.detach(); }
}

const extensionId = () => new URL(worker.url()).host;

// The popup in a tab of its own, with the video tab in front again: the
// popup's "active tab" is then the video page, as when it opens from the toolbar
async function openPopup() {
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${extensionId()}/popup.html`);
  await page.bringToFront();
  return popup;
}

// Change a popup control the way the user does: new value, then its event
const setControl = (popup, id, value, event) => popup.evaluate(([id, value, event]) => {
  const el = document.getElementById(id);
  if (el.type === 'checkbox') el.checked = value; else el.value = value;
  el.dispatchEvent(new el.ownerDocument.defaultView.Event(event, { bubbles: true }));
}, [id, value, event]);

// Expand a collapsed popup section (loads its data)
const openSection = (popup, name) => popup.evaluate((name) => {
  const s = document.querySelector(`[data-section="${name}"]`);
  if (s.classList.contains('collapsed')) s.querySelector('.section-toggle').click();
}, name);

const clickIn = (popup, selector) => popup.evaluate((sel) => document.querySelector(sel).click(), selector);

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

test('romaji switched off disappears from the line on screen', async () => {
  await seek(25.5);  // line 6
  await until('line 6 with romaji', async () => /gyou/i.test((await overlay()).romaji || ''));
  await setSettings({ showRomaji: false });
  await until('no romaji', async () => (await overlay()).romaji === '');
  assert.equal((await overlay()).english, 'Line 6');
  await setSettings({ showRomaji: true });
});

// Before the server restart below, which starts from an empty cache
test('popup: History lists saved videos; SRT downloads one, × removes one', async () => {
  const popup = await openPopup();
  await openSection(popup, 'history');
  const rows = () => popup.evaluate(() => [...document.querySelectorAll('#historyList .blacklist-item .blacklist-text')]
    .map((el) => el.firstChild.textContent.trim()));
  await until('the saved videos', async () => (await rows()).includes('Second Song'));
  assert.ok((await rows()).includes('Test Song【MV】'));
  const button = (label, cls) => popup.evaluate(([label, cls]) => {
    const row = [...document.querySelectorAll('#historyList .blacklist-item')]
      .find((r) => r.querySelector('.blacklist-text').firstChild.textContent.trim() === label);
    row.querySelector(cls).click();
  }, [label, cls]);

  const [dl] = await Promise.all([popup.waitForEvent('download'), button('Test Song【MV】', '.hist-srt')]);
  assert.equal(dl.suggestedFilename(), 'yume-Test_Song【MV】.srt');
  assert.match(readFileSync(await dl.path(), 'utf8'), /第0行です\r?\nLine 0/);

  const library = () => worker.evaluate(async () => (await globalThis.serverRequest({ path: '/library' })).data.videos.map((v) => v.title));
  assert.ok((await library()).includes('Second Song'));
  await button('Second Song', '.hist-del');
  await until('the row gone', async () => !(await rows()).includes('Second Song'));
  assert.ok(!(await library()).includes('Second Song'));  // removed on the server, not only in the list
  await popup.close();
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

test('popup: appearance settings restyle the window live', async () => {
  await seek(29.5);  // line 7
  await until('line 7', async () => (await overlay()).english === 'Line 7');
  const popup = await openPopup();
  await openSection(popup, 'appearance');
  const style = async (cls) => (await overlayParts())[cls]?.attrs.style || '';

  await setControl(popup, 'windowBgColor', '#ff0000', 'input');
  await setControl(popup, 'windowOpacity', '50', 'input');
  await until('a red background at 50%', async () => /rgba\(255, 0, 0, 0\.5\)/.test(await style('subtitle-window')));
  assert.equal(await popup.textContent('#opacityVal'), '50%');

  await setControl(popup, 'originalColor', '#00ff00', 'input');
  await setControl(popup, 'originalFontSize', '30', 'change');
  await until('the original line restyled', async () => {
    const s = await style('subtitle-original');
    return /(^|;)\s*color: rgb\(0, 255, 0\)/.test(s) && /font-size: 30px/.test(s);
  });

  // A font the browser has (the list holds only detected ones; none on a bare Linux CI)
  const font = await popup.evaluate(() => [...document.getElementById('translationFont').options].map((o) => o.value).find(Boolean));
  if (font) {
    await setControl(popup, 'translationFont', font, 'change');
    await until(`the translation in ${font}`, async () => (await style('subtitle-english')).includes(`"${font}"`));
  }

  await setControl(popup, 'windowTitle', 'Live Title', 'change');
  await until('the new title', async () => (await overlayParts())['subtitle-title']?.text === 'Live Title');

  await setControl(popup, 'windowShadow', false, 'change');
  await until('no shadow', async () => /box-shadow: none/.test(await style('subtitle-window')));

  await setControl(popup, 'glassEnabled', true, 'change');
  assert.equal(await popup.isVisible('#glassControls'), true);
  await until('the glass blur', async () => /backdrop-filter: blur\(18px\)/.test(await style('subtitle-window')));
  await setControl(popup, 'glassRadius', '0', 'input');  // a slider: input while dragging, change on release
  await setControl(popup, 'glassRadius', '0', 'change');
  await until('square corners', async () => !/border-radius/.test(await style('subtitle-window')));
  assert.equal(await popup.textContent('#glassRadiusVal'), '0px');

  await popup.close();
  await setSettings({
    windowBgColor: '#08080c', windowOpacity: 88, originalColor: '#ffffff', originalFontSize: 19, translationFont: '',
    windowTitle: 'Yume', windowShadow: true, glassEnabled: false, glassRadius: 20,
  });
  await until('the default look back', async () => /rgba\(8, 8, 12, 0\.88\)/.test(await style('subtitle-window')));
});

test('the window can be dragged, resized, minimized and closed, and reopens where it was', async () => {
  await page.bringToFront();
  const parts = () => overlayParts(true);
  const w0 = (await parts())['subtitle-window'].box;
  const title = (await parts())['subtitle-title'].box;
  await page.mouse.move(title.x + 5, title.y + 5);
  await page.mouse.down();
  await page.mouse.move(title.x + 105, title.y - 45, { steps: 5 });
  await page.mouse.up();
  const w1 = (await parts())['subtitle-window'].box;
  assert.deepEqual([Math.round(w1.x - w0.x), Math.round(w1.y - w0.y)], [100, -50]);

  const handle = (await parts())['resize-handle'].box;
  await page.mouse.move(handle.x + handle.w / 2, handle.y + handle.h / 2);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.w / 2 + 80, handle.y + handle.h / 2 + 40, { steps: 5 });
  await page.mouse.up();
  const w2 = (await parts())['subtitle-window'].box;
  assert.deepEqual([Math.round(w2.w - w1.w), Math.round(w2.h - w1.h)], [80, 40]);
  const saved = await worker.evaluate(async () => (await chrome.storage.local.get('windowPosition')).windowPosition);
  assert.deepEqual([parseFloat(saved.left), parseFloat(saved.top)], [w1.x, w1.y]);

  const click = async (cls) => { const b = (await parts())[cls].box; await page.mouse.click(b.x + b.w / 2, b.y + b.h / 2); };
  await click('minimize-btn');
  let p = await parts();
  assert.match(p['subtitle-window'].attrs.class, /\bminimized\b/);
  assert.equal(p['subtitle-content'].box, null);  // only the header is left
  await click('minimize-btn');
  p = await parts();
  assert.doesNotMatch(p['subtitle-window'].attrs.class, /\bminimized\b/);
  assert.ok(p['subtitle-content'].box);

  await click('close-btn');
  assert.equal(await page.evaluate(() => !!document.querySelector('yume-subtitles')), false);
  assert.equal((await toTab({ action: 'GET_STATUS' })).active, false);  // closing the window stops the session

  await toTab({ action: 'TOGGLE_SUBTITLES' });
  await until('the window again', async () => (await parts())['subtitle-window']?.box);
  await sleep(500);  // its 0.25 s fade-in slides it up 8 px
  const w3 = (await parts())['subtitle-window'].box;
  assert.deepEqual([w3.x, w3.y, w3.w], [w1.x, w1.y, w2.w]);  // where it was left, as wide
  await seek(33.5);  // line 8
  await until('line 8 in the reopened window', async () => (await overlay()).english === 'Line 8');
});

test('the keyboard shortcut toggles the subtitles on the active tab', async () => {
  await page.bringToFront();
  await worker.evaluate(() => globalThis.onShortcut('toggle-subtitles'));  // what Alt+Y runs
  assert.equal((await toTab({ action: 'GET_STATUS' })).active, false);
  await worker.evaluate(() => globalThis.onShortcut('some-other-command'));  // ignored
  assert.equal((await toTab({ action: 'GET_STATUS' })).active, false);
  await worker.evaluate(() => globalThis.onShortcut('toggle-subtitles'));
  assert.equal((await toTab({ action: 'GET_STATUS' })).active, true);
  await seek(37.5);  // line 9
  await until('line 9 after the shortcut', async () => (await overlay()).english === 'Line 9');
});

test('in fullscreen the window moves into the fullscreen element, and back after', async () => {
  await page.bringToFront();
  await page.evaluate(() => {
    const b = document.createElement('button');
    b.id = 'fs';
    b.textContent = 'fullscreen';
    b.onclick = () => document.body.requestFullscreen();
    document.body.appendChild(b);
  });
  const parent = () => page.evaluate(() => document.querySelector('yume-subtitles')?.parentElement?.tagName);
  assert.equal(await parent(), 'HTML');
  await page.click('#fs');  // fullscreen needs a user gesture
  await until('fullscreen', () => page.evaluate(() => document.fullscreenElement === document.body));
  await until('the window inside the fullscreen element', async () => (await parent()) === 'BODY');
  assert.equal((await overlay()).english, 'Line 9');  // still drawn
  await page.evaluate(() => document.exitFullscreen());
  await until('the window back on the page', async () => (await parent()) === 'HTML');
  await page.evaluate(() => document.getElementById('fs').remove());
});

test('popup: Diagnostics shows the job and its log; Export and Download Log save files', async () => {
  const popup = await openPopup();
  await openSection(popup, 'diagnostics');
  await until('the job status', async () => /^DONE \| regions \d+\/\d+ \| lines \d+/.test(await popup.textContent('#diagStatus')));
  assert.ok(await popup.locator('#diagLog .diag-entry').count() > 0, 'log entries');

  const save = async (button) => {
    const [dl] = await Promise.all([popup.waitForEvent('download'), clickIn(popup, button)]);
    return { name: dl.suggestedFilename(), body: readFileSync(await dl.path(), 'utf8') };
  };
  const srt = await save('#exportSrt');
  assert.equal(srt.name, 'yume-Third_Song.srt');
  assert.match(srt.body, /^1\r?\n00:00:01,000 --> 00:00:03,500\r?\n/);
  assert.match(srt.body, /第0行です\r?\nLine 0/);
  assert.match(await popup.textContent('#exportStatus'), /Exported \d+ subtitles/);
  const vtt = await save('#exportVtt');
  assert.match(vtt.body, /^WEBVTT/);
  const log = await save('#downloadDiag');
  assert.match(log.name, /^yume-diag-.*\.json$/);
  const diag = JSON.parse(log.body);
  assert.equal(diag.status, 'done');
  assert.match(diag.videoId, /v=three/);
  await popup.close();
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
