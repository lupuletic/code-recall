"""Utilities for parsing local coding-agent session files."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_DIR = Path.home() / ".codex"
CODEX_STATE_DB = CODEX_DIR / "state_5.sqlite"
APP_DIR_NAME = ".code-recall"
LEGACY_APP_DIR_NAMES = (".claude-code-recall", ".claude-recall")


def app_data_dir() -> Path:
    """Return the app data directory, migrating the old name when possible."""
    app_dir = Path.home() / APP_DIR_NAME
    if app_dir.exists():
        return app_dir
    for legacy_name in LEGACY_APP_DIR_NAMES:
        legacy_dir = Path.home() / legacy_name
        if not legacy_dir.exists():
            continue
        try:
            legacy_dir.rename(app_dir)
            return app_dir
        except OSError:
            return legacy_dir
    return app_dir

# Max chars to keep for indexed text fields
MAX_FIRST_PROMPT = 500
MAX_FIRST_REPLY = 500
MAX_MESSAGES_TEXT = 50_000  # ~50KB per session — balances coverage with BM25 precision
MAX_TOOL_CONTEXT = 30_000  # Bounded operational context from tool calls/results


def decode_project_path(project_dir: str, projects_dir: Path = PROJECTS_DIR) -> str:
    """Decode an encoded project directory name back to a filesystem path.

    Checks sessions-index.json for originalPath first (most reliable),
    then tries to reconstruct the path by testing which combination of
    dashes-as-slashes vs dashes-as-literal-dashes actually exists on disk.
    """
    # Best source: sessions-index.json has the original path
    idx_path = projects_dir / project_dir / "sessions-index.json"
    if idx_path.exists():
        try:
            with open(idx_path) as f:
                data = json.load(f)
                if data.get("originalPath"):
                    return data["originalPath"]
        except (json.JSONDecodeError, OSError):
            pass

    if not project_dir.startswith("-"):
        return project_dir

    # Smart decode: try to find which path actually exists on disk
    # The encoding replaces / with - but folder names can also contain -
    # So "-Users-foo-my-project" could be /Users/foo/my-project or /Users/foo/my/project
    # We try the most likely paths by splitting on - and testing existence
    parts = project_dir[1:].split("-")  # strip leading dash, split
    resolved = _resolve_path_parts(parts)
    if resolved:
        return resolved

    # Last resort: naive replace (may be wrong for paths with dashes)
    decoded = project_dir.replace("-", "/")
    if sys.platform == "win32" and len(decoded) > 2 and decoded[2] == "/":
        decoded = decoded[1] + ":" + decoded[2:]
    return decoded


def _resolve_path_parts(parts: list[str]) -> str | None:
    """Try to reconstruct a filesystem path from encoded parts.

    Greedily matches the longest existing directory at each level.
    e.g. ["Users", "foo", "demo", "project"] tries:
      /Users → exists, consume
      /Users/foo → exists, consume
      /Users/foo/demo-project → exists! consume both
    """
    if not parts:
        return None

    current = "/"
    i = 0

    while i < len(parts):
        # Try joining progressively more parts with dashes (longest match first)
        matched = False
        for end in range(len(parts), i, -1):
            candidate = "-".join(parts[i:end])
            test_path = os.path.join(current, candidate)
            if os.path.isdir(test_path):
                current = test_path
                i = end
                matched = True
                break

        if not matched:
            # No match found — use single part and continue
            current = os.path.join(current, parts[i])
            i += 1

    return current


import re

# Patterns to strip from displayed text (internal Claude Code markup)
_MARKUP_PATTERNS = [
    # Strip any XML/HTML-like tags that are internal Claude Code markup
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL),
    re.compile(r"<local-command-stderr>.*?</local-command-stderr>", re.DOTALL),
    re.compile(r"<teammate-message[^>]*>.*?</teammate-message>", re.DOTALL),
    re.compile(r"<command-name>.*?</command-name>", re.DOTALL),
    re.compile(r"<command-message>.*?</command-message>", re.DOTALL),
    re.compile(r"<command-args>.*?</command-args>", re.DOTALL),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<user_instructions>.*?</user_instructions>", re.DOTALL),
    re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL),
    re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL),
    re.compile(r"\[Request interrupted by user\]"),
    # Catch any remaining XML-style tags
    re.compile(r"<[a-z_-]+(?:\s[^>]*)?>.*?</[a-z_-]+>", re.DOTALL),
    re.compile(r"<[a-z_-]+(?:\s[^>]*)?\s*/?>"),
]


def clean_display_text(text: str | None) -> str | None:
    """Strip internal Claude Code markup from text for display."""
    if not text:
        return text
    for pattern in _MARKUP_PATTERNS:
        text = pattern.sub("", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def extract_text_from_content(content) -> str | None:
    """Extract readable text from a message content field.

    Content can be a plain string or a list of content blocks.
    """
    if isinstance(content, str):
        return content.strip() if content.strip() else None

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts) if texts else None

    return None


def _clip(text: str, max_chars: int = 1000) -> str:
    """Normalize and truncate text for indexing."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _append_unique(items: list[str], text: str | None, max_items: int = 120) -> None:
    """Append a non-empty text item once, keeping the list bounded."""
    if len(items) >= max_items or not text:
        return
    clipped = _clip(text)
    if clipped and clipped not in items:
        items.append(clipped)


