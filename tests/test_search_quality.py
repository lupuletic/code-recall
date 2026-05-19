"""Optional search quality benchmark for a contributor's own local index.

Run with:

    CODE_RECALL_RUN_QUALITY_TESTS=1 \
    CODE_RECALL_QUALITY_CASES=~/.code-recall/eval-cases.json \
    uv run pytest tests/test_search_quality.py -v --tb=short

Generate a private eval file with:

    code-recall eval generate ~/.code-recall/eval-cases.json 30

The eval file should not be committed; it may contain local project names,
session IDs, and transcript-derived summaries.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_recall.db import DB_PATH
from code_recall.eval import load_eval_cases, run_eval_cases


pytestmark = pytest.mark.skipif(
    os.environ.get("CODE_RECALL_RUN_QUALITY_TESTS") != "1" or not DB_PATH.exists(),
    reason=(
        "Search quality benchmark requires a representative local index and "
        "CODE_RECALL_RUN_QUALITY_TESTS=1"
    ),
)


def _quality_cases_path() -> Path:
    configured = os.environ.get("CODE_RECALL_QUALITY_CASES")
    if not configured:
        pytest.skip("Set CODE_RECALL_QUALITY_CASES to a private eval JSON file")
    path = Path(configured).expanduser()
    if not path.exists():
        pytest.skip(f"Quality eval file does not exist: {path}")
    return path


def test_local_search_quality_cases_pass():
    cases = load_eval_cases(_quality_cases_path())
    report = run_eval_cases(cases, db_path=DB_PATH)

    failed = [case for case in report["cases"] if not case["ok"]]
    failure_summary = "\n".join(
        f"- {case['query']} expected session={case['expected_session_id']} "
        f"project={case['expected_project']}"
        for case in failed[:10]
    )
    assert not failed, (
        f"{report['passed']}/{report['total']} search quality cases passed.\n"
        f"{failure_summary}"
    )
