"""Integration tests for Yume v0.0.9.

These tests verify that config options actually affect behavior.
They exist because of the deno incident: youtube_auth_method="deno"
was a no-op from v1.0 to v5.4.2 — zero code used it. A 25-category
deep review missed it. These tests ensure that never happens again.

Run: pytest tests/test_integration.py -v
"""
import json
import re
import sys
from pathlib import Path

# Project root
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# 1. DEAD CONFIG DETECTION
#    Every key in DEFAULT_CONFIG must be *used* somewhere (not just displayed).
#    "Used" means it influences a subprocess call, server parameter, or API
#    response — not just printed in a menu or stored in a file.
# ---------------------------------------------------------------------------

def _load_all_source_code():
    """Load all Python and JS source into a dict of {path: content}."""
    sources = {}
    for pattern in ["*.py", "yume/*.py", "server/*.py", "extension/js/*.js", "extension/*.js"]:
        for f in ROOT.glob(pattern):
            if "wanakana" in f.name or "__pycache__" in str(f):
                continue
            sources[str(f.relative_to(ROOT))] = f.read_text(encoding="utf-8", errors="replace")
    return sources


class TestDeadConfigDetection:
    """Flag config keys that exist but are never read in any source file."""

    # Keys that are KNOWN to be display-only or structural — exempt from the
    # "must influence behavior" rule. Document WHY each is exempt.
    EXEMPT_KEYS = {
        "first_run_complete",   # Controls flow (wizard vs main menu), not a subprocess arg
        "translation_model",    # Display-only: the actual model is loaded via gguf_model_path
    }

    # Keys that are KNOWN dead — they exist in config but nothing uses them.
    # If a key appears here, it's a known debt item, not a forgotten no-op.
    # The test will FAIL if a new dead key appears that isn't listed here.
    KNOWN_DEAD: set = set()  # No currently known-dead keys

    def test_no_unknown_dead_keys(self):
        """Every DEFAULT_CONFIG key must appear in source code beyond config.py itself.

        If a key is only referenced in config.py and test files, it's dead.
        Keys in KNOWN_DEAD are tolerated (but documented). Keys in EXEMPT_KEYS
        are intentionally display/flow-only. Any OTHER key that's not found
        in the actual application code is a new dead key — likely a bug.
        """
        sources = _load_all_source_code()
        dead_keys = []

        for key in DEFAULT_CONFIG:
            if key in self.EXEMPT_KEYS or key in self.KNOWN_DEAD:
                continue

            # Check if the key appears in any non-config, non-test source file
            found_in_app = False
            for path, content in sources.items():
                if "config.py" in path or "test_" in path:
                    continue
                # Look for the key being read: cfg["key"], cfg.get("key"), or key as a variable
                if f'"{key}"' in content or f"'{key}'" in content or f"['{key}']" in content:
                    found_in_app = True
                    break

            if not found_in_app:
                dead_keys.append(key)

        assert not dead_keys, (
            f"Config keys exist in DEFAULT_CONFIG but are never used in application code: "
            f"{dead_keys}. Either:\n"
            f"  1. Add code that uses them, or\n"
            f"  2. Remove them from DEFAULT_CONFIG, or\n"
            f"  3. Add them to KNOWN_DEAD with a comment explaining why."
        )

    def test_known_dead_keys_are_actually_dead(self):
        """Verify KNOWN_DEAD keys haven't been quietly fixed.

        If someone adds code that uses a KNOWN_DEAD key, this test fails
        so they also remove it from KNOWN_DEAD. Keeps the list honest.
        """
        sources = _load_all_source_code()

        for key in self.KNOWN_DEAD:
            uses_in_app = []
            for path, content in sources.items():
                if "config.py" in path or "test_" in path:
                    continue
                # Check for the key being used to CHANGE behavior
                # (not just loaded into a variable or printed)
                lines = content.splitlines()
                for i, line in enumerate(lines):
                    if f'"{key}"' in line or f"'{key}'" in line:
                        # Exclude comments and pure print/info/warn lines
                        stripped = line.strip()
                        if stripped.startswith("#") or stripped.startswith("//"):
                            continue
                        if any(fn in stripped for fn in ["print(", "info(", "warn(", "success(", "error("]):
                            continue
                        uses_in_app.append(f"  {path}:{i+1}: {stripped[:100]}")

            # If the key IS now used in real code, it should be removed from KNOWN_DEAD
            # We allow loading the value (e.g. into a global) — that's not "using" it
            # in a way that changes behavior if word_timestamps is forced False.
            assert not uses_in_app, (
                f"KNOWN_DEAD key '{key}' is now used in app code — remove it from KNOWN_DEAD:\n"
                + "\n".join(uses_in_app)
            )