def _looks_like_path(value: str) -> bool:
    """Return True for likely file or directory paths from tool inputs."""
    if not value or len(value) > 500:
        return False
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or bool(re.search(r"\.[A-Za-z0-9]{1,12}$", value))
    )


def _tool_result_texts(content) -> list[str]:
    """Extract bounded text from Claude tool_result blocks without counting as user prompts."""
    if not isinstance(content, list):
        return []

    results = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        payload = block.get("content")
        text = extract_text_from_content(payload)
        if text is None and isinstance(payload, str):
            text = payload
        if text:
            results.append(_clip(text, 1200))
    return results


def _tool_input_fragments(value, prefix: str = "") -> list[str]:
    """Flatten high-signal scalar fields from a tool input object."""
    fragments: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_l = str(key).lower()
            if isinstance(child, (dict, list)):
                fragments.extend(_tool_input_fragments(child, key_l))
            elif child is not None and key_l in {
                "command",
                "description",
                "file_path",
                "glob",
                "path",
                "pattern",
                "prompt",
                "query",
                "url",
                "content",
                "title",
                "status",
            }:
                label = prefix or key_l
                fragments.append(f"{label}: {_clip(str(child), 500)}")
    elif isinstance(value, list):
        for child in value[:20]:
            fragments.extend(_tool_input_fragments(child, prefix))
    return fragments


def _tool_context_line(tool: str, inp: dict) -> str | None:
    """Build a compact searchable description of a tool call."""
    fragments = _tool_input_fragments(inp)
    if not fragments:
        return None
    return f"Tool {tool}: " + " | ".join(fragments[:12])


