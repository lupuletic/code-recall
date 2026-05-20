"""MCP server exposing code-recall session search to coding agents.

Runs over stdio so an agent (Claude Code, Codex, etc.) can call fast,
ranked session retrieval instead of blindly grepping transcript files.

Add to Claude Code:
    claude mcp add code-recall -- code-recall mcp
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Pydantic (used by FastMCP for schema generation) requires
# typing_extensions.TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from code_recall.db import DB_PATH, get_connection
from code_recall.models import SearchResult
from code_recall.searcher import search
from code_recall.utils import CODEX_DIR, PROJECTS_DIR, clean_display_text


class SessionHit(TypedDict):
    """A ranked session match returned by search_sessions."""

    session_id: str
    title: str
    project: str
    provider: str
    branch: str | None
    modified: str | None
    message_count: int
    score: float
    why: str
    snippet: str
    resume_command: str


class SessionDetail(TypedDict):
    """Full detail for a single session returned by get_session_detail."""

    session_id: str
    title: str
    project: str
    project_path: str
    provider: str
    branch: str | None
    model: str | None
    created: str | None
    modified: str | None
    message_count: int
    first_prompt: str | None
    last_prompt: str | None
    files_modified: list[str]
    commands_run: list[str]
    resume_command: str
    transcript_path: str


def _why_matched(result: SearchResult) -> str:
    """A short, agent-readable reason this session matched."""
    snippets = [clean_display_text(s) for s in result.snippets if clean_display_text(s)]
    if result.fts_rank is not None and result.vec_score is not None:
        return "hybrid keyword + semantic match"
    if result.fts_rank is not None:
        return "keyword match"
    if result.vec_score is not None:
        return "semantic match"
    if snippets:
        return f"matched text: {snippets[0][:120]}"
    return "relevance match"


def _title(result: SearchResult) -> str:
    s = result.session
    return (
        clean_display_text(s.summary)
        or clean_display_text(s.first_prompt)
        or "(untitled session)"
    )


def _to_hit(result: SearchResult) -> SessionHit:
    s = result.session
    snippets = [clean_display_text(x) for x in result.snippets if clean_display_text(x)]
    return SessionHit(
        session_id=s.session_id,
        title=_title(result),
        project=result.display_project,
        provider=s.provider,
        branch=s.git_branch or s.git_branch_detected,
        modified=s.modified,
        message_count=s.message_count,
        score=round(result.score, 4),
        why=_why_matched(result),
        snippet=(snippets[0][:280] if snippets else ""),
        resume_command=result.resume_command,
    )


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def build_server(
    db_path: Path = DB_PATH,
    projects_dir: Path = PROJECTS_DIR,
    codex_dir: Path | None = CODEX_DIR,
):
    """Construct the FastMCP server. Imported lazily so the rest of the
    package works without the optional `mcp` dependency installed.

    Paths are captured in the tool closures so the server honors the same
    --db / --claude-dir / --codex-dir flags as the rest of the CLI."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("code-recall")

    @mcp.tool()
    def search_sessions(
        query: str,
        limit: int = 10,
        provider: str | None = None,
        project: str | None = None,
    ) -> list[SessionHit]:
        """Search past Claude Code and Codex sessions by intent.

        Use this instead of grepping transcript files: it runs a fast,
        ranked hybrid search (keyword + semantic + knowledge graph) over
        the local index and returns the most relevant sessions with a
        ready-to-run resume command.

        Args:
            query: Natural-language description of the session you want.
                Supports structured prefixes: 'file:path', 'cmd:name',
                'branch:name'.
            limit: Max sessions to return (default 10).
            provider: Filter to 'claude' or 'codex'. None = both.
            project: Substring filter on the project path.
        """
        _ensure_fresh_index(db_path, projects_dir, codex_dir)
        results = search(query, db_path=db_path, limit=limit, project_filter=project)
        if provider:
            results = [r for r in results if r.session.provider == provider]
        return [_to_hit(r) for r in results[:limit]]

    @mcp.tool()
    def get_session_detail(session_id: str) -> SessionDetail | None:
        """Get full detail for one session by its id.

        Returns the files it touched, commands it ran, branch, model,
        first/last prompts, and the resume command. Use after
        search_sessions to inspect a specific candidate.
        """
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None

        from code_recall.models import Session

        cols = row.keys()
        s = Session(**{k: row[k] for k in cols if k in Session.__dataclass_fields__})
        result = SearchResult(session=s)
        return SessionDetail(
            session_id=s.session_id,
            title=_title(result),
            project=result.display_project,
            project_path=s.project_path,
            provider=s.provider,
            branch=s.git_branch or s.git_branch_detected,
            model=s.model,
            created=s.created,
            modified=s.modified,
            message_count=s.message_count,
            first_prompt=clean_display_text(s.first_prompt),
            last_prompt=clean_display_text(s.last_prompt),
            files_modified=_json_list(s.files_modified),
            commands_run=_json_list(s.commands_run),
            resume_command=result.resume_command,
            transcript_path=s.file_path,
        )

    return mcp


def _ensure_fresh_index(
    db_path: Path = DB_PATH,
    projects_dir: Path = PROJECTS_DIR,
    codex_dir: Path | None = CODEX_DIR,
) -> None:
    """Quick incremental index before serving a query. Never raises —
    a locked or missing index just means we search what's there."""
    try:
        from code_recall.indexer import ensure_index

        ensure_index(
            projects_dir=projects_dir,
            db_path=db_path,
            codex_dir=codex_dir if (codex_dir and codex_dir.exists()) else None,
            verbose=False,
        )
    except Exception:
        pass


def run(
    db_path: Path = DB_PATH,
    projects_dir: Path = PROJECTS_DIR,
    codex_dir: Path | None = CODEX_DIR,
) -> None:
    """Entry point for `code-recall mcp`. Serves over stdio.

    Note: stdout is reserved for the JSON-RPC protocol — all diagnostics
    must go to stderr.
    """
    try:
        server = build_server(db_path=db_path, projects_dir=projects_dir, codex_dir=codex_dir)
    except ImportError:
        print(
            "The 'mcp' package is required. Install with: pip install 'code-recall[mcp]'",
            file=sys.stderr,
        )
        raise SystemExit(1)
    server.run(transport="stdio")
