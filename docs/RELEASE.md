# Releasing code-recall

Releases are tag driven. CI verifies the version declarations match before publishing. The release workflow builds distributions, publishes to PyPI through Trusted Publishing, and creates or updates the matching GitHub release.

## Trusted Publisher fields

| Field | Value |
|-------|-------|
| PyPI project name | `code-recall` |
| Owner | `lupuletic` |
| Repository name | `code-recall` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

## Cutting a release

```bash
uv run python scripts/check_version.py
uv run pytest -q
uv run python -m compileall -q src tests
uv build --no-sources

git tag v0.2.2
git push origin main --tags
```
