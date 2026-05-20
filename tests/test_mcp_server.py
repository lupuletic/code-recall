"""Tests for the MCP server (code_recall.mcp_server).

These exercise the FastMCP tool layer end-to-end via call_tool, against a
temporary index, with the real-projects reindex disabled. Async calls are
wrapped with asyncio.run so no pytest-asyncio dependency is needed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="mcp extra not installed")

import code_recall.mcp_server as mcp_server


@pytest.fixture
def server(populated_db, db_path, monkeypatch):
    """A FastMCP server pointed at the populated temp index, with the
    pre-query incremental reindex stubbed out."""
    monkeypatch.setattr(mcp_server, "_ensure_fresh_index", lambda *a, **k: None)
    return mcp_server.build_server(db_path=db_path)


def _call(server, name, args):
    """Run a tool and return its structured result payload."""
    _content, structured = asyncio.run(server.call_tool(name, args))
    return structured["result"]


def test_lists_expected_tools(server):
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"search_sessions", "get_session_detail"}


def test_search_returns_structured_hits(server):
    hits = _call(server, "search_sessions", {"query": "auth middleware"})
    assert hits, "expected at least one hit for 'auth middleware'"
    top = hits[0]
    assert top["session_id"] == "abc123"
    for key in (
        "session_id", "title", "project", "provider", "branch",
        "modified", "message_count", "score", "why", "snippet",
        "resume_command",
    ):
        assert key in top
    assert top["resume_command"] == "claude --resume abc123"
    assert 0.0 <= top["score"] <= 1.0


def test_search_respects_limit(server):
    hits = _call(server, "search_sessions", {"query": "the", "limit": 1})
    assert len(hits) <= 1


def test_search_provider_filter(server):
    # All sample sessions are provider 'claude'; codex filter yields nothing.
    assert _call(server, "search_sessions", {"query": "router", "provider": "codex"}) == []
    assert _call(server, "search_sessions", {"query": "router", "provider": "claude"})


def test_get_session_detail(server):
    detail = _call(server, "get_session_detail", {"session_id": "abc123"})
    assert detail["session_id"] == "abc123"
    assert detail["title"] == "Debugging auth middleware"
    assert detail["branch"] == "fix/auth-bug"
    assert detail["resume_command"] == "claude --resume abc123"
    assert isinstance(detail["files_modified"], list)
    assert isinstance(detail["commands_run"], list)


def test_get_session_detail_missing_returns_none(server):
    assert _call(server, "get_session_detail", {"session_id": "nope"}) is None


def test_install_no_agent_cli_returns_error(monkeypatch):
    """install_to_agents reports cleanly when neither agent CLI is present."""
    monkeypatch.setattr(mcp_server.shutil, "which", lambda _name: None)
    assert mcp_server.install_to_agents() == 1


def test_install_registers_found_agents(monkeypatch):
    """install_to_agents runs add for each CLI on PATH and returns 0."""
    import subprocess

    monkeypatch.setattr(
        mcp_server.shutil, "which",
        lambda name: f"/usr/local/bin/{name}" if name in ("claude", "codex") else None,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    assert mcp_server.install_to_agents() == 0
    # Both agents' add commands were issued
    added = [c for c in calls if "add" in c]
    assert any("claude" in c for c in added)
    assert any("codex" in c for c in added)
