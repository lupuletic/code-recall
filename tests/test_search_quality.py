"""Optional search quality benchmark for a contributor's own local index.

Run with:

    CLAUDE_RECALL_RUN_QUALITY_TESTS=1 \
    CLAUDE_RECALL_QUALITY_CASES=~/.claude-recall/eval-cases.json \
    uv run pytest tests/test_search_quality.py -v --tb=short

The eval file should not be committed; it may contain local project names,
session IDs, and transcript-derived summaries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_code_recall.db import DB_PATH
from claude_code_recall.searcher import search


pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUDE_RECALL_RUN_QUALITY_TESTS") != "1" or not DB_PATH.exists(),
    reason=(
        "Search quality benchmark requires a representative local index and "
        "CLAUDE_RECALL_RUN_QUALITY_TESTS=1"
    ),
)


def _quality_cases_path() -> Path:
    configured = os.environ.get("CLAUDE_RECALL_QUALITY_CASES")
    if not configured:
        pytest.skip("Set CLAUDE_RECALL_QUALITY_CASES to a private eval JSON file")
    path = Path(configured).expanduser()
    if not path.exists():
        pytest.skip(f"Quality eval file does not exist: {path}")
    return path


def test_local_search_quality_cases_pass():
    data = json.loads(_quality_cases_path().read_text())
    cases = data.get("cases", data) if isinstance(data, dict) else data
    failed = []

    for case in cases:
        query = str(case["query"])
        top_k = int(case.get("top_k") or 3)
        expected_session_id = case.get("expected_session_id")
        expected_project = case.get("expected_project")
        results = search(query, db_path=DB_PATH, limit=top_k)
        ok = False
        for result in results:
            if expected_session_id and result.session.session_id == expected_session_id:
                ok = True
            if expected_project and expected_project.lower() in result.session.project_path.lower():
                ok = True
        if not ok:
            failed.append(query)

    assert not failed, f"Failed search quality cases: {failed[:10]}"
