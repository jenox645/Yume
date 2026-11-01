"""YouTube PO tokens: the bgutil plugin for yt-dlp and its token server (run by Deno)."""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

from yume.constants import TOOLS_DIR
from yume.ui import C, error, info, success, warn
from yume.utils import _run, find_tool, safe_extractall

BGUTIL_VERSION = "1.3.1"


def install_plugin() -> None:
    info("Installing YouTube PO token plugin (bgutil-ytdlp-pot-provider) + yt-dlp (pip)...")
    info(f"{C.DIM}This plugin uses Deno to solve YouTube's bot-detection challenges.{C.RESET}")
    info(f"{C.DIM}yt-dlp is left unpinned on purpose: YouTube changes break old versions within weeks.{C.RESET}")
    try:
        r = _run(
            [
                sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", "-U",
                "yt-dlp", f"bgutil-ytdlp-pot-provider=={BGUTIL_VERSION}",
            ],
            timeout=180,
        )  # fmt: skip
        if r.returncode == 0:
            success("PO token plugin installed")
        else:
            warn("PO token plugin install failed — YouTube may require sign-in")
    except Exception as e:
        warn(f"PO token plugin install failed: {e}")


def setup_server() -> None:
    """The plugin's token server (run by Deno), from its release source."""
    from yume.network import download_file

    info("Setting up bgutil PO token server...")
    bgutil_dir = TOOLS_DIR / "bgutil-ytdlp-pot-provider"
    main_ts = bgutil_dir / "server" / "src" / "main.ts"
    if main_ts.exists():
        success("bgutil server already set up")
        return
    zip_url = f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{BGUTIL_VERSION}.zip"
    zip_path = TOOLS_DIR / "bgutil.zip"
    if not download_file(zip_url, zip_path, "bgutil server"):
        warn("bgutil server download failed")
        return
    try:
        with zipfile.ZipFile(zip_path) as zf:
            safe_extractall(zf, TOOLS_DIR)  # nosec B202 — zip-slip checked inside
        zip_path.unlink(missing_ok=True)
        extracted = TOOLS_DIR / f"bgutil-ytdlp-pot-provider-{BGUTIL_VERSION}"
        if extracted.exists():
            if bgutil_dir.exists():
                shutil.rmtree(bgutil_dir)
            extracted.rename(bgutil_dir)
        success("bgutil server extracted")
    except Exception as e:
        error(f"Extract failed: {e}")
        zip_path.unlink(missing_ok=True)
        return
    if main_ts.exists():
        _deno_install_deps(bgutil_dir / "server")
    else:
        warn("main.ts not found after extraction")


def _deno_install_deps(server_dir: Path) -> None:
    deno = find_tool("deno") or "deno"
    info("Installing bgutil server dependencies (deno install)...")
    try:
        # The lockfile first; without it if the lockfile no longer resolves
        r = _run([deno, "install", "--allow-scripts=npm:canvas", "--frozen"], cwd=str(server_dir), timeout=180)
        if r.returncode != 0:
            r = _run([deno, "install", "--allow-scripts=npm:canvas"], cwd=str(server_dir), timeout=180)
        if r.returncode == 0:
            success("bgutil server dependencies installed")
        else:
            warn("deno install failed — server may not start")
    except Exception as e:
        warn(f"deno install failed: {e}")