def _parse_json_arguments(value) -> dict:
    """Return a dict from a JSON-ish tool arguments payload."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _content_texts(content, text_keys: tuple[str, ...] = ("text", "input_text", "output_text")) -> list[str]:
    """Extract text blocks from OpenAI/Codex-style message content."""
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    if not isinstance(content, list):
        return []

    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in text_keys:
            text = block.get(key)
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
                break
    return texts


def parse_session_file(file_path: str | Path) -> dict:
    """Parse a session .jsonl file and extract searchable content.

    Returns dict with:
        first_prompt, first_reply, last_prompt, last_reply: str | None
        messages_text: str  (sampled user+assistant messages for FTS)
        message_count: int
        chunks: list[str]  (conversation chunks for embedding)
        files_modified: list[str]  (file paths edited/written)
        files_read: list[str]  (file paths inspected/read)
        files_touched: list[str]  (read + edited paths)
        commands_run: list[str]  (key bash commands)
        git_branch_detected: str | None
    """
    first_prompt = None
    first_reply = None
    last_prompt = None
    last_reply = None
    user_messages: list[str] = []
    assistant_texts: list[str] = []
    files_modified: set[str] = set()
    files_read: set[str] = set()
    commands_run: list[str] = []
    tool_context: list[str] = []
    pr_links: list[str] = []
    ai_title: str | None = None
    git_branch_detected: str | None = None
    first_activity: str | None = None
    last_activity: str | None = None

    # Commands to skip (low signal)
    _SKIP_CMDS = {"cd", "ls", "cat", "echo", "pwd", "head", "tail", "wc", "true", "false"}

    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                timestamp = obj.get("timestamp")

                if msg_type == "ai-title":
                    title = obj.get("aiTitle")
                    if isinstance(title, str) and title.strip():
                        ai_title = title.strip()
                        _append_unique(tool_context, f"AI title: {ai_title}")
                    continue

                if msg_type == "pr-link":
                    pr_url = obj.get("prUrl")
                    repo = obj.get("prRepository")
                    pr_number = obj.get("prNumber")
                    text = " ".join(str(p) for p in (repo, pr_number, pr_url) if p)
                    if text:
                        pr_links.append(text)
                        _append_unique(tool_context, f"Pull request: {text}")
                    continue

                if msg_type == "user":
                    if timestamp:
                        if first_activity is None:
                            first_activity = timestamp
                        last_activity = timestamp
                    msg = obj.get("message", {})
                    text = extract_text_from_content(msg.get("content", ""))
                    if text:
                        user_messages.append(text)
                        # Use cleaned text for display fields
                        cleaned = clean_display_text(text)
                        if cleaned:
                            if first_prompt is None:
                                first_prompt = cleaned[:MAX_FIRST_PROMPT]
                            last_prompt = cleaned[:MAX_FIRST_PROMPT]
                    for result_text in _tool_result_texts(msg.get("content")):
                        _append_unique(tool_context, f"Tool result: {result_text}", max_items=80)

                elif msg_type == "assistant":
                    if timestamp:
                        if first_activity is None:
                            first_activity = timestamp
                        last_activity = timestamp
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    text = extract_text_from_content(content)
                    if text:
                        assistant_texts.append(text)
                        cleaned = clean_display_text(text)
                        if cleaned:
                            if first_reply is None:
                                first_reply = cleaned[:MAX_FIRST_REPLY]
                            last_reply = cleaned[:MAX_FIRST_REPLY]

                    # Extract tool calls (files modified, commands run)
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            tool = block.get("name", "")
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                _append_unique(tool_context, _tool_context_line(tool, inp))

                            if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                                fp = inp.get("file_path", "")
                                if fp:
                                    # Store full file path for accurate graph edges
                                    files_modified.add(fp)
                                    files_read.add(fp)

                            elif tool == "Read":
                                fp = inp.get("file_path", "")
                                if fp:
                                    files_read.add(fp)

                            elif tool in ("Grep", "Glob", "LS"):
                                for key in ("path", "glob", "pattern"):
                                    value = inp.get(key, "")
                                    if isinstance(value, str) and _looks_like_path(value):
                                        files_read.add(value)

                            elif tool == "Bash":
                                cmd = inp.get("command", "").strip()
                                if cmd:
                                    # Extract first word (the actual command)
                                    first_word = cmd.split()[0] if cmd.split() else ""
                                    if first_word and first_word not in _SKIP_CMDS:
                                        commands_run.append(cmd[:200])
                                    # Detect git branch
                                    if not git_branch_detected:
                                        for pattern in ["git checkout -b ", "git checkout ", "git switch -c ", "git switch "]:
                                            if pattern in cmd:
                                                branch = cmd.split(pattern)[-1].split()[0]
                                                if branch and not branch.startswith("-"):
                                                    git_branch_detected = branch
                                                    break

                # Detect git branch from session metadata
                if not git_branch_detected and obj.get("gitBranch"):
                    git_branch_detected = obj["gitBranch"]

    except OSError:
        pass

    # Build FTS text: smart sampling of conversation plus operational recall profile.
    profile_parts = []
    if ai_title:
        profile_parts.append(f"AI title: {ai_title}")
    if pr_links:
        profile_parts.append("Pull requests: " + " ".join(pr_links[:8]))
    if files_modified:
        profile_parts.append("Files edited: " + " ".join(sorted(files_modified)[:80]))
    read_only_files = files_read - files_modified
    if read_only_files:
        profile_parts.append("Files read: " + " ".join(sorted(read_only_files)[:120]))
    if commands_run:
        profile_parts.append("Commands: " + " ".join(commands_run[:40]))
    if tool_context:
        profile_parts.append("Tool context: " + "\n".join(tool_context))
    recall_profile = "\n".join(profile_parts)[:MAX_TOOL_CONTEXT]

    messages_text = _build_fts_text(user_messages, assistant_texts, [recall_profile] if recall_profile else None)

    # Build conversation chunks for embedding
    chunks = _build_chunks(user_messages, assistant_texts, [recall_profile] if recall_profile else None)

    # Auto-generate summary from first prompt + reply
    summary = ai_title or generate_summary(first_prompt, first_reply)

    # Deduplicate commands by full command string and limit
    seen_cmds: set[str] = set()
    unique_cmds: list[str] = []
    for cmd in commands_run:
        if cmd not in seen_cmds:
            seen_cmds.add(cmd)
            unique_cmds.append(cmd)
    commands_run = unique_cmds[:30]

    return {
        "first_prompt": first_prompt,
        "first_reply": first_reply,
        "last_prompt": last_prompt,
        "last_reply": last_reply,
        "messages_text": messages_text,
        "message_count": len(user_messages),
        "chunks": chunks,
        "summary": summary,
        "ai_title": ai_title,
        "pr_links": pr_links[:10],
        "tool_context": tool_context[:120],
        "files_modified": sorted(files_modified)[:50],
        "files_read": sorted(read_only_files)[:100],
        "files_touched": sorted(files_modified | files_read)[:150],
        "file_actions": [
            {"path": path, "action": "edit"} for path in sorted(files_modified)[:80]
        ] + [
            {"path": path, "action": "read"} for path in sorted(read_only_files)[:120]
        ],
        "commands_run": commands_run,
        "git_branch_detected": git_branch_detected,
        "first_activity": first_activity,
        "last_activity": last_activity,
    }


def _codex_indexed_session_id(thread_id: str) -> str:
    """Namespace Codex thread IDs to avoid primary-key collisions with Claude."""
    return f"codex:{thread_id}"


def discover_codex_sessions(codex_dir: Path = CODEX_DIR) -> list[dict]:
    """Discover Codex sessions from the local thread index and rollout files."""
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, rollout_path, cwd, title, first_user_message,
                      model_provider, model, git_branch, created_at, updated_at,
                      created_at_ms, updated_at_ms, source, thread_source
               FROM threads
               WHERE archived = 0
               ORDER BY updated_at DESC"""
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    sessions = []
    for row in rows:
        thread_id = row["id"]
        file_path = Path(row["rollout_path"])
        if not file_path.is_absolute():
            file_path = codex_dir / file_path
        if not file_path.exists():
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue

        cwd = row["cwd"] or ""
        updated_ms = row["updated_at_ms"]
        created_ms = row["created_at_ms"]
        sessions.append({
            "provider": "codex",
            "session_id": _codex_indexed_session_id(thread_id),
            "provider_session_id": thread_id,
            "file_path": str(file_path),
            "project_dir": cwd or "codex",
            "project_path": cwd or str(codex_dir),
            "is_subagent": False,
            "parent_session": None,
            "file_size": stat.st_size,
            "mtime": stat.st_mtime,
            "title": row["title"],
            "first_user_message": row["first_user_message"],
            "model_provider": row["model_provider"],
            "model": row["model"],
            "git_branch": row["git_branch"],
            "source": row["source"],
            "thread_source": row["thread_source"],
            "created": _millis_to_iso(created_ms) if created_ms else _unix_to_iso(row["created_at"]),
            "modified": _millis_to_iso(updated_ms) if updated_ms else _unix_to_iso(row["updated_at"]),
        })
    return sessions


