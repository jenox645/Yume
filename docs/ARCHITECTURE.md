# Architecture

## Overview

Three processes on the user's machine:

```text
┌─ Browser extension ─────────────────┐      ┌─ Yume server (port 5001) ─────────────────────────┐
│ content.js   page lifecycle, SPA nav │      │ faster_whisper_server.py   routes, auth, startup   │
│ session.js   create job, poll, render│ ───▶ │ _jobs.py       job manager + 2 worker threads      │
│ subtitle-window.js  shadow-DOM overlay│ HTTP │ _audio.py      yt-dlp / ffmpeg download            │
│ background.js  authenticated proxy   │ ◀─── │ _regions.py    split audio at quiet points         │
│ popup.js     settings, library, stats│      │ _transcribe.py Whisper (music-tuned params)         │
└─────────────────────────────────────┘      │ _filter.py     hallucination filter + blacklist    │
                                             │ _translate.py  LLM client, structured output        │
                                             │ _romanize.py   pykakasi / pypinyin / KO / RU       │
                                             │ _store.py      SQLite cache (config/yume_cache.db) │
                                             └───────────────────────┬────────────────────────────┘
                                                                     │ OpenAI-compatible API
                                             ┌───────────────────────▼────────────────────────────┐
                                             │ Translation LLM: llama.cpp (5000) / Ollama (11434)  │
                                             │ / LM Studio / any /v1/chat/completions server       │
                                             └────────────────────────────────────────────────────┘

CLI: pocket_yume.py (entry point) + yume/ package — setup, launch, menus, health, installers.
```

The extension only renders. Everything that costs time or needs to persist —
download, transcription, translation, romanization, caching, the blacklist — is
in the server. (Up to v0.1.0 the extension orchestrated the pipeline itself
from a content script and an MV3 service worker; see "Why server-side" below.)

## Data flow

1. **Enable** in the popup → `content.js` finds the main video (largest visible)
   and starts a `SubtitleSession`.
2. `session.js` waits for `/health` = `ready`, then `POST /jobs` with the page
   URL, a stable video id, source/target language, romanization flag, duration.
   Same video + language + target + Whisper model → the server returns the
   existing job.
3. The server (`_jobs.JobManager`):
   - **Cache first.** If every region of this video is cached for this language
     and model, the job is done immediately — no download.
   - **Download** the full audio once (`_audio.download_full_audio`, several
     yt-dlp auth strategies, ffmpeg fallback; `download_direct` for a custom
     stream URL). Meanwhile a **stream preview** transcribes the first 30 s
     straight from the stream so subtitles start before the download ends.
   - **Plan regions** (`_regions.plan_regions`): region 0 is `[0, 30)`, then a cut
     every ~26 s at the quietest point in a ±5 s window. Regions are exclusive —
     no overlap, nothing to de-duplicate.
   - **Transcription worker** (one thread, the model is not thread-safe): takes
     the region under the playhead of the most recently polled job, then the
     next ~10, then earlier ones. Raw Whisper segments go to the cache.
   - **Filter**: built-in hallucination patterns, credits lines and the user
     blacklist mark segments `hidden` (raw text is kept, so blacklist edits apply
     retroactively).
   - **Romanize** deterministically (JA pykakasi, ZH pypinyin, KO Revised
     Romanization, RU BGN/PCGN); Arabic, and JA/ZH without the libraries, go to
     the LLM when the user enabled romanization.
   - **LLM worker** (one thread): batches of ≤10 untranslated lines, playhead
     first, with the video title and the previous 3 line pairs as context.
4. `session.js` polls `GET /jobs/<id>?since=<rev>&t=<playhead>` every second
   (4 s once done, 5 s in a background tab). Every segment change bumps the
   job's revision; a poll returns only segments changed since `since`. Hidden
   segments are sent so the client removes them.
5. On `timeupdate` the session binary-searches the cue that started last; the
   overlay shows it until its end (+1 s grace).

A 404 on poll (server restart, cache clear, Whisper model switch) makes the
session recreate its job, which reloads whatever is cached.

## Translation

`_translate.Translator` calls `/v1/chat/completions` on the configured backend.
Batches request **structured output**: `response_format` with a JSON schema of
exactly N strings (llama.cpp, Ollama ≥ 0.5, LM Studio), which makes dropped or
merged lines impossible. If the backend rejects `json_schema` it tries
`json_object`, then plain numbered lines, and remembers what worked per
endpoint. Missing lines and CJK leaking into a non-CJK target get one
single-line retry. The model name is sent for every backend except llama.cpp
(Ollama and LM Studio reject requests without it). A down server backs off
instead of giving lines up.

## Cache (`config/yume_cache.db`)

| Table | Key | Content |
|-------|-----|---------|
| `videos` | video_key | URL, title, duration, region plan |
| `transcripts` | video, language, Whisper model, region | raw segments (pre-filter) |
| `translations` | source, target, LLM model, text | translation |
| `romanizations` | language, text | LLM romanization |

