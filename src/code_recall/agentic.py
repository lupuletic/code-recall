"""Agentic answer synthesis over indexed coding-agent sessions."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from code_recall.db import DB_PATH, get_connection, get_related_sessions
from code_recall.models import SearchResult
from code_recall.utils import CODEX_DIR, PROJECTS_DIR, app_data_dir, clean_display_text, format_date

READ_ONLY_TOOLS = "Read,Grep,Glob"


@dataclass
class EvidenceSource:
    rank: int
    session_id: str
    title: str
    project_path: str
    activity: str | None
    score: float
    resume_command: str
    file_path: str


@dataclass
class AgenticAnswer:
    ok: bool
    query: str
    answer: str
    sources: list[EvidenceSource]
    error: str | None = None
    assistant_provider: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "query": self.query,
            "answer": self.answer,
            "sources": [asdict(source) for source in self.sources],
            "error": self.error,
            "assistant_provider": self.assistant_provider,
        }


def answer_query(
    query: str,
    results: list[SearchResult],
    db_path: Path = DB_PATH,
    model: str | None = None,
    timeout: int = 90,
    max_sessions: int = 8,
    use_read_tools: bool = True,
    assistant_provider: str = "auto",
    preferred_provider: str | None = None,
) -> AgenticAnswer:
    """Ask an available coding assistant to answer using bounded indexed evidence."""
    selected = results[:max_sessions]
    sources = [_source_for_result(result, i + 1) for i, result in enumerate(selected)]
    if not selected:
        return AgenticAnswer(
            ok=False,
            query=query,
            answer="No indexed sessions matched the question.",
            sources=[],
            error="no_results",
        )

    assistant = _select_assistant(assistant_provider, preferred_provider, selected)
    if assistant is None:
        requested = assistant_provider if assistant_provider != "auto" else (preferred_provider or "claude/codex")
        return AgenticAnswer(
            ok=False,
            query=query,
            answer=(
                "No supported AI CLI was found in PATH for this request. "
                "Install Claude Code or Codex, or choose a provider that is available."
            ),
            sources=sources,
            error=f"missing_ai_cli:{requested}",
            assistant_provider=None,
        )

    try:
        prompt = build_prompt(query, selected, db_path)
    except Exception as exc:
        return AgenticAnswer(
            ok=False,
            query=query,
            answer="AI investigation could not prepare the indexed session evidence.",
            sources=sources,
            error=f"prompt_build_failed: {exc}",
            assistant_provider=assistant[0],
        )

    command = _assistant_command(assistant[0], assistant[1], model, use_read_tools)

    try:
        proc = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return AgenticAnswer(
            ok=False,
            query=query,
            answer="AI investigation timed out before the assistant returned an answer.",
            sources=sources,
            error="timeout",
            assistant_provider=assistant[0],
        )
    except Exception as exc:
        return AgenticAnswer(
            ok=False,
            query=query,
            answer="AI investigation failed before the assistant returned an answer.",
            sources=sources,
            error=str(exc),
            assistant_provider=assistant[0],
        )

    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "AI CLI failed").strip()
        return AgenticAnswer(
            ok=False,
            query=query,
            answer="AI investigation failed.",
            sources=sources,
            error=error[:1000],
            assistant_provider=assistant[0],
        )

    answer = proc.stdout.strip()
    return AgenticAnswer(
        ok=bool(answer),
        query=query,
        answer=answer or "The assistant returned an empty answer.",
        sources=sources,
        error=None if answer else "empty_answer",
        assistant_provider=assistant[0],
    )


def _select_assistant(
    assistant_provider: str,
    preferred_provider: str | None,
    results: list[SearchResult],
) -> tuple[str, str] | None:
    """Return (provider, executable) for the assistant to use."""
    provider_order: list[str] = []
    if assistant_provider != "auto":
        provider_order.append(assistant_provider)
    else:
        if preferred_provider:
            provider_order.append(preferred_provider)
        elif results:
            provider_order.append(_dominant_provider(results))
        provider_order.extend(["claude", "codex"])

    seen = set()
    for provider in provider_order:
        if provider in seen:
            continue
        seen.add(provider)
        if provider not in ("claude", "codex"):
            continue
        executable = shutil.which(provider)
        if executable:
            return provider, executable
    return None


def _dominant_provider(results: list[SearchResult]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        provider = result.session.provider or "claude"
        counts[provider] = counts.get(provider, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0] if counts else "claude"


def _assistant_command(
    provider: str,
    executable: str,
    model: str | None,
    use_read_tools: bool,
) -> list[str]:
    if provider == "codex":
        command = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-s",
            "read-only",
            "-C",
            str(Path.home()),
        ]
        if use_read_tools:
            command.extend([
                "--add-dir",
                str(PROJECTS_DIR),
                "--add-dir",
                str(CODEX_DIR),
                "--add-dir",
                str(app_data_dir()),
            ])
        if model:
            command.extend(["--model", model])
        command.append("-")
        return command

    command = [
        executable,
        "-p",
        "--model",
        model or "haiku",
        "--no-session-persistence",
        "--output-format",
        "text",
    ]
    if use_read_tools:
        command.extend([
            "--tools",
            READ_ONLY_TOOLS,
            "--allowedTools",
            READ_ONLY_TOOLS,
            "--add-dir",
            str(PROJECTS_DIR),
            str(CODEX_DIR),
            str(app_data_dir()),
        ])
    else:
        command.extend(["--tools", ""])
    return command


def build_prompt(query: str, results: list[SearchResult], db_path: Path = DB_PATH) -> str:
    """Build a bounded evidence prompt for an assistant."""
    evidence = []
    related = _related_by_session(results, db_path)
    for i, result in enumerate(results, 1):
        evidence.append(_format_result_evidence(i, result, related.get(result.session.session_id, [])))

    return f"""You are answering a question about past coding-agent sessions.

