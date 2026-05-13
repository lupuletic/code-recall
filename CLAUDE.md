# Repository Instructions

## Versioning

Any user-visible behavior change, CLI/TUI change, search/indexing behavior change, packaging change, or bug fix that should be distinguishable in an installed copy must bump the package version before finishing.

When bumping the version:

1. Update `pyproject.toml`.
2. Update `src/claude_recall/__init__.py`.
3. Run `uv lock` so `uv.lock` records the same package version.
4. Verify with `uv run claude-recall --version`.
5. If the user is using the global tool install, reinstall with:
   `uv tool install --force /Users/lupuletic/TheHutGroup/claude-recall --with textual --with fastembed --with sqlite-vec`
6. Verify the installed command with `claude-recall --version`.

The TUI displays the runtime version in its title/status area, so a restarted TUI should show the new version after reinstalling.

## Validation

Before handing back changes, run:

```bash
uv run pytest -q
uv run python -m compileall -q src tests
git diff --check
```
