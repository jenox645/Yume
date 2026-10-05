"""HuggingFace browser: pick and download GGUF translation models."""

from __future__ import annotations

import logging
import re

from yume.hardware import detect_gpu
from yume.network import (
    hf_download,
    hf_list_gguf,
)
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
from yume.utils import GGUF_DIR, GiB, KiB, MiB

from yume.menus import _shared

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


def browse_hf(cfg: dict) -> None:
    """Browse and download GGUF models from HuggingFace."""
    from config import save_config

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

        opts = list(_HF_RECOMMENDED_REPOS)
        opts.append(("Other repository...", "Type any HuggingFace repo (owner/model-name)"))
        opts.append(("Back", None))
        ch = ask_arrow("Pick a model:", opts, default=0)
        if ch == -1 or ch == len(opts) - 1:
            return
        if ch == len(_HF_RECOMMENDED_REPOS):
            repo = ask_input("HuggingFace repo (owner/model-name)", "")
            if not repo:
                continue
        else:
            repo = _HF_RECOMMENDED_REPOS[ch][0]

        info(f"Fetching file list from {repo}...")
        files = hf_list_gguf(repo)
        if not files:
            warn("No .gguf files found. Use a GGUF repo (usually has '-GGUF' suffix).")
            pause()
            continue

        entries = _group_gguf_parts(files)

        # Build labels: honest fit tag (total size vs VRAM) + ★ on the pick
        # we'd recommend (largest Q4_K_M that fits comfortably).
        def _fits(e: dict) -> str:
            if not gpu["has_nvidia"]:
                return ""
            sg = e["bytes"] / GiB
            if sg * 1.15 < vram_gb:
                return "ok"
            if sg < vram_gb:
                return "tight"
            return "no"

        star_idx = -1
        for i, e in enumerate(entries):  # entries are size-ascending
            if "q4_k_m" in e["name"].lower() and _fits(e) in ("ok", ""):
                star_idx = i

        fopts = []
        for i, e in enumerate(entries):
            fit = {
                "ok": f"  {C.GREEN}✓ fits your GPU{C.RESET}",
                "tight": f"  {C.YELLOW}~ tight fit{C.RESET}",
                "no": f"  {C.RED}✗ too large ({vram_gb:.0f} GB VRAM){C.RESET}",
                "": "",
            }[_fits(e)]
            parts_note = f", {len(e['parts'])} files" if len(e["parts"]) > 1 else ""
            star = f"  {C.GOLD}★ recommended{C.RESET}" if i == star_idx else ""
            fopts.append((f"{e['name']}  ({e['size']}{parts_note}){fit}{star}", None))
        fopts.append(("Back", None))

        fc = ask_arrow(f"Files in {repo}:", fopts, default=star_idx if star_idx >= 0 else len(fopts) - 1)
        if fc == -1 or fc == len(entries):
            continue

        sel = entries[fc]
        print()
        info(f"Model: {sel['name']}")
        info(f"Size:  {sel['size']}" + (f"  ({len(sel['parts'])} files)" if len(sel["parts"]) > 1 else ""))
        info(f"Dest:  {GGUF_DIR}")
        if _fits(sel) == "no":
            warn("Larger than your VRAM — it will run partly on CPU (much slower).")
        print()

        if ask_yn(f"Download {sel['name']}?"):
            ok = True
            for i, part in enumerate(sel["parts"], 1):
                if len(sel["parts"]) > 1:
                    info(f"Part {i}/{len(sel['parts'])}")
                if not hf_download(repo, part, sel.get("sha256", {}).get(part)):
                    error(f"Download failed: {part}")
                    ok = False
                    break
            if ok:
                first = sel["parts"][0]
                cfg["gguf_model_path"] = str(GGUF_DIR / first)
                cfg["translation_model"] = sel["name"]
                save_config(cfg)
                success(f"Saved to {GGUF_DIR / first}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch backend to llama.cpp to use this model?"):
                        bi = _shared.BI.get("llamacpp", {})
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi.get("dh", "127.0.0.1")
                        cfg["translation_port"] = bi.get("dp", 5000)
                        save_config(cfg)
        pause()
        return