Pruned to the newest 200 videos and 50,000 translations. The popup's History
panel and `/library` list it; `/library/export` builds SRT/VTT without a job.

## Translation engine

For the `llamacpp` backend Yume runs llama.cpp's official prebuilt server
(`tools/llama.cpp/llama-server`, installed by `yume/llama_server.py` from the
newest numbered GitHub release that has a build for the machine: the newest
CUDA build the NVIDIA driver supports plus its CUDA runtime, Vulkan for other
GPUs, Metal on Apple Silicon, else CPU). It is started with one slot and the
whole 4096-token context. When it is not installed, `llama_cpp.server`
(llama-cpp-python) is used as before. The translator keeps its system prompt
identical for a whole video so the server's prompt cache is reused; the
per-batch context goes into the user message.

## One-click start (native messaging)

```
popup / session.js ──{type:'YUME', cmd}──▶ background.js
    ──runtime.sendNativeMessage('com.pocketyume.yume')──▶ config/native_host/yume_host.bat|.sh
    ──▶ yume/native_host.py ──start──▶ pythonw pocket_yume.py serve   (detached)
                                             ├─ llama.cpp / Ollama   (hidden, logs/translation_server.log)
                                             └─ Whisper server        (hidden, logs/whisper_server.log)
```

- `python pocket_yume.py autostart on` (also offered by the setup wizard and
  Settings → One-click start) writes the launcher and host manifests to
  `config/native_host/` and registers them: HKCU registry keys on Windows
  (Chrome, Edge, Brave, Chromium, Firefox), `NativeMessagingHosts/` folders on
  Linux/macOS. Chromium manifests allow only the extension ID derived from the
  manifest `key`; the Firefox one only the gecko id.
- The host answers `ping` / `status` / `start` / `stop`. `start` does nothing if
  a server is already up (e.g. started from the launcher) or already starting.
- Browsers run hosts in a kill-on-close job object on Windows; `serve` is spawned
  with `CREATE_BREAKAWAY_FROM_JOB`, falling back to `Win32_Process.Create` (WMI)
  when the job forbids breakaway, so it outlives the host.
- `serve` (`yume/service.py`) reuses the launcher's command builders, writes its
  state to `config/service.json` (`starting` + message → `running`; a failure
  stays readable as `last_error`), and stops on `config/service.stop`, when a
  child dies, or after `auto_stop_minutes` (default 30, 0 = never) with no job
  polled (`/stats` → `active`).
- When the extension's `/health` check finds no server, `session.js` sends
  `start` and shows the supervisor's progress message until the server is ready.
  Without the helper it shows the "start it with START_YUME" hint as before.

## Security

| Layer | Mechanism |
|-------|-----------|
| Token | `secrets.token_urlsafe(32)` per server run, required on every endpoint except `/health`. |
| Discovery | `/health` hands the token only to browser-extension origins and to callers without an Origin (local tools). |
| CORS | Headers only for `chrome-extension://` / `moz-extension://` origins. |
| Host | Requests whose `Host` is not `127.0.0.1`/`localhost` are rejected (DNS rebinding). |
| Input | URLs validated before any subprocess; language codes `^[a-z]{2,3}$`; 2 MB body limit. |
| Proxy | `background.js` forwards only an allowlist of server paths. |
| Native host | Callable only by the Yume extension's ID; accepts `ping`/`status`/`start`/`stop` with a 64 KB message cap. |
| Overlay | Closed Shadow DOM; subtitle text via `textContent`; popup HTML escaped (quotes included). |

See [SECURITY.md](SECURITY.md) for the threat model.

## Why server-side

The old extension-side pipeline needed: fixed 30 s chunks with a 5 s overlap
and two de-duplication heuristics; empty-chunk streak detection; a race between
the first chunk and the download; three stacked translation retry layers around
free-text `[N]` parsing; translation caches in a service worker that Chrome
kills after 30 s idle; and a per-browser subtitle library. Moving the pipeline
into the long-lived Python process removed ~3,800 lines of JavaScript and gave
one place for prompts, caching, the blacklist and scheduling.

## Key decisions

- **Download once, transcribe ahead** — instead of capturing tab audio live:
  subtitles are ready before playback reaches them and a full SRT can be
  exported. Cost: only sites yt-dlp/ffmpeg can fetch (no DRM).
- **VAD off, no word timestamps** — Silero VAD drops singing; the
  word-timestamp decode path drops segments. Music is the main use case.
- **No build step** for the extension — plain scripts, one cross-browser
  manifest (`service_worker` for Chromium, `scripts` for Firefox 121+).
- **CLI package layout** — `pocket_yume.py` is the entry point; logic lives in
  `yume/`, with shared state injected through `set_*()` setters.
