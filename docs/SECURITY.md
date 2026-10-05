# Security Policy

## How Yume protects your data

Yume runs entirely on your machine — no cloud APIs, no telemetry, no analytics. The only
network traffic is downloading the audio of the videos you watch (and, during setup, the
tools and models you choose to install).

**Security layers:**
- Per-session API token (random 32 bytes via `secrets.token_urlsafe`) required on all server endpoints except `/health`
- CORS headers only for `chrome-extension://` and `moz-extension://` origins; `/health` hands the token only to those origins and to local callers that send no `Origin` header
- Host header validation blocks DNS rebinding attacks (rejects non-localhost requests); the server binds `127.0.0.1` only
- All URLs validated before passing to subprocess (prevents argument injection); request bodies capped at 2 MB
- The extension's background worker only forwards an allowlist of server paths
- No shell invocation — all subprocess calls use explicit argv lists; no `shell=True`, no `os.system()`, no `curl | sh`
- XSS prevention — popup `innerHTML` content is escaped with `_escapeHtml()` (quotes included); the subtitle overlay uses `textContent` inside a closed Shadow DOM
- Python dependencies pinned to exact versions (`==`), except `yt-dlp` (must track YouTube) and `llama-cpp-python` (wheel availability)
- Browser cookies accessed read-only for YouTube authentication (never modified or stored)
- The one-click start helper (native messaging host) is registered per user, callable only by the Yume extension's ID, and only starts/stops Yume's own servers; `autostart off` removes it

## Threat model

The token protects the server from **web pages**: a site you visit cannot read it,
cannot call the API cross-origin, and cannot reach it through DNS rebinding.

It does **not** isolate Yume from software already running as your user: local
programs can call `/health` without an `Origin` header (or read `.yume_token`), and any
installed browser extension receives the token from `/health`. Treat the Yume server
like any other local service — only install extensions and programs you trust.

## Reporting a vulnerability

If you find a security issue, please **do not open a public GitHub issue**.

Instead, [open a private security advisory](https://github.com/jenox645/Yume/security/advisories/new) on GitHub.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

I'll respond within 7 days and work with you on a fix before any public disclosure.

## Supported versions

Only the latest release receives security fixes. Please update to the newest version.
