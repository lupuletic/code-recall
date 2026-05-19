"""Tests for code_recall.indexer."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_recall.db import get_all_session_ids, get_connection, get_session_mtime
from code_recall.indexer import build_index
from code_recall.searcher import search


class TestBuildIndex:
    def test_discovers_and_indexes_sessions(self, projects_dir, db_path):
        """build_index should discover and index all valid session files."""
        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            force=False,
            verbose=False,
        )
        assert stats["total_discovered"] == 4  # 3 main + 1 subagent
        assert stats["indexed"] >= 3  # at least the sessions with user messages
        assert stats["errors"] == 0

    def test_all_sessions_in_db(self, projects_dir, db_path):
        """After indexing, all sessions should be in the database."""
        build_index(projects_dir=projects_dir, db_path=db_path, verbose=False)

        conn = get_connection(db_path)
        ids = get_all_session_ids(conn)
        conn.close()

        assert "session-001" in ids
        assert "session-002" in ids
        assert "session-003" in ids
        assert "agent-sub1" in ids

    def test_incremental_skips_unchanged(self, projects_dir, db_path):
        """Second run should skip already-indexed, unchanged files."""
        stats1 = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        indexed_first = stats1["indexed"]

        stats2 = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        assert stats2["skipped"] >= indexed_first
        assert stats2["indexed"] == 0

    def test_force_reindex_processes_all(self, projects_dir, db_path):
        """Force reindex should process all files again."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            force=True,
            verbose=False,
        )
        # With force, all discovered sessions should be re-indexed
        assert stats["indexed"] >= 3

    def test_empty_sessions_skipped(self, projects_dir, db_path, tmp_path):
        """Sessions with no user messages should be skipped."""
        import json

        # Create a session file with only system messages
        proj = projects_dir / "-Users-test-Projects-empty"
        proj.mkdir()
        empty_session = json.dumps({
            "type": "system",
            "message": {"role": "system", "content": "init"},
        })
        (proj / "empty-session.jsonl").write_text(empty_session + "\n")

        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        # The empty session should be skipped
        conn = get_connection(db_path)
        ids = get_all_session_ids(conn)
        conn.close()
        assert "empty-session" not in ids

    def test_subagent_detection(self, projects_dir, db_path):
        """Subagent sessions should be marked as such."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 'agent-sub1'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["is_subagent"] == 1
        assert row["parent_session"] == "session-001"

    def test_project_path_decoded(self, projects_dir, db_path):
        """Project path should be decoded from sessions-index.json."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 'session-001'"
        ).fetchone()
        conn.close()

        # sessions-index.json has originalPath = /Users/test/Projects/myapp
        assert row["project_path"] == "/Users/test/Projects/myapp"

    def test_gc_removes_orphaned(self, projects_dir, db_path):
        """Removed session files should be cleaned up on re-index."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        # Delete a session file
        session_file = projects_dir / "-Users-test-Projects-webapp" / "session-003.jsonl"
        session_file.unlink()

        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        assert stats["removed"] >= 1

        conn = get_connection(db_path)
        ids = get_all_session_ids(conn)
        conn.close()
        assert "session-003" not in ids

    def test_modified_file_reindexed(self, projects_dir, db_path):
        """A file with changed mtime should be re-indexed."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        # Touch the file to update mtime
        import json
        import os

        session_file = projects_dir / "-Users-test-Projects-myapp" / "session-001.jsonl"
        # Append a new message
        with open(session_file, "a") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "new question added"},
            }) + "\n")

        # Ensure mtime is different (on some filesystems, 1s resolution)
        future_time = time.time() + 10
        os.utime(session_file, (future_time, future_time))

        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        assert stats["indexed"] >= 1

    def test_chunks_stored(self, projects_dir, db_path):
        """Chunks should be stored in the chunks table."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        conn = get_connection(db_path)
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()

        assert chunk_count > 0

    @patch("code_recall.indexer.has_semantic", return_value=False)
    def test_no_semantic_deps(self, mock_semantic, projects_dir, db_path):
        """Should work fine without semantic dependencies."""
        stats = build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )
        assert stats["indexed"] >= 3
        assert stats["embeddings"] == 0

    def test_sessions_index_metadata_used(self, projects_dir, db_path):
        """Metadata from sessions-index.json should be incorporated."""
        build_index(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=False,
        )

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 'session-001'"
        ).fetchone()
        conn.close()

        assert row["summary"] == "Auth middleware debugging"
        assert row["git_branch"] == "fix/auth"
        assert row["created"] == "2025-01-15T10:00:00Z"

    def test_read_files_are_indexed_for_file_search(self, tmp_path, db_path):
        """Files only inspected by Claude should still be searchable."""
        import json

        projects = tmp_path / "projects"
        proj = projects / "-Users-test-Projects-payments"
        proj.mkdir(parents=True)
        session = proj / "session-read.jsonl"
        session.write_text("\n".join([
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "debug checkout webhook"},
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/repo/src/webhook_handler.py"},
                        }
                    ],
                },
            }),
        ]))

        build_index(projects_dir=projects, db_path=db_path, verbose=False)

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT action FROM session_files WHERE file_path = ?",
            ("/repo/src/webhook_handler.py",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["action"] == "read"

    def test_indexes_codex_sessions_when_codex_dir_supplied(self, tmp_path, db_path):
        """Codex threads should be indexed alongside Claude sessions."""
        import json
        import sqlite3

        projects = tmp_path / "claude-projects"
        projects.mkdir()
        codex_dir = tmp_path / ".codex"
        session_dir = codex_dir / "sessions" / "2026" / "05" / "19"
        session_dir.mkdir(parents=True)
        rollout = session_dir / "rollout-2026-05-19T10-00-00-thread-1.jsonl"
        rollout.write_text("\n".join([
            json.dumps({
                "timestamp": "2026-05-19T10:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "fix codex auth flow"},
            }),
            json.dumps({
                "timestamp": "2026-05-19T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "The auth flow is fixed."},
            }),
        ]) + "\n")

        conn = sqlite3.connect(codex_dir / "state_5.sqlite")
        conn.execute(
            """CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                first_user_message TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                model TEXT,
                git_branch TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                source TEXT NOT NULL,
                thread_source TEXT,
                archived INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """INSERT INTO threads
               (id, rollout_path, cwd, title, first_user_message, model_provider, model,
                git_branch, created_at, updated_at, created_at_ms, updated_at_ms, source,
                thread_source, archived)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                "thread-1",
                str(rollout),
                "/repo/codex-app",
                "Fix Codex auth",
                "fix codex auth flow",
                "openai",
                "gpt-5.5",
                "main",
                1779184800,
                1779188400,
                1779184800000,
                1779188400000,
                "cli",
                "user",
            ),
        )
        conn.commit()
        conn.close()

        stats = build_index(projects_dir=projects, codex_dir=codex_dir, db_path=db_path, verbose=False)

        assert stats["indexed"] == 1
        conn = get_connection(db_path)
        row = conn.execute("SELECT * FROM sessions WHERE session_id = 'codex:thread-1'").fetchone()
        conn.close()

        assert row is not None
        assert row["provider"] == "codex"
        assert row["provider_session_id"] == "thread-1"
        assert row["project_path"] == "/repo/codex-app"
        assert row["summary"] == "Fix Codex auth"
        assert row["model"] == "gpt-5.5"

        results = search("codex auth flow", db_path=db_path, semantic=False)

        assert results
        assert results[0].session.provider == "codex"
        assert results[0].session.provider_session_id == "thread-1"
        assert results[0].resume_command == "codex resume thread-1"