def _unix_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _millis_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def parse_codex_session_file(file_path: str | Path, metadata: dict | None = None) -> dict:
    """Parse a Codex rollout JSONL file into the common indexed-session shape."""
    metadata = metadata or {}
    first_prompt = None
    first_reply = None
    last_prompt = None
    last_reply = None
    user_messages: list[str] = []
    assistant_texts: list[str] = []
    files_modified: set[str] = set()
    files_read: set[str] = set()
    commands_run: list[str] = []
    tool_context: list[str] = []
    first_activity: str | None = metadata.get("created")
    last_activity: str | None = metadata.get("modified")
    title = metadata.get("title") or None
    git_branch_detected = metadata.get("git_branch")

    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = obj.get("timestamp")
                if timestamp:
                    if first_activity is None:
                        first_activity = timestamp
                    last_activity = timestamp

                event_type = obj.get("type")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                if event_type == "session_meta":
                    meta = payload
                    title = title or meta.get("title")
                    git_branch_detected = git_branch_detected or meta.get("git_branch")
                    continue

                if event_type == "event_msg":
                    payload_type = payload.get("type")
                    if payload_type == "user_message":
                        text = payload.get("message")
                        if isinstance(text, str) and text.strip():
                            _add_codex_user_text(text, user_messages, tool_context)
                            cleaned = clean_display_text(text)
                            if cleaned:
                                if first_prompt is None:
                                    first_prompt = cleaned[:MAX_FIRST_PROMPT]
                                last_prompt = cleaned[:MAX_FIRST_PROMPT]
                    elif payload_type == "agent_message":
                        text = payload.get("message")
                        if isinstance(text, str) and text.strip():
                            assistant_texts.append(text)
                            cleaned = clean_display_text(text)
                            if cleaned:
                                if first_reply is None:
                                    first_reply = cleaned[:MAX_FIRST_REPLY]
                                last_reply = cleaned[:MAX_FIRST_REPLY]
                            _extract_codex_external_tool_text(text, files_modified, files_read, commands_run, tool_context)
                    continue

                if event_type != "response_item":
                    continue

                item_type = payload.get("type")
                if item_type == "message":
                    role = payload.get("role")
                    texts = _content_texts(payload.get("content"))
                    text = "\n".join(texts).strip()
                    if not text:
                        continue
                    if role == "user":
                        _add_codex_user_text(text, user_messages, tool_context)
                        cleaned = clean_display_text(text)
                        if cleaned:
                            if first_prompt is None:
                                first_prompt = cleaned[:MAX_FIRST_PROMPT]
                            last_prompt = cleaned[:MAX_FIRST_PROMPT]
                    elif role == "assistant":
                        assistant_texts.append(text)
                        cleaned = clean_display_text(text)
                        if cleaned:
                            if first_reply is None:
                                first_reply = cleaned[:MAX_FIRST_REPLY]
                            last_reply = cleaned[:MAX_FIRST_REPLY]
                        _extract_codex_external_tool_text(text, files_modified, files_read, commands_run, tool_context)
                elif item_type == "function_call":
                    name = str(payload.get("name") or "")
                    args = _parse_json_arguments(payload.get("arguments"))
                    _append_unique(tool_context, _tool_context_line(name, args))
                    _extract_codex_function_call(name, args, files_modified, files_read, commands_run)
                elif item_type == "function_call_output":
                    output = payload.get("output")
                    if isinstance(output, str) and output.strip():
                        _append_unique(tool_context, f"Tool result: {_clip(output, 1200)}", max_items=80)
    except OSError:
        pass

    if not first_prompt and metadata.get("first_user_message"):
        cleaned = clean_display_text(metadata["first_user_message"]) or str(metadata["first_user_message"])
        first_prompt = cleaned[:MAX_FIRST_PROMPT]
        last_prompt = first_prompt
        user_messages.append(metadata["first_user_message"])

    profile_parts = []
    if title:
        profile_parts.append(f"Codex title: {title}")
    if metadata.get("model"):
        profile_parts.append(f"Model: {metadata['model']}")
    if metadata.get("model_provider"):
        profile_parts.append(f"Model provider: {metadata['model_provider']}")
    if files_modified:
        profile_parts.append("Files edited: " + " ".join(sorted(files_modified)[:80]))
    read_only_files = files_read - files_modified
    if read_only_files:
        profile_parts.append("Files read: " + " ".join(sorted(read_only_files)[:120]))
    if commands_run:
        profile_parts.append("Commands: " + " ".join(commands_run[:40]))
    if tool_context:
        profile_parts.append("Tool context: " + "\n".join(tool_context))
    recall_profile = "\n".join(profile_parts)[:MAX_TOOL_CONTEXT]

    messages_text = _build_fts_text(user_messages, assistant_texts, [recall_profile] if recall_profile else None)
    chunks = _build_chunks(user_messages, assistant_texts, [recall_profile] if recall_profile else None)
    summary = title or generate_summary(first_prompt, first_reply)

    return {
        "first_prompt": first_prompt,
        "first_reply": first_reply,
        "last_prompt": last_prompt,
        "last_reply": last_reply,
        "messages_text": messages_text,
        "message_count": len(user_messages),
        "chunks": chunks,
        "summary": summary,
        "ai_title": title,
        "files_modified": sorted(files_modified)[:50],
        "files_read": sorted(read_only_files)[:100],
        "files_touched": sorted(files_modified | files_read)[:150],
        "file_actions": [
            {"path": path, "action": "edit"} for path in sorted(files_modified)[:80]
        ] + [
            {"path": path, "action": "read"} for path in sorted(read_only_files)[:120]
        ],
        "commands_run": _dedupe(commands_run)[:30],
        "git_branch_detected": git_branch_detected,
        "first_activity": first_activity,
        "last_activity": last_activity,
    }


