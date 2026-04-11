"""Whisper model benchmarking."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from yume.hardware import IS_WIN, detect_gpu, recommend_whisper_model
from yume.ui import C, ask_input, ask_yn, error, header, info, pause, section, success, table, warn
from yume.utils import LOGS_DIR, find_tool, _run

_log = logging.getLogger("pocket_yume")

WHISPER_SAMPLE_RATE = 16000
KiB = 1024
GiB = 1024**3

WHISPER_MODELS_INFO = [
    # (name, params, vram, user-friendly description)
    ("tiny", "39M params", "~1 GB", "Fastest, low accuracy"),
    ("base", "74M params", "~1 GB", "Fast, decent accuracy"),
    ("small", "244M params", "~2 GB", "Good balance of speed and accuracy"),
    ("medium", "769M params", "~5 GB", "High accuracy, slower"),
    ("large-v2", "1550M params", "~10 GB", "Very high accuracy"),
    ("large-v3", "1550M params", "~10 GB", "Best accuracy (recommended if VRAM allows)"),
    ("large-v3-turbo", "809M params", "~6 GB", "Near-v3 accuracy at 2x speed (best mid-range)"),
    ("distil-large-v2", "756M params", "~4 GB", "Compressed v2 -- 2x faster, similar accuracy"),
    ("distil-large-v3", "756M params", "~4 GB", "Compressed v3 -- 2x faster, similar accuracy"),
]


def _is_whisper_model_cached(model_name: str) -> bool:
    """Check if a Whisper model is already downloaded in the HuggingFace cache."""
    try:
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache_dir.exists():
            return False
        patterns = [
            f"models--Systran--faster-whisper-{model_name}",
            f"models--guillaumekln--faster-whisper-{model_name}",
            f"models--openai--whisper-{model_name}",
            f"models--mobiuslabsgmbh--faster-whisper-{model_name}",
        ]
        for entry in cache_dir.iterdir():
            for pat in patterns:
                if pat.lower() in entry.name.lower():
                    return True
        ct2_cache = Path.home() / ".cache" / "faster_whisper"
        if ct2_cache.exists():
            for entry in ct2_cache.iterdir():
                if model_name in entry.name:
                    return True
        return False
    except Exception:
        return False


def benchmark_whisper(cfg: dict) -> None:
    """Compare Whisper model speeds on this hardware."""
    from config import save_config

    header("Whisper Benchmark")
    info("This measures how fast each speech recognition model runs on your hardware.")
    info(f"{C.DIM}Yume processes audio in chunks. Faster models = subtitles appear sooner.{C.RESET}")
    info(f"{C.DIM}A model running at '10x realtime' transcribes 1 minute of audio in 6 seconds.{C.RESET}")
    print()

    try:
        import faster_whisper  # noqa: F401

        success("faster-whisper found")
    except ImportError:
        error("faster-whisper not installed. Run: pip install faster-whisper")
        pause()
        return

    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    elif gpu.get("has_amd"):
        info(f"GPU: {gpu['name']} ({gpu.get('vram_mb', '?')} MB VRAM, ROCm)")
    else:
        info("No GPU detected — benchmarking in CPU mode")

    rec, reason = recommend_whisper_model(gpu)
    info(f"Recommended: {rec} ({reason})")
    print()

    # Generate test audio using ffmpeg (5s of speech-like noise)
    ffmpeg = find_tool("ffmpeg")
    test_wav = LOGS_DIR / "_benchmark_test.wav"
    if ffmpeg:
        try:
            _run(
                [
                    ffmpeg, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=5",
                    "-ar", str(WHISPER_SAMPLE_RATE), "-ac", "1", str(test_wav),
                ],
                timeout=10,
            )
        except Exception as e:
            _log.debug("[benchmark_whisper] test-audio-gen failed: %s", e)

    if not test_wav.exists():
        try:
            import math
            import struct
            import wave

            sr = WHISPER_SAMPLE_RATE
            dur = 5
            samples = []
            for i in range(sr * dur):
                t = i / sr
                v = 0.5 * math.sin(2 * math.pi * 300 * t) + 0.3 * math.sin(2 * math.pi * 600 * t)
                samples.append(int(v * 32767))
            with wave.open(str(test_wav), "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            success("Generated test audio (5s)")
        except Exception as e:
            error(f"Could not create test audio: {e}")
            pause()
            return

    # Select models to benchmark
    gpu_has = gpu.get("has_nvidia") or gpu.get("has_amd")
    vram = gpu.get("vram_mb", 0) if gpu_has else 0
    available = []
    for name, params, vram_req, desc in WHISPER_MODELS_INFO:
        try:
            req_mb = int(vram_req.replace("~", "").replace("GB", "").strip()) * KiB
        except (ValueError, AttributeError):
            req_mb = 0
        fits = True
        if gpu_has and vram > 0 and req_mb > vram:
            fits = False
        if not gpu_has and req_mb > 5120:
            fits = False
        available.append((name, params, vram_req, desc, fits))

    info("Select models to benchmark:")
    info("  'distil' models = compressed versions (same accuracy, 2x faster, half the size)")
    info("  'turbo' = optimized large-v3 (near-full accuracy at much higher speed)")
    info("  VRAM = your GPU's video memory — models marked [fits] will work on your hardware")
    print()
    for i, (name, params, vr, desc, fits) in enumerate(available):
        tag = f"{C.GREEN}fits{C.RESET}" if fits else f"{C.RED}may OOM{C.RESET}"
        cached = _is_whisper_model_cached(name)
        dl_tag = f"{C.GREEN}downloaded{C.RESET}" if cached else f"{C.DIM}not downloaded{C.RESET}"
        cur = f" {C.GOLD}<- current{C.RESET}" if name == cfg.get("whisper_model") else ""
        print(f"    {i + 1:2d}. {name:22s} {vr:8s} [{tag}] [{dl_tag}]{cur}")
        print(f"        {C.DIM}{desc}{C.RESET}")
    print()

    selection = (
        ask_input("Enter model numbers (comma-separated) or 'rec' for recommended, 'all' for all that fit", "rec")
        .strip()
        .lower()
    )

    models_to_test: list[str] = []
    if selection == "all":
        models_to_test = [name for name, _, _, _, fits in available if fits]
    elif selection == "rec":
        models_to_test = [rec]
    else:
        for part in selection.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(available):
                    models_to_test.append(available[idx][0])

    if not models_to_test:
        warn("No models selected")
        pause()
        return

    info(f"Benchmarking {len(models_to_test)} model(s): {', '.join(models_to_test)}")

    needs_download = [m for m in models_to_test if not _is_whisper_model_cached(m)]
    if needs_download:
        warn(f"These models will be downloaded first: {', '.join(needs_download)}")
        if not ask_yn("Download and benchmark?", True):
            return
    else:
        info("All selected models are already downloaded (cached)")
        if not ask_yn("Start benchmark?", True):
            return

    # Determine device/compute
    if gpu.get("has_nvidia"):
        device = "cuda"
    elif gpu.get("has_amd") and not IS_WIN:
        device = "auto"
        info("AMD GPU detected — benchmarking via ROCm/auto")
    else:
        device = "cpu"
    vram_mb = gpu.get("vram_mb", 0)
    if device in ("cuda", "auto") and vram_mb >= 8000:
        compute = "float16"
    elif device in ("cuda", "auto") and vram_mb >= 4000:
        compute = "int8_float16"
    else:
        compute = "int8"

    # Run benchmarks
    results: list[dict] = []
    total = len(models_to_test)
    for idx, model_name in enumerate(models_to_test):
        section(f"Testing {idx + 1}/{total}: {model_name}")
        cached = _is_whisper_model_cached(model_name)
        if not cached:
            info(f"{C.DIM}Downloading model... (this may take a few minutes the first time){C.RESET}")
        info(f"Device: {device} | Precision: {compute}")

        try:
            from faster_whisper import WhisperModel
            import warnings

            warnings.filterwarnings("ignore", message=".*huggingface.*")
            warnings.filterwarnings("ignore", message=".*symlinks.*")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

            t0 = time.time()
            model = WhisperModel(model_name, device=device, compute_type=compute)
            load_time = time.time() - t0
            success(f"Loaded in {load_time:.1f}s")

            info(f"{C.DIM}Running 3 transcription passes (5s test audio each)...{C.RESET}")
            times = []
            segments_count = 0
            for _ in range(3):
                t0 = time.time()
                segs, _seg_info = model.transcribe(
                    str(test_wav), language="ja", vad_filter=False, word_timestamps=False,
                )
                seg_list = list(segs)
                elapsed = time.time() - t0
                times.append(elapsed)
                segments_count = len(seg_list)

            median_time = sorted(times)[len(times) // 2]
            rtf = median_time / 5.0
            speed = 5.0 / median_time if median_time > 0 else 0

            results.append({
                "model": model_name, "load_s": round(load_time, 1),
                "median_s": round(median_time, 3), "rtf": round(rtf, 3),
                "speed_x": round(speed, 1), "segments": segments_count, "status": "OK",
            })
            success(f"Median: {median_time:.3f}s for 5s audio ({speed:.1f}x realtime)")

            del model
            try:
                import gc
                gc.collect()
                if device == "cuda":
                    import torch
                    torch.cuda.empty_cache()
            except Exception as e:
                _log.debug("[benchmark_whisper] cuda-cleanup failed: %s", e)

        except Exception as e:
            err_msg = str(e)
            short_err = err_msg[:80]
            error(f"Failed: {short_err}")

            is_cuda_lib_error = any(
                lib in err_msg.lower()
                for lib in [
                    "cublas", "cudnn", "cudart", "cufft", "cusolver", "cusparse",
                    "nvcuda", "is not found or cannot be loaded", "cuda",
                ]
            )

            if is_cuda_lib_error and device in ("cuda", "auto"):
                print()
                warn("CUDA libraries are missing or incomplete.")
                info("This usually means the NVIDIA CUDA Toolkit isn't fully installed.")
                if IS_WIN:
                    info("Fix: Install CUDA Toolkit from https://developer.nvidia.com/cuda-downloads")
                    info("     Or install cuBLAS via: pip install nvidia-cublas-cu12")
                else:
                    info("Fix: Install CUDA Toolkit for your distro, or:")
                    info("     pip install nvidia-cublas-cu12")

                if ask_yn("Retry this model on CPU instead?", default=True):
                    try:
                        info(f"Retrying {model_name} on CPU...")
                        from faster_whisper import WhisperModel as WM2

                        t0 = time.time()
                        cpu_model = WM2(model_name, device="cpu", compute_type="int8")
                        load_time = time.time() - t0
                        success(f"Loaded on CPU in {load_time:.1f}s")

                        info(f"{C.DIM}Running 3 transcription passes (5s test audio each)...{C.RESET}")
                        cpu_times = []
                        for _ in range(3):
                            t0 = time.time()
                            segs2, _ = cpu_model.transcribe(
                                str(test_wav), language="ja", vad_filter=False, word_timestamps=False
                            )
                            list(segs2)
                            cpu_times.append(time.time() - t0)

                        median_time = sorted(cpu_times)[len(cpu_times) // 2]
                        speed = 5.0 / median_time if median_time > 0 else 0
                        results.append({
                            "model": f"{model_name} (CPU)", "load_s": round(load_time, 1),
                            "median_s": round(median_time, 3), "rtf": round(median_time / 5.0, 3),
                            "speed_x": round(speed, 1), "segments": 0, "status": "OK",
                        })
                        success(f"CPU median: {median_time:.3f}s for 5s audio ({speed:.1f}x realtime)")
                        del cpu_model
                    except Exception as e2:
                        error(f"CPU fallback also failed: {str(e2)[:60]}")
                        results.append({
                            "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                            "speed_x": 0, "segments": 0, "status": f"FAIL: {short_err}",
                        })
                else:
                    results.append({
                        "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                        "speed_x": 0, "segments": 0, "status": f"FAIL: {short_err}",
                    })
            else:
                results.append({
                    "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                    "speed_x": 0, "segments": 0, "status": f"FAIL: {short_err}",
                })

    # Clean up test audio
    test_wav.unlink(missing_ok=True)

    # Display results
    print()
    section("Benchmark Results")
    info(f"Device: {device} | Precision: {compute} | Test: 5s audio, 3 runs (median)")
    if gpu.get("has_nvidia"):
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    print()

    rows = []
    best_speed = max((r["speed_x"] for r in results if r["status"] == "OK"), default=0)
    for r in results:
        if r["status"] == "OK":
            bar_len = min(25, int(r["speed_x"] / max(best_speed, 1) * 25))
            speed_bar = f"{C.GREEN}{'█' * bar_len}{C.DIM}{'░' * (25 - bar_len)}{C.RESET}"
            is_best = f" {C.GOLD}★{C.RESET}" if r["speed_x"] == best_speed and len(results) > 1 else ""
            rows.append([
                r["model"], f"{r['load_s']}s", f"{r['median_s']:.3f}s",
                f"{r['speed_x']}x{is_best}", speed_bar,
            ])
        else:
            rows.append([r["model"], "-", "-", "-", f"{C.RED}{r['status']}{C.RESET}"])

    table(
        ["Model", "Load", "5s Audio", "Speed", ""],
        rows,
        col_styles=[C.CYAN, C.RESET, C.RESET, C.GREEN, C.RESET],
        title="Whisper Benchmark",
    )

    if results:
        ok_results = [r for r in results if r["status"] == "OK"]
        if ok_results:
            fastest = min(ok_results, key=lambda r: r["median_s"])
            print()
            info("How to read the results:")
            info(f"  {C.DIM}Speed = how many times faster than real-time. Higher is better.{C.RESET}")
            info(f"  {C.DIM}10x = 1 minute of audio transcribed in 6 seconds{C.RESET}")
            info(f"  {C.DIM}50x = 1 minute of audio transcribed in ~1 second{C.RESET}")
            print()
            success(f"Fastest: {C.BOLD}{fastest['model']}{C.RESET} at {C.GREEN}{fastest['speed_x']}x{C.RESET} realtime")

            if fastest["speed_x"] > 0:
                mins_per_min = 60.0 / fastest["speed_x"]
                info(f"  {C.DIM}→ A 4-minute song would be transcribed in ~{mins_per_min * 4:.1f} seconds{C.RESET}")

            if fastest["model"] != cfg.get("whisper_model"):
                print()
                if ask_yn(f"Switch to {fastest['model']}?", default=False):
                    cfg["whisper_model"] = fastest["model"]
                    save_config(cfg)
                    success(f"Config updated to {fastest['model']}")
    pause()
