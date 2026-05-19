"""Tests for provider-aware LLM reranking."""

from __future__ import annotations

from types import SimpleNamespace

from code_recall import agentic
from code_recall.llm_reranker import llm_rerank


def test_llm_rerank_uses_codex_when_claude_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(agentic.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="[1, 0]", stderr="")

    monkeypatch.setattr("code_recall.llm_reranker.subprocess.run", fake_run)

    order = llm_rerank(
        "auth",
        [
            {"summary": "less relevant", "project_path": "/repo/a"},
            {"summary": "auth fix", "project_path": "/repo/b"},
        ],
    )

    assert order == [1, 0]
    assert calls[0][0][:2] == ["/bin/codex", "exec"]


def test_llm_rerank_falls_back_to_original_order_without_ai(monkeypatch):
    monkeypatch.setattr(agentic.shutil, "which", lambda name: None)

    order = llm_rerank("auth", [{"summary": "a"}, {"summary": "b"}])

    assert order == [0, 1]