def _add_codex_user_text(text: str, user_messages: list[str], tool_context: list[str]) -> None:
    """Add user-authored text while keeping shell IO searchable but lower signal."""
    cleaned = clean_display_text(text)
    if cleaned and (not user_messages or user_messages[-1] != cleaned):
        user_messages.append(cleaned)
    if "<bash-" in text:
        _append_unique(tool_context, f"Shell interaction: {_clip(text, 1200)}", max_items=80)


def _extract_codex_function_call(
    name: str,
    args: dict,
    files_modified: set[str],
    files_read: set[str],
    commands_run: list[str],
) -> None:
    """Extract commands and file paths from Codex function/tool calls."""
    lname = name.lower()
    if lname.endswith("exec_command") or lname == "exec_command":
        cmd = str(args.get("cmd") or args.get("command") or "").strip()
        if cmd:
            commands_run.append(cmd[:200])
    if lname.endswith("apply_patch") or lname == "apply_patch":
        patch_text = str(args.get("patch") or args.get("input") or "")
        for path in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch_text, flags=re.MULTILINE):
            files_modified.add(path.strip())
    for key in ("path", "file_path", "workdir"):
        value = args.get(key)
        if isinstance(value, str) and _looks_like_path(value):
            files_read.add(value)


def _extract_codex_external_tool_text(
    text: str,
    files_modified: set[str],
    files_read: set[str],
    commands_run: list[str],
    tool_context: list[str],
) -> None:
    """Handle imported Codex Desktop transcripts that encode tools as text."""
    if "[external_agent_tool_call:" not in text:
        return
    _append_unique(tool_context, _clip(text, 1200), max_items=80)
    command_match = re.search(r"command:\s*(.+?)(?:\n\[/external_agent_tool_call\]|\Z)", text, re.DOTALL)
    if command_match:
        commands_run.append(_clip(command_match.group(1), 200))
    for file_match in re.finditer(r"(?:file|path):\s*([^\n]+)", text):
        path = file_match.group(1).strip()
        if _looks_like_path(path):
            if "Write" in text or "Edit" in text:
                files_modified.add(path)
            else:
                files_read.add(path)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


