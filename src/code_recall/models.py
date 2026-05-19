"""Data models for code-recall."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    """A coding-agent session extracted from a local transcript."""

    session_id: str
    project_path: str
    project_dir: str
    file_path: str
    provider: str = "claude"
    provider_session_id: str | None = None
    summary: str | None = None
    first_prompt: str | None = None
    first_reply: str | None = None
    last_prompt: str | None = None
    last_reply: str | None = None
    messages_text: str | None = None
    git_branch: str | None = None
    message_count: int = 0
    file_size: int = 0
    created: str | None = None
    modified: str | None = None
    last_activity: str | None = None
    mtime: float = 0.0
    is_subagent: bool = False
    parent_session: str | None = None
    files_modified: str | None = None  # JSON list of file paths
    commands_run: str | None = None  # JSON list of commands
    git_branch_detected: str | None = None
    model: str | None = None


@dataclass
class SearchResult:
    """A search result with relevance scoring and context."""

    session: Session
    score: float = 0.0
    fts_rank: float | None = None
    vec_score: float | None = None
    snippets: list[str] = field(default_factory=list)

    @property
    def display_project(self) -> str:
        """Short project path for display (~/Projects/foo)."""
        path = self.session.project_path
        import os

        home = os.path.expanduser("~")
        if path.startswith(home):
            return "~" + path[len(home) :]
        return path

    @property
    def resume_command(self) -> str:
        session_id = self.session.provider_session_id or self.session.session_id
        if self.session.provider == "codex":
            return f"codex resume {session_id}"
        return f"claude --resume {session_id}"