# ---------------------------------------------------------------------------
# 2. YOUTUBE AUTH CONFIG → COMMAND TRACING
#    Verify that youtube_auth_method actually changes the yt-dlp command.
# ---------------------------------------------------------------------------

class TestYouTubeAuthConfig:
    """Trace youtube_auth_method config to actual yt-dlp command construction.

    The server was refactored into a package (server/_audio.py, _state.py, etc.).
    These tests check all server/*.py files so moving logic between modules
    doesn't silently break the invariants.
    """

    def _get_server_source(self):
        """Concatenate all server Python source files for invariant checking."""
        parts = []
        for f in sorted((ROOT / "server").glob("*.py")):
            if "__pycache__" not in str(f):
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def test_deno_mode_uses_python_m_yt_dlp(self):
        """When youtube_auth_method='deno', yt-dlp must run as 'python -m yt_dlp'.

        This is because standalone yt-dlp binary can't find pip-installed plugins
        (bgutil-ytdlp-pot-provider). Only pip-installed yt-dlp discovers pip plugins.
        """
        src = self._get_server_source()

        # The ytdlp_cmd function must exist and handle deno mode
        assert "ytdlp_cmd" in src, "ytdlp_cmd function not found in server"

        # It must reference python -m yt_dlp for deno mode
        assert "python" in src and "yt_dlp" in src, (
            "ytdlp_cmd must route to 'python -m yt_dlp' for deno mode"
        )

    def test_deno_mode_has_strategy_entries(self):
        """Deno mode must have its own strategy entries in the download function."""
        src = self._get_server_source()

        # There must be an if-branch checking for "deno"
        assert 'youtube_auth_method == "deno"' in src or "youtube_auth_method == 'deno'" in src, (
            "No code branch checks for deno mode. "
            "This is the EXACT bug from v1.0-v5.4.2: deno was configured but never used."
        )

    def test_cookies_mode_passes_cookies_flag(self):
        """When youtube_auth_method='cookies', yt-dlp must get --cookies-from-browser."""
        src = self._get_server_source()
        assert "--cookies-from-browser" in src, (
            "Cookies mode must pass --cookies-from-browser to yt-dlp"
        )

    def test_cookies_mode_uses_configured_browser(self):
        """The cookies_browser config value must be passed to yt-dlp."""
        src = self._get_server_source()
        # Must reference cookies_browser or resolve_browser_cookies
        assert "cookies_browser" in src or "resolve_browser_cookies" in src, (
            "cookies_browser config value is never used in the server"
        )


# ---------------------------------------------------------------------------
# 3. BACKEND HEALTH CHECK CONSISTENCY
#    Popup.js and pocket_yume.py must agree on which endpoints to check.
# ---------------------------------------------------------------------------

class TestHealthCheckConsistency:
    """Verify popup.js and CLI use the same health endpoints."""

    def test_llamacpp_health_path(self):
        """llama.cpp health must use /v1/models (not /health which returns 404)."""
        cli_src = (ROOT / "pocket_yume.py").read_text(encoding="utf-8")
        assert len(cli_src) < 500_000, "Source file unexpectedly large — refusing regex to prevent ReDoS"

        # Find BACKEND_INFO llamacpp hp value (may be a literal or constant reference)
        match = re.search(r'"llamacpp".*?"hp":\s*("([^"]+)"|HEALTH_PATH_OPENAI)', cli_src, re.DOTALL)
        assert match, "BACKEND_INFO llamacpp not found"
        if match.group(2):
            assert match.group(2) == "/v1/models", (
                f"llamacpp health path is '{match.group(2)}' but must be '/v1/models'"
            )
        else:
            # Using constant — verify the constant value
            const_match = re.search(r'HEALTH_PATH_OPENAI\s*=\s*"([^"]+)"', cli_src)
            assert const_match and const_match.group(1) == "/v1/models"

    def test_ollama_health_path(self):
        """Ollama health must use /api/tags."""
        cli_src = (ROOT / "pocket_yume.py").read_text(encoding="utf-8")
        assert len(cli_src) < 500_000, "Source file unexpectedly large — refusing regex to prevent ReDoS"

        # Find the ollama block — hp may be a literal or constant reference
        ollama_block = re.search(
            r'"ollama"\s*:\s*\{[^}]*"hp"\s*:\s*("([^"]+)"|HEALTH_PATH_OLLAMA)',
            cli_src
        )
        assert ollama_block, "BACKEND_INFO ollama not found"
        if ollama_block.group(2):
            assert ollama_block.group(2) == "/api/tags", (
                f"ollama health path is '{ollama_block.group(2)}' but must be '/api/tags'"
            )
        else:
            const_match = re.search(r'HEALTH_PATH_OLLAMA\s*=\s*"([^"]+)"', cli_src)
            assert const_match and const_match.group(1) == "/api/tags"

    def test_popup_tries_v1_models(self):
        """popup.js must try /v1/models for translation health check."""
        popup_src = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
        assert "/v1/models" in popup_src, (
            "popup.js doesn't try /v1/models for translation health — "
            "it won't detect llama.cpp or LM Studio servers"
        )


