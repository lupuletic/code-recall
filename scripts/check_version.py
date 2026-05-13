#!/usr/bin/env python3
"""Verify that package version declarations agree."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "claude_code_recall" / "__init__.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Optional release tag, e.g. v0.1.4")
    args = parser.parse_args(argv)

    versions = {
        "pyproject.toml": _read_pyproject_version(PYPROJECT),
        "src/claude_code_recall/__init__.py": _read_init_version(INIT),
    }
    if args.tag:
        versions["tag"] = args.tag.removeprefix("v")

    unique = set(versions.values())
    if len(unique) == 1:
        version = next(iter(unique))
        print(f"Version OK: {version}")
        return 0

    print("Version mismatch:", file=sys.stderr)
    for source, version in versions.items():
        print(f"  {source}: {version}", file=sys.stderr)
    return 1


def _read_pyproject_version(path: Path) -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        text = path.read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
        raise RuntimeError(f"Could not find project.version in {path}")

    data = tomllib.loads(path.read_text())
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"Could not find project.version in {path}")
    return version


def _read_init_version(path: Path) -> str:
    text = path.read_text()
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not find __version__ in {path}")
    return match.group(1)


if __name__ == "__main__":
    raise SystemExit(main())
