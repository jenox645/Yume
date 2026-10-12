"""HuggingFace browser: pick and download GGUF translation models."""

from __future__ import annotations

import logging
import re

from yume.constants import GGUF_DIR, KiB, MiB, GiB, use_backend
from yume.hardware import detect_gpu
from yume.network import hf_download, hf_list_gguf
from yume.ui import (
    C,
    ask_arrow,
    ask_input,
    ask_yn,
    error,
    header,
    info,
    pause,
    success,
    warn,
)


_log = logging.getLogger("pocket_yume")


# ── HuggingFace browser ────────────────────────────────────────────────────────


# Curated starting points — a pick list, so nothing needs to be retyped
_HF_RECOMMENDED_REPOS = [
    ("Qwen/Qwen2.5-7B-Instruct-GGUF", "Best quality/VRAM balance for JP→EN — ~4.4 GB in Q4_K_M"),
    ("Qwen/Qwen2.5-14B-Instruct-GGUF", "Noticeably better translations — ~8.7 GB in Q4_K_M"),
    ("Qwen/Qwen2.5-3B-Instruct-GGUF", "Light — for CPU mode or small GPUs, ~2 GB in Q4_K_M"),
    ("bartowski/gemma-2-9b-it-GGUF", "Google Gemma 2 alternative — ~5.8 GB in Q4_K_M"),
]

# "model-q4_0-00001-of-00002.gguf" → split file; strip the part suffix to group
_GGUF_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)")


def _group_gguf_parts(files: list[dict]) -> list[dict]:
    """Collapse multi-part GGUFs into single logical entries.

    llama.cpp needs EVERY part of a split model — offering individual parts
    invites downloading a useless fragment, and per-part sizes make the
    VRAM-fit tag lie (four 3.6 GB parts each "fit" a 12 GB card; the 14 GB
    whole does not).
    """
    groups: dict[str, dict] = {}
    for f in files:
        base = _GGUF_SPLIT_RE.sub("", f["name"])
        g = groups.setdefault(base, {"name": base, "bytes": 0, "parts": [], "sha256": {}})
        g["bytes"] += f["bytes"]
        g["parts"].append(f["name"])
        g["sha256"][f["name"]] = f.get("sha256")
    out = []
    for g in groups.values():
        g["parts"].sort()  # 00001 first — llama.cpp loads from the first shard
        sb = g["bytes"]
        g["size"] = f"{sb / GiB:.2f} GB" if sb >= GiB else f"{sb / MiB:.0f} MB"
        out.append(g)
    out.sort(key=lambda g: g["bytes"])
    return out


def _fit(size_bytes: int, vram_gb: float) -> str:
    """How a model fits in VRAM: "ok", "tight" (no room for the context), "no"; "" without a GPU."""
    if not vram_gb:
        return ""
    gb = size_bytes / GiB
    if gb * 1.15 < vram_gb:
        return "ok"
    return "tight" if gb < vram_gb else "no"


def _fit_label(fit: str, vram_gb: float) -> str:
    # Built per call: --no-color empties the colour codes after import
    return {
        "ok": f"  {C.GREEN}✓ fits your GPU{C.RESET}",
        "tight": f"  {C.YELLOW}~ tight fit{C.RESET}",
        "no": f"  {C.RED}✗ too large ({vram_gb:.0f} GB VRAM){C.RESET}",
    }.get(fit, "")


def _pick_repo() -> str | None:
    """A repository from the curated list or typed in; "" to ask again, None for Back."""
    opts = [
        *_HF_RECOMMENDED_REPOS,
        ("Other repository...", "Type any HuggingFace repo (owner/model-name)"),
        ("Back", None),
    ]
    ch = ask_arrow("Pick a model:", opts, default=0)
    if ch == -1 or ch == len(opts) - 1:
        return None
    if ch == len(_HF_RECOMMENDED_REPOS):
        return ask_input("HuggingFace repo (owner/model-name)", "")
    return _HF_RECOMMENDED_REPOS[ch][0]


def _pick_file(repo: str, entries: list[dict], vram_gb: float) -> dict | None:
    """One (possibly split) model from the repo; ★ on the largest Q4_K_M that fits comfortably."""
    star = -1
    for i, e in enumerate(entries):  # size-ascending
        if "q4_k_m" in e["name"].lower() and _fit(e["bytes"], vram_gb) in ("ok", ""):
            star = i
    opts = []
    for i, e in enumerate(entries):
        parts_note = f", {len(e['parts'])} files" if len(e["parts"]) > 1 else ""
        fit = _fit_label(_fit(e["bytes"], vram_gb), vram_gb)
        rec = f"  {C.GOLD}★ recommended{C.RESET}" if i == star else ""
        opts.append((f"{e['name']}  ({e['size']}{parts_note}){fit}{rec}", None))
    opts.append(("Back", None))
    ch = ask_arrow(f"Files in {repo}:", opts, default=star if star >= 0 else len(opts) - 1)
    return entries[ch] if 0 <= ch < len(entries) else None


def _download_model(cfg: dict, repo: str, sel: dict, vram_gb: float) -> None:
    """Download every part, then make it the translation model."""
    from config import save_config

    parts = sel["parts"]
    print()
    info(f"Model: {sel['name']}")
    info(f"Size:  {sel['size']}" + (f"  ({len(parts)} files)" if len(parts) > 1 else ""))
    info(f"Dest:  {GGUF_DIR}")
    if _fit(sel["bytes"], vram_gb) == "no":
        warn("Larger than your VRAM — it will run partly on CPU (much slower).")
    print()
    if not ask_yn(f"Download {sel['name']}?"):
        return
    for i, part in enumerate(parts, 1):
        if len(parts) > 1:
            info(f"Part {i}/{len(parts)}")
        if not hf_download(repo, part, sel.get("sha256", {}).get(part)):
            error(f"Download failed: {part}")
            return
    # llama.cpp loads a split model from its first part
    cfg["gguf_model_path"] = str(GGUF_DIR / parts[0])
    cfg["translation_model"] = sel["name"]
    save_config(cfg)
    success(f"Saved to {GGUF_DIR / parts[0]}")
    if cfg["translation_backend"] != "llamacpp" and ask_yn("Switch backend to llama.cpp to use this model?"):
        use_backend(cfg, "llamacpp")
        save_config(cfg)


def browse_hf(cfg: dict) -> None:
    """Browse and download GGUF models from HuggingFace."""
    while True:
        header("Download Translation Model")
        gpu = detect_gpu()
        vram_gb = gpu["vram_mb"] / KiB if gpu["has_nvidia"] else 0.0
        if gpu["has_nvidia"]:
            info(f"Your GPU: {gpu['name']} — {C.BOLD}{vram_gb:.0f} GB VRAM{C.RESET}")
            info(f"{C.DIM}Rule of thumb: biggest Q4_K_M that fits with ~1 GB spare.{C.RESET}")
        else:
            info(f"Hardware: {C.YELLOW}CPU mode{C.RESET} — prefer 3B models in Q4_K_M.")
        info(f"{C.DIM}More detail: Help & Guides → Pick the right models for your PC.{C.RESET}")

        repo = _pick_repo()
        if repo is None:
            return
        if not repo:
            continue
        info(f"Fetching file list from {repo}...")
        files = hf_list_gguf(repo)
        if not files:
            warn("No .gguf files found. Use a GGUF repo (usually has '-GGUF' suffix).")
            pause()
            continue
        sel = _pick_file(repo, _group_gguf_parts(files), vram_gb)
        if sel is None:
            continue
        _download_model(cfg, repo, sel, vram_gb)
        pause()
        return
