"""`stats` command: what the running Yume server reports."""

from __future__ import annotations

from yume.network import server_get as _server_get
from yume.ui import C, error, header, info, section, success


def _server_down(h: str, p: int) -> None:
    error(f"Whisper server not reachable at {h}:{p}")
    info(f"Start Yume first: {C.CYAN}python pocket_yume.py launch{C.RESET}  (or Start Yume in the extension popup)")


def cli_server_stats(cfg: dict) -> bool:
    """Print server stats; False when the server is not running."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    if not data:
        _server_down(h, p)
        return False

    header("Server Statistics")
    gpu = data.get("gpu")
    if gpu:
        pct = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100) if gpu["vram_total_mb"] else 0
        bar_w = 30
        filled = round(bar_w * pct / 100)
        bar = f"[{'#' * filled}{'-' * (bar_w - filled)}] {pct}%"
        success(f"GPU: {gpu['gpu_name']}")
        info(f"  VRAM: {gpu['vram_used_mb']}/{gpu['vram_total_mb']} MB  {bar}")
        info(f"  Util: {gpu.get('gpu_util_pct') or '?'}%  |  Temp: {gpu.get('gpu_temp_c') or '?'}C")
    else:
        info("GPU: N/A (CPU mode or nvidia-smi unavailable)")
    print()
    section("Whisper Engine")
    info(
        f"Model: {C.BOLD}{data.get('model', '?')}{C.RESET}  ({data.get('device', '?')}/{data.get('compute_type', '?')})"
    )
    info(f"Uptime: {data.get('uptime_human', '?')}")
    section("Session")
    info(f"Sections transcribed:    {data.get('regions_transcribed', 0)}")
    info(f"Lines produced:          {data.get('segments_produced', 0)}")
    info(f"Hallucinations blocked:  {data.get('hallucinations_filtered', 0)}")
    info(f"Audio processed:         {data.get('total_audio_seconds', 0):.0f}s")
    info(f"Avg Whisper time:        {data.get('avg_whisper_time', 0)}s per section")
    info(
        f"Lines translated:        {data.get('lines_translated', 0)}  (cache hits: {data.get('translation_cache_hits', 0)})"
    )
    info(f"Saved videos:            {data.get('library_size', 0)}")
    info(f"Active jobs:             {data.get('active', 0)} of {data.get('jobs', 0)}")
    info(f"Blacklist size:          {data.get('blacklist_size', 0)} items")
    return True