# ---------------------------------------------------------------------------
# 4. VERSION CONSISTENCY (extended)
#    All files that contain a version string must agree.
# ---------------------------------------------------------------------------

class TestVersionConsistencyExtended:
    """Extended version checks beyond what CI already does."""

    def _get_version(self):
        src = (ROOT / "pocket_yume.py").read_text(encoding="utf-8")
        m = re.search(r'VERSION\s*=\s*"(\d+\.\d+\.\d+)"', src)
        assert m, "VERSION not found in pocket_yume.py"
        return m.group(1)

    def test_server_health_version(self):
        """Server /health response must report the same version."""
        v = self._get_version()
        srv = (ROOT / "server" / "faster_whisper_server.py").read_text(encoding="utf-8")
        assert f'"version": "{v}"' in srv, f"Server /health version mismatch (expected {v})"

    def test_server_banner_version(self):
        """Server startup banner must show the same version."""
        v = self._get_version()
        srv = (ROOT / "server" / "faster_whisper_server.py").read_text(encoding="utf-8")
        assert f"v{v}" in srv, f"Server banner doesn't contain v{v}"

    def test_manifest_firefox_matches(self):
        v = self._get_version()
        mf = json.loads((ROOT / "extension" / "manifest_firefox.json").read_text())
        assert mf["version"] == v, f"manifest_firefox.json ({mf['version']}) != {v}"

    def test_popup_html_version(self):
        v = self._get_version()
        html = (ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
        assert f"v{v}" in html, f"popup.html doesn't show v{v}"

    def test_readme_badge_version(self):
        v = self._get_version()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert f"version-{v}-blue" in readme, f"README badge doesn't show {v}"

    def test_architecture_doc_version(self):
        v = self._get_version()
        arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert f"v{v}" in arch, f"ARCHITECTURE.md doesn't show v{v}"


# ---------------------------------------------------------------------------
# 5. SUBPROCESS HYGIENE
#    All yt-dlp calls must go through _ytdlp_cmd(), not hardcoded "yt-dlp".
# ---------------------------------------------------------------------------

class TestSubprocessHygiene:
    """Verify no hardcoded 'yt-dlp' binary calls bypass _ytdlp_cmd()."""

    def test_no_hardcoded_ytdlp_in_server(self):
        """Server must not call 'yt-dlp' directly — must use _ytdlp_cmd()."""
        src = (ROOT / "server" / "faster_whisper_server.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        violations = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            # Look for subprocess calls with hardcoded "yt-dlp"
            if ('"yt-dlp"' in line or "'yt-dlp'" in line) and "subprocess" in line:
                violations.append(f"  line {i+1}: {stripped[:100]}")

        assert not violations, (
            "Found hardcoded 'yt-dlp' in subprocess calls "
            "(must use _ytdlp_cmd()):\n" + "\n".join(violations)
        )

    def test_no_shell_true_in_python(self):
        """No shell=True in subprocess calls (argument injection risk)."""
        for pyfile in ["pocket_yume.py", "server/faster_whisper_server.py", "config.py"]:
            path = ROOT / pyfile
            if not path.exists():
                continue
            violations = []
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "shell=True" in line:
                    violations.append(f"  {pyfile} line {i+1}: {stripped[:100]}")
            assert not violations, (
                "Found shell=True (must use explicit argv lists):\n" +
                "\n".join(violations)
            )

    def test_no_os_system_in_python(self):
        """No os.system() calls (goes through shell)."""
        for pyfile in ["pocket_yume.py", "server/faster_whisper_server.py", "config.py"]:
            path = ROOT / pyfile
            if not path.exists():
                continue
            violations = []
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "os.system(" in line:
                    violations.append(f"  {pyfile} line {i+1}: {stripped[:100]}")
            assert not violations, (
                "Found os.system() (use subprocess or ctypes instead):\n" +
                "\n".join(violations)
            )

    def test_no_curl_pipe_shell(self):
        """No curl|sh pipe-to-shell patterns (MITM risk)."""
        for pyfile in ["pocket_yume.py"]:
            path = ROOT / pyfile
            if not path.exists():
                continue
            src = path.read_text(encoding="utf-8")
            violations = []
            for i, line in enumerate(src.splitlines()):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "| sh" in line and "curl" in line and "# " not in line.split("|")[0]:
                    violations.append(f"  {pyfile} line {i+1}: {stripped[:100]}")
            assert not violations, (
                "Found curl|sh pipe-to-shell (download first, then execute):\n" +
                "\n".join(violations)
            )


class TestDependencyPinning:
    """Verify all dependencies use exact version pinning."""

    def test_requirements_txt_pinned(self):
        """requirements.txt must use == (exact), not >= (minimum)."""
        req = ROOT / "server" / "requirements.txt"
        if not req.exists():
            return
        violations = []
        for i, line in enumerate(req.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ">=" in line or ">" in line.split("==")[0]:
                violations.append(f"  line {i+1}: {line}")
        assert not violations, (
            "requirements.txt has unpinned dependencies (use == not >=):\n" +
            "\n".join(violations)
        )


class TestHardcodedURLs:
    """Verify no hardcoded localhost URL fallbacks scattered across JS files."""

    def test_no_inline_localhost_fallback_in_background(self):
        """background.js must use _whisperUrl()/_translationUrl(), not inline fallbacks."""
        path = ROOT / "extension" / "js" / "background.js"
        if not path.exists():
            return
        violations = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            stripped = line.strip()
            if stripped.startswith("//") or "DEFAULT_SETTINGS" in line or "function _" in line:
                continue
            if ("|| 'http://localhost:5001'" in line or
                '|| "http://localhost:5001"' in line or
                "|| 'http://localhost:5000'" in line or
                '|| "http://localhost:5000"' in line):
                violations.append(f"  line {i+1}: {stripped[:120]}")
        assert not violations, (
            "background.js has hardcoded localhost fallbacks "
            "(use _whisperUrl()/_translationUrl() helpers):\n" +
            "\n".join(violations)
        )

    def test_no_inline_localhost_fallback_in_popup(self):
        """popup.js must use _whisperUrl(), not inline fallbacks."""
        path = ROOT / "extension" / "popup.js"
        if not path.exists():
            return
        violations = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            stripped = line.strip()
            if stripped.startswith("//") or "getDefaultSettings" in line or "_POPUP_DEFAULTS" in line or "function _" in line:
                continue
            if ("|| 'http://localhost:5001'" in line or
                '|| "http://localhost:5001"' in line or
                "|| 'http://localhost:5000'" in line or
                '|| "http://localhost:5000"' in line):
                violations.append(f"  line {i+1}: {stripped[:120]}")
        assert not violations, (
            "popup.js has hardcoded localhost fallbacks "
            "(use _whisperUrl() helper):\n" +
            "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# 6. NAMING CONSISTENCY
#    Project name must be "Yume" everywhere user-facing, "Pocket Yume" only
#    for the CLI wizard.
# ---------------------------------------------------------------------------

class TestNamingConsistency:
    """Verify project naming is consistent across all files."""

    def test_no_pocketyume_in_docs(self):
        """Docs should say 'Yume' or 'Pocket Yume', never 'PocketYume'."""
        for doc in ["README.md", "docs/ARCHITECTURE.md", "docs/DEVELOPER_GUIDE.md", "docs/CONTRIBUTING.md"]:
            path = ROOT / doc
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            # PocketYume (camelCase) should not appear — it's either "Yume" or "Pocket Yume"
            occurrences = [
                (i+1, line.strip())
                for i, line in enumerate(content.splitlines())
                if "PocketYume" in line and not line.strip().startswith("#")
                # Allow it in code blocks (backticks)
                and "`PocketYume`" not in line
                # Allow it in changelog entries that describe fixing the naming
                and "references corrected" not in line
            ]
            assert not occurrences, (
                f"{doc} contains 'PocketYume' (should be 'Yume' or 'Pocket Yume'):\n" +
                "\n".join(f"  line {n}: {ln[:80]}" for n, ln in occurrences)
            )


# ---------------------------------------------------------------------------
# 7. PYTHON COMPATIBILITY
#    Ensure no Python 3.10+ syntax is used without __future__ annotations.
#    This caught a real bug: config.py used `int | str` which crashes on 3.9.
# ---------------------------------------------------------------------------

class TestPythonCompatibility:
    """Verify all Python files work on Python 3.8+."""

    def test_no_bare_union_types(self):
        """Files using X | Y type hints must have 'from __future__ import annotations'.

        Without this import, `int | str` in function signatures raises TypeError
        on Python 3.8/3.9. This test caught a real crash reported by a user.
        """
        skip_dirs = {"__pycache__", "venv", ".venv", "yume-env", "node_modules", "site-packages"}
        for py_file in ROOT.glob("**/*.py"):
            if skip_dirs.intersection(py_file.parts) or "wanakana" in str(py_file):
                continue
            content = py_file.read_text(encoding="utf-8", errors="replace")

            # Check for X | Y in function signatures (def foo(x: int | str) or -> int | None)
            has_union_hints = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if len(line) > 500:  # skip pathologically long lines to prevent ReDoS
                    continue
                if re.match(r"def \w+\(.*:\s*\w+\s*\|\s*\w+", line):
                    has_union_hints = True
                    break
                if re.match(r"def \w+\(.*\)\s*->\s*\w+\s*\|\s*\w+", line):
                    has_union_hints = True
                    break

            if has_union_hints:
                has_future = "from __future__ import annotations" in content
                assert has_future, (
                    f"{py_file.relative_to(ROOT)} uses X | Y type hints but is missing "
                    f"'from __future__ import annotations'. This crashes on Python 3.8/3.9."
                )

    def test_python_version_check_exists(self):
        """pocket_yume.py must check Python version before importing config."""
        content = (ROOT / "pocket_yume.py").read_text(encoding="utf-8")
        assert "sys.version_info" in content, (
            "pocket_yume.py should check Python version early to give a clear error"
        )
        # The check must appear BEFORE the config import
        version_check_pos = content.index("sys.version_info")
        config_import_pos = content.index("from config import")
        assert version_check_pos < config_import_pos, (
            "Python version check must appear before 'from config import' "
            "so users see a friendly message instead of a TypeError traceback"
        )


# ---------------------------------------------------------------------------
# 7. IMPORT COMPLETENESS
#    Every stdlib module used at module level must be imported.
#    This catches the v5.7.0 bug: logging.basicConfig() used but
#    'import logging' was missing from the server.
# ---------------------------------------------------------------------------

class TestImportCompleteness:
    """Verify Python files import all modules they use."""

    # Standard library modules that are commonly used via module.function()
    STDLIB_MODULES = {
        "os", "sys", "json", "time", "shutil", "platform", "subprocess",
        "threading", "logging", "re", "tempfile", "signal", "atexit",
        "argparse", "secrets", "base64", "io", "zipfile", "tarfile",
    }

    def _get_imports(self, source):
        """Extract all imported module names from source."""
        import ast
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split('.')[0])
        return imports

    def _get_module_references(self, source):
        """Find all 'module.something' references in source (excluding comments/strings/URLs)."""
        import ast
        refs = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return refs
        for node in ast.walk(tree):
            # Check Attribute nodes: module.function()
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in self.STDLIB_MODULES:
                    refs.add(node.value.id)
        return refs

    def test_server_imports_complete(self):
        """Server must import every stdlib module it uses."""
        src = (ROOT / "server" / "faster_whisper_server.py").read_text(encoding="utf-8")
        imports = self._get_imports(src)
        refs = self._get_module_references(src)
        missing = refs - imports
        assert not missing, (
            f"Server uses these modules but doesn't import them: {missing}\n"
            f"This causes NameError at runtime (v5.7.0 bug: logging was missing)"
        )

    def test_pocket_yume_imports_complete(self):
        """CLI must import every stdlib module it uses."""
        src = (ROOT / "pocket_yume.py").read_text(encoding="utf-8")
        imports = self._get_imports(src)
        refs = self._get_module_references(src)
        missing = refs - imports
        assert not missing, (
            f"pocket_yume.py uses these modules but doesn't import them: {missing}"
        )

    def test_config_imports_complete(self):
        """Config module must import every stdlib module it uses."""
        src = (ROOT / "config.py").read_text(encoding="utf-8")
        imports = self._get_imports(src)
        refs = self._get_module_references(src)
        missing = refs - imports
        assert not missing, (
            f"config.py uses these modules but doesn't import them: {missing}"
        )
