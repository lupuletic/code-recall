"""Auto-update check for claude-recall."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from claude_recall import __version__

UPDATE_CHECK_FILE = Path.home() / ".claude-recall" / ".last-update-check"
CHECK_INTERVAL = 86400  # 24 hours
PYPI_JSON_URL = "https://pypi.org/pypi/claude-recall/json"


def get_latest_version(timeout: float = 2) -> str | None:
    """Return the latest PyPI version, or None if it cannot be fetched."""
    try:
        with urlopen(PYPI_JSON_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
        latest = data.get("info", {}).get("version")
        return latest if isinstance(latest, str) and latest else None
    except Exception:
        return None


def check_for_update(quiet: bool = False) -> None:
    """Check PyPI for a newer version. Runs at most once per day."""
    if quiet:
        return

    from claude_recall.config import load_config

    if not load_config().get("update_check", True):
        return

    # Skip if checked recently
    if UPDATE_CHECK_FILE.exists():
        try:
            last_check = float(UPDATE_CHECK_FILE.read_text().strip())
            if time.time() - last_check < CHECK_INTERVAL:
                return
        except (ValueError, OSError):
            pass

    # Record this check
    try:
        UPDATE_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CHECK_FILE.write_text(str(time.time()))
    except OSError:
        return

    latest = get_latest_version(timeout=2)
    if latest and latest != __version__ and _is_newer(latest, __version__):
        command = format_command(detect_update_command().command)
        print(
            f"\n  Update available: {__version__} -> {latest}"
            f"\n  Run: {command}\n",
            file=sys.stderr,
        )


class UpdateCommand:
    """Detected command for upgrading the current installation."""

    def __init__(self, command: list[str], method: str) -> None:
        self.command = command
        self.method = method


def detect_update_command() -> UpdateCommand:
    """Return the best upgrade command for the user's install method."""
    if _uv_tool_installed():
        return UpdateCommand(["uv", "tool", "upgrade", "claude-recall"], "uv tool")

    if _pipx_installed():
        return UpdateCommand(["pipx", "upgrade", "claude-recall"], "pipx")

    return UpdateCommand(
        [sys.executable, "-m", "pip", "install", "--upgrade", "claude-recall[all]"],
        "pip",
    )


def format_command(command: list[str]) -> str:
    """Format a command list for copy-pasteable shell output."""
    return " ".join(shlex.quote(part) for part in command)


def run_update(yes: bool = False, quiet: bool = False) -> int:
    """Check for and optionally install the latest release."""
    latest = get_latest_version(timeout=10)
    if not latest:
        print("Could not check PyPI for the latest claude-recall version.", file=sys.stderr)
        return 1

    if not _is_newer(latest, __version__):
        if not quiet:
            print(f"claude-recall is already up to date ({__version__}).")
        return 0

    update_command = detect_update_command()
    command_text = format_command(update_command.command)
    if not quiet:
        print(f"Current version: {__version__}")
        print(f"Latest version:  {latest}")
        print(f"Install method:  {update_command.method}")
        print(f"Command:         {command_text}")

    if not yes:
        if not quiet:
            print("Run with --yes to execute the update.")
        return 0

    return subprocess.run(update_command.command).returncode


def _uv_tool_installed() -> bool:
    if not shutil.which("uv"):
        return False
    try:
        result = subprocess.run(
            ["uv", "tool", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and "claude-recall" in result.stdout


def _pipx_installed() -> bool:
    if not shutil.which("pipx"):
        return False

    try:
        result = subprocess.run(
            ["pipx", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            packages = data.get("venvs", {})
            if isinstance(packages, dict) and "claude-recall" in packages:
                return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and "claude-recall" in result.stdout


def _is_newer(latest: str, current: str) -> bool:
    """Compare simple dotted version strings."""
    try:
        lat = _version_tuple(latest)
        cur = _version_tuple(current)
        return lat > cur
    except ValueError:
        return False


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        raise ValueError(version)
    values = tuple(int(part) for part in parts)
    return values + (0,) * (3 - len(values))
