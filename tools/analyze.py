#!/usr/bin/env python3
"""Yume — local code analysis runner.

Usage:
    python tools/analyze.py all          # run every check, print summary
    python tools/analyze.py complexity   # cyclomatic complexity (radon)
    python tools/analyze.py deadcode     # unused code detection (vulture)
    python tools/analyze.py security     # security scan (bandit)
    python tools/analyze.py callgraph    # function call graph (pyan3 + graphviz)
    python tools/analyze.py coverage     # test coverage (pytest-cov)
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PY_TARGETS = ["pocket_yume.py", "config.py", "server/faster_whisper_server.py"]
JS_DIR = "extension/js"


def _run(cmd, *, cwd=None, check=False, capture=False):
    """Run a shell command, return CompletedProcess."""
    kwargs = dict(cwd=cwd or ROOT, text=True)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.run(cmd, check=check, **kwargs)


def _ensure_tool(name, pip_name=None):
    """Check if a CLI tool is available; print install hint if missing."""
    if shutil.which(name):
        return True
    pkg = pip_name or name
    print(f"  [SKIP] '{name}' not found. Install with: pip install {pkg}")
    return False


def _header(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


# ── Checks ──────────────────────────────────────────────────────────────────

def run_complexity():
    _header("Cyclomatic Complexity (radon)")
    if not _ensure_tool("radon"):
        return "skip"
    print("── Cyclomatic Complexity (grade C and above) ──")
    r1 = _run(["radon", "cc"] + PY_TARGETS + ["-a", "-nc"], capture=True)
    print(r1.stdout if r1.stdout.strip() else "  All functions grade A-B")
    print("\n── Maintainability Index ──")
    r2 = _run(["radon", "mi"] + PY_TARGETS + ["-nc"], capture=True)
    print(r2.stdout if r2.stdout.strip() else "  All files maintainable")
    return "warn" if r1.stdout.strip() else "pass"


def run_deadcode():
    _header("Dead Code Detection (vulture)")
    if not _ensure_tool("vulture"):
        return "skip"
    whitelist = ROOT / "vulture_whitelist.py"
    cmd = ["vulture"] + PY_TARGETS + ["--min-confidence", "80"]
    if whitelist.exists():
        cmd.append(str(whitelist))
    r = _run(cmd, capture=True)
    print(r.stdout if r.stdout.strip() else "  No dead code found")
    return "warn" if r.returncode != 0 else "pass"


def run_security():
    _header("Security Scan (bandit)")
    if not _ensure_tool("bandit"):
        return "skip"
    pyproject = ROOT / "pyproject.toml"
    cmd = ["bandit", "-r"] + PY_TARGETS + ["-ll"]
    if pyproject.exists():
        cmd += ["-c", str(pyproject)]
    r = _run(cmd, capture=True)
    print(r.stdout)
    has_issues = r.returncode not in (0,)
    # Also run pip-audit if available
    if shutil.which("pip-audit"):
        req = ROOT / "server" / "requirements.txt"
        if req.exists():
            print("── Dependency Audit (pip-audit) ──")
            r2 = _run(["pip-audit", "-r", str(req)], capture=True)
            print(r2.stdout)
            if r2.returncode != 0:
                has_issues = True
    else:
        print("  [SKIP] pip-audit not found. Install with: pip install pip-audit")
    return "fail" if has_issues else "pass"


def run_callgraph():
    _header("Call Graph (pyan3 + graphviz)")
    if not _ensure_tool("pyan3"):
        return "skip"
    has_dot = shutil.which("dot")
    if not has_dot:
        print("  [WARN] graphviz 'dot' not found — will generate .dot files only")
        print("         Install from: https://graphviz.org/download/")
    REPORTS.mkdir(exist_ok=True)
    targets_map = {
        "pocket_yume": [str(ROOT / "pocket_yume.py")],
        "server": [str(ROOT / "server" / "faster_whisper_server.py")],
    }
    for name, files in targets_map.items():
        dot_file = REPORTS / f"callgraph_{name}.dot"
        svg_file = REPORTS / f"callgraph_{name}.svg"
        r = _run(
            ["pyan3"] + files + ["--dot", "--no-defines", "--grouped"],
            capture=True,
        )
        if r.returncode != 0:
            print(f"  [WARN] pyan3 failed for {name}: {r.stdout[:200]}")
            continue
        dot_file.write_text(r.stdout, encoding="utf-8")
        print(f"  Generated {dot_file.relative_to(ROOT)}")
        if has_dot:
            _run(["dot", "-Tsvg", str(dot_file), "-o", str(svg_file)])
            print(f"  Generated {svg_file.relative_to(ROOT)}")
    return "pass"


def run_coverage():
    _header("Test Coverage (pytest-cov)")
    if not _ensure_tool("pytest", "pytest"):
        return "skip"
    REPORTS.mkdir(exist_ok=True)
    r = _run(
        [
            sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short",
            "--cov=.", "--cov-report=term-missing",
            f"--cov-report=html:{REPORTS / 'htmlcov'}",
        ],
        capture=True,
    )
    print(r.stdout)
    return "pass" if r.returncode == 0 else "fail"


# ── Summary ─────────────────────────────────────────────────────────────────

CHECKS = {
    "complexity": run_complexity,
    "deadcode": run_deadcode,
    "security": run_security,
    "callgraph": run_callgraph,
    "coverage": run_coverage,
}

STATUS_ICON = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}


def run_all():
    results = {}
    for name, fn in CHECKS.items():
        results[name] = fn()
    _header("Summary")
    for name, status in results.items():
        icon = STATUS_ICON.get(status, "?")
        print(f"  {icon}  {name:15s} {status}")
    print()
    failures = [n for n, s in results.items() if s == "fail"]
    if failures:
        print(f"  {len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Yume code analysis runner")
    parser.add_argument(
        "command",
        choices=["all", *CHECKS.keys()],
        help="Analysis to run",
    )
    args = parser.parse_args()

    if args.command == "all":
        sys.exit(run_all())
    else:
        result = CHECKS[args.command]()
        sys.exit(1 if result == "fail" else 0)


if __name__ == "__main__":
    main()
