# Security Policy

## How Yume protects your data

Yume runs entirely on your machine. No data leaves your computer — no cloud APIs, no telemetry, no analytics.

**Security layers:**
- Per-session API token (random 32 bytes via `secrets.token_urlsafe`) required on all server endpoints except `/health`
- Token discovery restricted to `chrome-extension://` and `moz-extension://` origins — web pages cannot obtain it
- Host header validation blocks DNS rebinding attacks (rejects non-localhost requests)
- All URLs validated before passing to subprocess (prevents argument injection)
- No shell invocation — all subprocess calls use explicit argv lists; no `shell=True`, no `os.system()`, no `curl | sh`
- XSS prevention — all dynamic content in popup innerHTML is escaped via `_escapeHtml()`; subtitle overlay uses `textContent` only
- All Python dependencies pinned to exact versions (`==`) to prevent supply-chain attacks from untested upgrades
- Browser cookies accessed read-only for YouTube authentication (never modified or stored)

## Reporting a vulnerability

If you find a security issue, please **do not open a public GitHub issue**.

Instead, email the maintainer directly: [open a private security advisory](https://github.com/jenox645/Yume/security/advisories/new) on GitHub.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

I'll respond within 7 days and work with you on a fix before any public disclosure.

## Supported versions

Only the latest release receives security fixes. Please update to the newest version.