# Chunk configuration
CHUNK_SIZE = 5  # messages per chunk
CHUNK_OVERLAP = 1  # overlapping messages between chunks
MAX_CHUNK_CHARS = 8000  # max chars per chunk (~2K tokens, well under embedder's 8K limit)


def _build_fts_text(
    user_messages: list[str],
    assistant_texts: list[str] | None = None,
    extra_texts: list[str] | None = None,
) -> str:
    """Build FTS-indexed text by sampling messages throughout the conversation.

    Interleaves user and assistant messages for keyword coverage.
    For short conversations (≤ 20 msgs): include everything.
    For longer ones: first 5 + sampled middle + last 5.
    Sampling prevents long sessions from dominating BM25 rankings
    for unrelated queries, while still covering keywords from any turn.
    """
    # Interleave user and assistant messages for better coverage
    all_messages: list[str] = []
    assistant = assistant_texts or []
    for i in range(max(len(user_messages), len(assistant))):
        if i < len(user_messages) and user_messages[i].strip():
            all_messages.append(user_messages[i])
        if i < len(assistant) and assistant[i].strip():
            all_messages.append(assistant[i])

    n = len(all_messages)
    if n <= 20:
        text = "\n".join(all_messages)
    else:
        sampled = []
        sampled.extend(all_messages[:5])
        middle = all_messages[5:-5]
        step = max(1, len(middle) // 30)
        sampled.extend(middle[::step])
        sampled.extend(all_messages[-5:])
        text = "\n".join(sampled)

    if extra_texts:
        text = "\n".join([text, *[t for t in extra_texts if t]]).strip()

    if len(text) > MAX_MESSAGES_TEXT:
        text = text[:MAX_MESSAGES_TEXT]
    return text


def _build_chunks(
    user_messages: list[str],
    assistant_texts: list[str],
    extra_texts: list[str] | None = None,
) -> list[str]:
    """Build conversation chunks for embedding.

    Interleaves user + assistant messages into turn pairs, then creates
    overlapping sliding windows. Research shows including assistant
    responses improves semantic retrieval (anchors what was discussed).

    Each chunk is a window of ~5 turn pairs (~512 tokens).
    """
    # Build interleaved turn pairs
    turns: list[str] = []
    for i in range(max(len(user_messages), len(assistant_texts))):
        parts = []
        if i < len(user_messages) and user_messages[i].strip():
            # Truncate individual messages to keep chunks balanced
            parts.append(f"User: {user_messages[i][:500]}")
        if i < len(assistant_texts) and assistant_texts[i].strip():
            parts.append(f"Assistant: {assistant_texts[i][:500]}")
        if parts:
            turns.append("\n".join(parts))

    if not turns:
        return [text[:MAX_CHUNK_CHARS] for text in (extra_texts or []) if text.strip()]

    # For very short sessions, one chunk is enough
    if len(turns) <= CHUNK_SIZE:
        text = "\n\n".join(turns)
        chunks = [text[:MAX_CHUNK_CHARS]]
        for extra in extra_texts or []:
            if extra.strip():
                chunks.append(("Session recall profile:\n" + extra)[:MAX_CHUNK_CHARS])
        return chunks

    chunks = []
    step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)

    for start in range(0, len(turns), step):
        end = min(start + CHUNK_SIZE, len(turns))
        window = turns[start:end]
        chunk_text = "\n\n".join(window)
        if len(chunk_text) > MAX_CHUNK_CHARS:
            chunk_text = chunk_text[:MAX_CHUNK_CHARS]
        if chunk_text.strip():
            chunks.append(chunk_text)

        if end >= len(turns):
            break

    for text in extra_texts or []:
        if text.strip():
            chunks.append(("Session recall profile:\n" + text)[:MAX_CHUNK_CHARS])

    return chunks