The app has already retrieved the most relevant indexed sessions using keyword,
semantic, recency, and graph signals. You may use Read/Grep/Glob only to inspect
the listed JSONL source files if the excerpts are not enough. Do not make claims
that are not grounded in the evidence.

User question:
{query}

Answer requirements:
- Start with a direct answer.
- Cite session IDs for important claims.
- Mention projects, dates, files, or commands when they materially distinguish sessions.
- Say when evidence is weak or ambiguous.
- End with the best session(s) to resume and the exact resume command(s).

Retrieved evidence:
{chr(10).join(evidence)}
"""


def _source_for_result(result: SearchResult, rank: int) -> EvidenceSource:
    s = result.session
    title = _clean_text(s.summary) or _clean_text(s.first_prompt) or "Untitled"
    return EvidenceSource(
        rank=rank,
        session_id=s.session_id,
        title=title[:120],
        project_path=s.project_path,
        activity=format_date(s.last_activity or s.modified),
        score=round(result.score, 4),
        resume_command=result.resume_command,
        file_path=s.file_path,
    )


def _format_result_evidence(rank: int, result: SearchResult, related: list) -> str:
    s = result.session
    files = _json_list(s.files_modified)[:12]
    commands = _json_list(s.commands_run)[:8]
    snippet_lines = []
    for snippet in result.snippets[:3]:
        cleaned = _clean_text(snippet)
        if cleaned:
            snippet_lines.append(f"- {cleaned[:400]}")
    snippets = "\n".join(snippet_lines)
    excerpt = _excerpt_for_query(s.messages_text or "", max_chars=4500)
    related_text = "\n".join(
        f"- {row['session_id']} | {row['project_path']} | {row['shared_files']} shared files | "
        f"{_clean_text(row['summary'])[:100] or 'Untitled'}"
        for row in related[:3]
    )

    return f"""
[{rank}] session_id: {s.session_id}
provider: {s.provider}
score: {result.score:.3f}
project: {s.project_path}
activity: {format_date(s.last_activity or s.modified)}
branch: {s.git_branch or "unknown"}
messages: {s.message_count}
source_jsonl: {s.file_path}
resume: {result.resume_command}
summary: {_clean_text(s.summary)}
started: {_clean_text(s.first_prompt)[:500]}
left_off: {_clean_text(s.last_prompt)[:500]}
files: {", ".join(files) if files else "none indexed"}
commands: {", ".join(commands) if commands else "none indexed"}
search_snippets:
{snippets or "- none"}
related_sessions:
{related_text or "- none"}
conversation_excerpt:
{excerpt}
""".strip()


def _related_by_session(results: list[SearchResult], db_path: Path) -> dict[str, list]:
    related: dict[str, list] = {}
    try:
        conn = get_connection(db_path)
        for result in results:
            related[result.session.session_id] = get_related_sessions(conn, result.session.session_id, limit=3)
        conn.close()
    except Exception:
        pass
    return related


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _clean_text(text: str | None) -> str:
    return clean_display_text(text) or ""


def _excerpt_for_query(text: str, max_chars: int = 4500) -> str:
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    head = text[:1200]
    tail = text[-1800:]
    return f"{head}\n...\n{tail}"
