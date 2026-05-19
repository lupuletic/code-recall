"""Search recall evaluation helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_recall.db import DB_PATH, get_connection
from code_recall.searcher import search


@dataclass
class EvalCase:
    """A single search recall expectation."""

    query: str
    expected_session_id: str | None = None
    expected_project: str | None = None
    expected_terms: list[str] | None = None
    top_k: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalCase":
        return cls(
            query=str(data["query"]),
            expected_session_id=data.get("expected_session_id"),
            expected_project=data.get("expected_project"),
            expected_terms=list(data.get("expected_terms") or []),
            top_k=int(data.get("top_k") or 3),
        )


def load_eval_cases(path: Path) -> list[EvalCase]:
    """Load eval cases from a JSON file."""
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError("Eval file must contain a list or a {'cases': [...]} object")
    return [EvalCase.from_dict(item) for item in data]


def run_eval_cases(
    cases: list[EvalCase],
    db_path: Path = DB_PATH,
    limit: int | None = None,
    semantic: bool | None = None,
) -> dict[str, Any]:
    """Run top-k recall checks and return a structured report."""
    results = []
    passed = 0

    for case in cases:
        max_results = max(limit or case.top_k, case.top_k)
        hits = search(case.query, db_path=db_path, limit=max_results, semantic=semantic)
        top = hits[:case.top_k]
        ok = _case_passed(case, top)
        passed += int(ok)
        results.append({
            "query": case.query,
            "ok": ok,
            "top_k": case.top_k,
            "expected_session_id": case.expected_session_id,
            "expected_project": case.expected_project,
            "expected_terms": case.expected_terms or [],
            "results": [
                {
                    "rank": i,
                    "session_id": result.session.session_id,
                    "project_path": result.session.project_path,
                    "summary": result.session.summary,
                    "score": round(result.score, 4),
                    "snippets": result.snippets[:2],
                }
                for i, result in enumerate(top, 1)
            ],
        })

    total = len(cases)
    return {
        "passed": passed,
        "total": total,
        "accuracy": passed / total if total else 0.0,
        "cases": results,
    }


def generate_eval_cases(
    db_path: Path = DB_PATH,
    limit: int = 30,
    min_messages: int = 3,
) -> list[dict[str, Any]]:
    """Generate exact-session recall cases from the local index.

    The generated cases are intentionally tied to session IDs so they catch
    regressions where a plausible but wrong session happens to share keywords.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT session_id, project_path, summary, first_prompt, last_prompt,
                      message_count, last_activity, modified
               FROM sessions
               WHERE is_subagent = 0
                 AND message_count >= ?
                 AND COALESCE(summary, first_prompt, '') != ''
               ORDER BY message_count DESC, COALESCE(last_activity, modified, '') DESC
               LIMIT ?""",
            (min_messages, max(limit * 2, limit)),
        ).fetchall()

        cases: list[dict[str, Any]] = []
        for row in rows:
            if len(cases) >= limit:
                break
            cases.extend(_cases_for_session(conn, row))
            cases = _dedupe_cases(cases)[:limit]
        return cases
    finally:
        conn.close()


def write_eval_cases(cases: list[dict[str, Any]], path: Path) -> None:
    """Write generated eval cases to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cases": cases}, indent=2) + "\n")


def _cases_for_session(conn: sqlite3.Connection, row: sqlite3.Row) -> list[dict[str, Any]]:
    session_id = row["session_id"]
    title = _clean_query_text(row["summary"] or row["first_prompt"] or "")
    project_name = Path(row["project_path"] or "").name
    cases = []

    if title:
        cases.append(_case(
            query=f"where did we work on {title}",
            session_id=session_id,
            project=row["project_path"],
            kind="topic",
        ))

    for file_row in conn.execute(
        """SELECT file_path, action
           FROM session_files
           WHERE session_id = ?
           ORDER BY CASE action WHEN 'edit' THEN 0 ELSE 1 END, LENGTH(file_path)
           LIMIT 2""",
        (session_id,),
    ).fetchall():
        file_name = Path(file_row["file_path"]).name or file_row["file_path"]
        verb = "edited" if file_row["action"] == "edit" else "inspected"
        topic_suffix = f" while working on {title}" if title else ""
        cases.append(_case(
            query=f"session where we {verb} {file_name} in {project_name}{topic_suffix}",
            session_id=session_id,
            project=row["project_path"],
            kind=f"file:{file_row['action']}",
        ))

    for cmd_row in conn.execute(
        """SELECT command_name, command
           FROM session_commands
           WHERE session_id = ?
           ORDER BY LENGTH(command)
           LIMIT 1""",
        (session_id,),
    ).fetchall():
        cmd_name = cmd_row["command_name"] or cmd_row["command"]
        if cmd_name:
            topic_suffix = f" while working on {title}" if title else ""
            cases.append(_case(
                query=f"where did we run {cmd_name} for {project_name}{topic_suffix}",
                session_id=session_id,
                project=row["project_path"],
                kind="command",
            ))

    return cases


def _case(query: str, session_id: str, project: str | None, kind: str) -> dict[str, Any]:
    return {
        "query": query,
        "expected_session_id": session_id,
        "expected_project": project,
        "top_k": 3,
        "kind": kind,
    }


def _clean_query_text(text: str) -> str:
    text = " ".join(str(text).replace("\n", " ").split())
    text = text.strip(" .,:;")
    if len(text) > 90:
        text = text[:90].rsplit(" ", 1)[0]
    return text


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for case in cases:
        key = (case["query"].lower(), case["expected_session_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)
    return unique


def _case_passed(case: EvalCase, results) -> bool:
    """Check whether top-k results satisfy a case."""
    if not results:
        return False

    if case.expected_session_id:
        return any(result.session.session_id == case.expected_session_id for result in results)

    for result in results:
        session = result.session
        text = " ".join(filter(None, [
            session.summary,
            session.first_prompt,
            session.last_prompt,
            session.messages_text,
            " ".join(result.snippets),
        ])).lower()

        if case.expected_project and case.expected_project.lower() in session.project_path.lower():
            return True
        if case.expected_terms and all(term.lower() in text for term in case.expected_terms):
            return True

    return False