def load_sessions_index(project_dir: str, projects_dir: Path = PROJECTS_DIR) -> dict[str, dict]:
    """Load sessions-index.json for a project directory.

    Returns dict mapping session_id -> index entry.
    """
    idx_path = projects_dir / project_dir / "sessions-index.json"
    if not idx_path.exists():
        return {}

    try:
        with open(idx_path) as f:
            data = json.load(f)
            return {
                entry["sessionId"]: entry
                for entry in data.get("entries", [])
                if "sessionId" in entry
            }
    except (json.JSONDecodeError, OSError):
        return {}


def discover_sessions(projects_dir: Path = PROJECTS_DIR) -> list[dict]:
    """Walk the projects directory and discover all session .jsonl files.

    Returns list of dicts with:
        session_id, file_path, project_dir, is_subagent, parent_session,
        file_size, mtime
    """
    sessions = []

    if not projects_dir.exists():
        return sessions

    for root, dirs, files in os.walk(projects_dir):
        for filename in files:
            if not filename.endswith(".jsonl"):
                continue

            file_path = os.path.join(root, filename)
            session_id = filename[:-6]  # strip .jsonl

            # Determine project dir and subagent status
            rel = os.path.relpath(root, projects_dir)
            parts = rel.split(os.sep)
            project_dir = parts[0]

            is_subagent = "subagents" in rel
            parent_session = None
            if is_subagent and len(parts) >= 2:
                # Structure: project_dir/session_id/subagents/agent-xxx.jsonl
                parent_session = parts[1]

            try:
                stat = os.stat(file_path)
            except OSError:
                continue

            sessions.append({
                "session_id": session_id,
                "file_path": file_path,
                "project_dir": project_dir,
                "is_subagent": is_subagent,
                "parent_session": parent_session,
                "file_size": stat.st_size,
                "mtime": stat.st_mtime,
            })

    return sessions


