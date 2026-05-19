"""Tests for agentic answer synthesis."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from code_recall import agentic
from code_recall.models import SearchResult, Session


def _result() -> SearchResult:
    return SearchResult(
        session=Session(
            session_id="s1",
            project_path="/tmp/project",
            project_dir="project",
            file_path="/tmp/project/session.jsonl",
            summary="Debugged auth middleware",
            first_prompt="fix auth bug",
            last_prompt="tests are passing",
            messages_text="User asked about auth middleware. Claude fixed token validation.",
            files_modified='["auth.py", "tests/test_auth.py"]',
            commands_run='["pytest tests/test_auth.py"]',
            modified="2026-05-13T10:00:00Z",
            message_count=8,
        ),
        score=0.9,
        snippets=["auth middleware token validation"],
    )


def _codex_result() -> SearchResult:
    result = _result()
    result.session.provider = "codex"
    result.session.provider_session_id = "thread-1"
    result.session.session_id = "codex:thread-1"
    return result


def test_answer_query_without_results():
    answer = agentic.answer_query("auth?", [], use_read_tools=False)

    assert answer.ok is False
    assert answer.error == "no_results"
    assert answer.sources == []


def test_answer_query_without_ai_cli(monkeypatch):
    monkeypatch.setattr(agentic.shutil, "which", lambda name: None)

    answer = agentic.answer_query("auth?", [_result()], use_read_tools=False)

    assert answer.ok is False
    assert answer.error == "missing_ai_cli:claude/codex"
    assert answer.sources[0].session_id == "s1"


def test_answer_query_returns_error_when_prompt_build_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(agentic.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(agentic, "build_prompt", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    answer = agentic.answer_query("auth?", [_result()], db_path=tmp_path / "missing.db")

    assert answer.ok is False
    assert answer.error == "prompt_build_failed: boom"
    assert answer.sources[0].session_id == "s1"


def test_answer_query_invokes_claude_with_read_only_tools(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(agentic.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(agentic, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(agentic, "app_data_dir", lambda: tmp_path / "app")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="Answer with session s1", stderr="")

    monkeypatch.setattr(agentic.subprocess, "run", fake_run)

    answer = agentic.answer_query(
        "what happened with auth?",
        [_result()],
        db_path=tmp_path / "missing.db",
        model="haiku",
        use_read_tools=True,
    )

    assert answer.ok is True
    assert answer.answer == "Answer with session s1"
    command = calls[0][0]
    assert "--tools" in command
    assert agentic.READ_ONLY_TOOLS in command
    assert "--add-dir" in command
    assert "what happened with auth?" in calls[0][1]["input"]
    assert answer.assistant_provider == "claude"


def test_answer_query_prefers_codex_for_codex_sessions(monkeypatch, tmp_path):
    calls = []

    def fake_which(name):
        return {"claude": "/bin/claude", "codex": "/bin/codex"}.get(name)

    monkeypatch.setattr(agentic.shutil, "which", fake_which)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="Codex answer", stderr="")

    monkeypatch.setattr(agentic.subprocess, "run", fake_run)

    answer = agentic.answer_query(
        "what happened?",
        [_codex_result()],
        db_path=tmp_path / "missing.db",
        preferred_provider="codex",
        use_read_tools=True,
    )

    assert answer.ok is True
    assert answer.assistant_provider == "codex"
    command = calls[0][0]
    assert command[:2] == ["/bin/codex", "exec"]
    assert "-s" in command
    assert "read-only" in command


def test_answer_query_falls_back_when_matching_provider_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(agentic.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(
        agentic.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="Claude fallback", stderr=""),
    )

    answer = agentic.answer_query(
        "what happened?",
        [_codex_result()],
        db_path=tmp_path / "missing.db",
        preferred_provider="codex",
        use_read_tools=False,
    )

    assert answer.ok is True
    assert answer.assistant_provider == "claude"


def test_build_prompt_contains_grounding_details(tmp_path):
    prompt = agentic.build_prompt("auth question", [_result()], db_path=tmp_path / "missing.db")

    assert "auth question" in prompt
    assert "session_id: s1" in prompt
    assert "source_jsonl: /tmp/project/session.jsonl" in prompt
    assert "pytest tests/test_auth.py" in prompt


def test_build_prompt_handles_markup_only_cleaned_text(tmp_path):
    result = _result()
    result.session.summary = "<system-reminder>hidden</system-reminder>"
    result.session.first_prompt = "<environment_context>hidden</environment_context>"
    result.session.last_prompt = "<local-command-caveat>hidden</local-command-caveat>"
    result.session.messages_text = "<local-command-caveat>hidden</local-command-caveat>"
    result.snippets = ["<local-command-caveat>hidden</local-command-caveat>"]

    prompt = agentic.build_prompt("auth question", [result], db_path=tmp_path / "missing.db")

    assert "session_id: s1" in prompt
    assert "search_snippets:\n- none" in prompt
