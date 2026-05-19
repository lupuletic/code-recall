# Repository Instructions

## Versioning

Any user-visible behavior change, CLI/TUI change, search/indexing behavior change, packaging change, or bug fix that should be distinguishable in an installed copy must bump the package version before finishing.

When bumping the version:

1. Update `pyproject.toml`.
2. Update `src/code_recall/__init__.py`.
3. Run `uv lock` so `uv.lock` records the same package version.
4. Verify consistency with `uv run python scripts/check_version.py`.
5. Verify the local command with `uv run code-recall --version`.
6. If the user is using the global tool install, reinstall with:
   `uv tool install --force "$PWD" --with textual --with fastembed --with sqlite-vec`
7. Verify the installed command with `code-recall --version`.

The TUI displays the runtime version in its title/status area, so a restarted TUI should show the new version after reinstalling.

## Release Process

Releases are tag driven through `.github/workflows/release.yml`.

Before tagging:

```bash
uv run python scripts/check_version.py
uv run pytest -q
uv run python -m compileall -q src tests
uv build --no-sources
```

Only push a `vX.Y.Z` tag when `pyproject.toml`, `src/code_recall/__init__.py`, and the tag version all match. The GitHub release workflow publishes the `code-recall` distribution to PyPI through Trusted Publishing using the `pypi` environment.

## Validation

Before handing back changes, run:

```bash
uv run python scripts/check_version.py
uv run pytest -q
uv run python -m compileall -q src tests
git diff --check
```
