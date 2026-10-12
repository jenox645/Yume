"""GPU detection against faked nvidia-smi / rocm-smi / rocminfo / Windows CIM output."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.hardware as hw  # noqa: E402
import yume.utils  # noqa: E402

NONE = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}


def detect(tools: dict, cim=(), win=False):
    """`tools` maps a command's first word (or "rocm-smi-vram") to its stdout,
    or to an exception it raises; missing tools exit with 127."""

    def run(cmd, **kw):
        key = "rocm-smi-vram" if cmd[0] == "rocm-smi" and "--csv" not in cmd else cmd[0]
        out = tools.get(key)
        if isinstance(out, Exception):
            raise out
        return types.SimpleNamespace(returncode=0 if out is not None else 127, stdout=out or "", stderr="")

    with (
        patch.object(yume.utils, "_run", run),
        patch.object(hw, "_win_cim", lambda cls, props: list(cim)),
        patch.object(hw, "IS_WIN", win),
    ):
        return hw._detect_gpu_uncached()


def test_nvidia_picks_the_card_with_the_most_vram():
    out = "NVIDIA GeForce GTX 1050, 2048\nNVIDIA GeForce RTX 3060, 12288\n"
    assert detect({"nvidia-smi": out}) == {**NONE, "has_nvidia": True, "vendor": "nvidia",
                                           "name": "NVIDIA GeForce RTX 3060", "vram_mb": 12288}  # fmt: skip


def test_unparsable_nvidia_output_falls_through():
    assert detect({"nvidia-smi": "No devices were found\n"}) == NONE


def test_rocm_smi_name_and_vram_in_bytes():
    smi = "device,Card series\ncard0,Radeon RX 6800\n"
    vram = "GPU[0] : VRAM Total Memory (B): 17163091968\nGPU[0] : VRAM Total Used Memory (B): 1\n"
    gpu = detect({"rocm-smi": smi, "rocm-smi-vram": vram})
    assert gpu == {**NONE, "has_amd": True, "vendor": "amd", "name": "Radeon RX 6800", "vram_mb": 16368}


def test_rocm_smi_without_a_name_asks_rocminfo_and_keeps_the_vram():
    smi = "device\ncard0\n"
    vram = "GPU[0] : VRAM Total Memory (MB): 8192\n"
    info = "  Name: gfx1030\n  Marketing Name: AMD Radeon RX 6600\n"
    gpu = detect({"rocm-smi": smi, "rocm-smi-vram": vram, "rocminfo": info})
    assert (gpu["has_amd"], gpu["name"], gpu["vram_mb"]) == (True, "AMD Radeon RX 6600", 8192)


def test_rocminfo_alone():
    gpu = detect({"rocminfo": "  Name: gfx1100\n  Marketing Name: Radeon RX 7900 XTX\n"})
    assert (gpu["has_amd"], gpu["name"], gpu["vram_mb"]) == (True, "Radeon RX 7900 XTX", 0)


def test_rocminfo_without_a_marketing_name():
    assert detect({"rocminfo": "  Name: gfx90a\n"})["name"] == "AMD GPU (ROCm)"


def test_rocminfo_without_a_gpu_agent_is_no_gpu():
    assert detect({"rocminfo": "  Name: AMD Ryzen 7 5800X\n"}) == NONE


def test_windows_radeon_from_cim():
    gpu = detect({}, cim=["Intel(R) UHD Graphics|1073741824", "AMD Radeon RX 580|4293918720"], win=True)
    assert (gpu["has_amd"], gpu["name"], gpu["vram_mb"]) == (True, "AMD Radeon RX 580", 4095)


def test_cim_is_only_asked_on_windows():
    assert detect({}, cim=["AMD Radeon RX 580|4293918720"], win=False) == NONE


def test_no_gpu_tools_at_all():
    missing = FileNotFoundError("not installed")
    assert detect({"nvidia-smi": missing, "rocm-smi": missing, "rocminfo": missing}) == NONE
