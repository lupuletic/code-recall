"""Tests for the Textual TUI shell."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from code_recall.models import SearchResult, Session
from code_recall.tui import DetailPanel, RecallApp, provider_display
from textual.widgets import Static


def _text(widget) -> str:
    return str(getattr(widget, "content", ""))


def _result(
    session_id: str,
    *,
    provider: str = "claude",
    project: str = "/tmp/project",
    summary: str = "Fix auth middleware",
    score: float = 0.9,
) -> SearchResult:
    provider_session_id = f"{provider}-{session_id}"
    return SearchResult(
        session=Session(
            session_id=session_id,
            provider=provider,
            provider_session_id=provider_session_id,
            project_path=project,
            project_dir="project",
            file_path=f"/tmp/{session_id}.jsonl",
            summary=summary,
            first_prompt=f"Please {summary.lower()}",
            last_prompt=f"Finished {summary.lower()}",
            messages_text=summary,
            git_branch="main",
            message_count=12,
            file_size=4096,
            last_activity="2026-05-19T12:00:00Z",
            files_modified='["src/auth.py", "tests/test_auth.py"]',
            commands_run='["pytest tests/test_auth.py"]',
            model="test-model",
        ),
        score=score,
        fts_rank=1.0,
        vec_score=0.12,
        snippets=["auth middleware matched in src/auth.py"],
    )


def test_provider_display_resume_commands():
    assert provider_display("claude").resume_command("abc") == "claude --resume abc"
    assert provider_display("codex").resume_command("abc") == "codex resume abc"
    assert provider_display("other").label == "Other"


def test_tui_renders_provider_filters_and_detail(tmp_path):
    async def run() -> None:
        app = RecallApp(
            initial_query="auth",
            initial_results=[
                _result("s1", provider="claude", summary="Fix auth middleware"),
                _result("s2", provider="codex", summary="Debug Codex auth regression", score=0.75),
            ],
            db_path=tmp_path / "missing.db",
        )
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            assert "2 of 2 results" in _text(app.query_one("#results-meta"))
            assert "Claude 1" in _text(app.query_one("#filter-bar"))
            assert "Codex 1" in _text(app.query_one("#filter-bar"))
            assert "visible" not in app.query_one("#results-loading").classes
            assert "loading-results" not in app.query_one("#results").classes

            detail = app.query_one("#detail", DetailPanel)
            assert detail.result is not None
            assert detail.result.session.provider == "claude"

            app.action_cycle_provider()
            await pilot.pause()
            assert app._provider_scope == "claude"
            assert len(app._visible_results) == 1
            assert app._visible_results[0].session.provider == "claude"

            app.action_cycle_provider()
            await pilot.pause()
            assert app._provider_scope == "codex"
            assert len(app._visible_results) == 1
            assert app._visible_results[0].session.provider == "codex"
            assert app.query_one("#detail", DetailPanel).result.session.provider == "codex"

    asyncio.run(run())


def test_tui_detail_tabs_group_information(tmp_path):
    async def run() -> None:
        app = RecallApp(
            initial_query="file:auth.py",
            initial_results=[_result("s1")],
            db_path=tmp_path / "missing.db",
        )
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            detail = app.query_one("#detail", DetailPanel)

            app.action_detail_tab("why")
            await pilot.pause()
            assert detail.active_tab == "why"

            app.action_detail_tab("activity")
            await pilot.pause()
            assert detail.active_tab == "activity"

            app.action_detail_tab("ai")
            await pilot.pause()
            assert detail.active_tab == "ai"

    asyncio.run(run())


def test_tui_marks_narrow_layout_and_opens_detail_view(tmp_path):
    async def run() -> None:
        app = RecallApp(
            initial_query="auth",
            initial_results=[_result("s1")],
            db_path=tmp_path / "missing.db",
        )
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            assert "narrow" in app.screen.classes

            app.action_focus_detail()
            await pilot.pause()
            assert "detail-visible" in app.query_one("#detail-column").classes

            app.action_escape()
            await pilot.pause()
            assert "detail-visible" not in app.query_one("#detail-column").classes

    asyncio.run(run())


def test_tui_ai_tab_exposes_transcript_chat_input(tmp_path):
    async def run() -> None:
        app = RecallApp(
            initial_query="auth",
            initial_results=[_result("s1", provider="codex")],
            db_path=tmp_path / "missing.db",
        )
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            app.action_detail_tab("ai")
            await pilot.pause()

            detail = app.query_one("#detail", DetailPanel)
            ai_input = app.query_one("#ai-chat-input")
            assert detail.active_tab == "ai"
            assert "visible" in ai_input.classes
            assert "Transcript chat" in _text(detail.query_one(Static))

    asyncio.run(run())


def test_tui_search_loading_banner_is_explicit(tmp_path):
    async def run() -> None:
        app = RecallApp(
            initial_query="auth",
            initial_results=[_result("s1")],
            db_path=tmp_path / "missing.db",
        )
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()

            app._set_results_loading(
                True,
                'Searching "auth" in LLM mode. Results below are from the previous search until this finishes.',
            )
            await pilot.pause()

            assert "visible" in app.query_one("#results-loading").classes
            assert "loading-results" in app.query_one("#results").classes
            assert "LLM mode" in _text(app.query_one("#results-loading-text", Static))
            assert "previous search" in _text(app.query_one("#results-loading-text", Static))

            app._set_results_loading(False)
            await pilot.pause()

            assert "visible" not in app.query_one("#results-loading").classes
            assert "loading-results" not in app.query_one("#results").classes
            assert _text(app.query_one("#results-loading-text", Static)) == ""

    asyncio.run(run())