def generate_summary(first_prompt: str | None, first_reply: str | None) -> str | None:
    """Generate a short summary from the first prompt and reply.

    Extracts the core intent by stripping boilerplate prefixes
    and combining with key context from the assistant's reply.
    Returns ~150 chars of dense, keyword-rich text for FTS ranking.
    """
    if not first_prompt:
        return None

    text = first_prompt.strip()

    # Strip common boilerplate prefixes from automated sessions
    _PREFIXES = [
        re.compile(r"^Fix issue #\d+:\s*\[[\w]+\]:\s*", re.IGNORECASE),
        re.compile(r"^Review this code change for issue #\d+:\s*\[[\w]+\]:\s*", re.IGNORECASE),
        re.compile(r"^Analyze this GitHub issue[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"^## Merge target:.*?\n", re.IGNORECASE),
        re.compile(r"^## Code Review.*?\n", re.IGNORECASE),
        re.compile(r"^Given the query and candidates in the input,.*", re.IGNORECASE),
    ]
    for pat in _PREFIXES:
        text = pat.sub("", text).strip()

    if not text:
        text = first_prompt.strip()

    # Take first meaningful sentence/line (up to 120 chars)
    # Split on sentence boundaries or newlines
    for sep in ["\n", ". ", "! ", "? "]:
        if sep in text[:150]:
            text = text[:text.index(sep, 0, 150)]
            break
    text = text[:120].strip()

    # Append context from first_reply if it adds new keywords
    if first_reply:
        reply_text = first_reply.strip()
        # Take first sentence of reply
        for sep in ["\n", ". ", "! ", "? "]:
            if sep in reply_text[:120]:
                reply_text = reply_text[:reply_text.index(sep, 0, 120)]
                break
        reply_text = reply_text[:80].strip()
        if reply_text and reply_text.lower() != text.lower():
            text = f"{text} — {reply_text}"

    return text[:200] if text else None


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def format_date(iso_date: str | None) -> str:
    """Format an ISO date string for display."""
    if not iso_date:
        return "unknown"
    return iso_date[:10]  # YYYY-MM-DD
