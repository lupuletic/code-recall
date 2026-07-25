"""Tests for code_recall.cli."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from unittest.mock import MagicMock, patch

import pytest

from code_recall import cli
from code_recall.cli import _sync_index_hook, main


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Isolate all CLI tests from real user data."""
    db_path = tmp_path / "test.db"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    config_path = tmp_path / "config.json"
    hooks_marker = tmp_path / ".hooks-installed"

    monkeypatch.setattr("code_recall.cli.DB_PATH", db_path)
    monkeypatch.setattr("code_recall.cli.PROJECTS_DIR", projects_dir)
    monkeypatch.setattr("code_recall.cli.HOOKS_MARKER", hooks_marker)
    monkeypatch.setattr("code_recall.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("code_recall.db.DB_PATH", db_path)
    monkeypatch.setattr("code_recall.utils.PROJECTS_DIR", projects_dir)

    return {
        "db_path": db_path,
        "projects_dir": projects_dir,
        "config_path": config_path,
    }


# ===========================================================================
# --version
# ===========================================================================

class TestVersion:
    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "code-recall" in out

    def test_command_entry_point_is_packaged(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]

        assert scripts["code-recall"] == "code_recall.cli:main"


# ===========================================================================
# index subcommand
# ===========================================================================

class TestIndexCommand:
    def test_index_runs(self, capsys, isolate_env):
        """'index' subcommand should run without error."""
        # Suppress ensure_models_downloaded
        with patch("code_recall.cli.build_index", return_value={
            "indexed": 0, "skipped": 0, "removed": 0, "errors": 0,
            "embeddings": 0, "elapsed": 0.1, "total_discovered": 0,
        }):
            main(["index", "--quiet"])

    def test_index_force(self, isolate_env):
        with patch("code_recall.cli.build_index", return_value={
            "indexed": 5, "skipped": 0, "removed": 0, "errors": 0,
            "embeddings": 0, "elapsed": 0.5, "total_discovered": 5,
        }) as mock_build:
            main(["index", "--force", "--quiet"])
            _, kwargs = mock_build.call_args
            assert kwargs.get("force") is True


# ===========================================================================
# update subcommand
# ===========================================================================

class TestUpdateCommand:
    @patch("code_recall.updater.run_update", return_value=0)
    def test_update_runs_without_confirmation(self, mock_run_update):
        main(["update"])

        mock_run_update.assert_called_once_with(yes=False, quiet=False)

    @patch("code_recall.updater.run_update", return_value=0)
    def test_update_yes_passes_confirmation(self, mock_run_update):
        main(["update", "--yes"])

        mock_run_update.assert_called_once_with(yes=True, quiet=False)

    @patch("code_recall.updater.run_update", return_value=1)
    def test_update_exits_on_failure(self, mock_run_update):
        with pytest.raises(SystemExit) as exc_info:
            main(["update", "--yes"])

        assert exc_info.value.code == 1


# ===========================================================================
# ask subcommand
# ===========================================================================

class TestAskCommand:
    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search")
    @patch("code_recall.agentic.answer_query")
    def test_ask_runs_agentic_answer(self, mock_answer, mock_search, mock_setup, capsys):
        from code_recall.agentic import AgenticAnswer, EvidenceSource
        from code_recall.models import SearchResult, Session

        result = SearchResult(
            session=Session(
                session_id="s1",
                project_path="/tmp/project",
                project_dir="project",
                file_path="/tmp/session.jsonl",
                summary="Auth work",
            ),
            score=0.9,
        )
        mock_search.return_value = [result]
        mock_answer.return_value = AgenticAnswer(
            ok=True,
            query="what happened",
            answer="Auth was fixed.",
            sources=[
                EvidenceSource(
                    rank=1,
                    session_id="s1",
                    title="Auth work",
                    project_path="/tmp/project",
                    activity="2026-05-13",
                    score=0.9,
                    resume_command="claude --resume s1",
                    file_path="/tmp/session.jsonl",
                )
            ],
        )

        main(["ask", "what", "happened", "--no-ai-tools"])

        assert "Auth was fixed." in capsys.readouterr().out
        mock_search.assert_called_once()
        mock_answer.assert_called_once()
        assert mock_answer.call_args.kwargs["query"] == "what happened"
        assert mock_answer.call_args.kwargs["use_read_tools"] is False

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    @patch("code_recall.agentic.answer_query")
    def test_ask_json_output(self, mock_answer, mock_search, mock_setup, capsys):
        from code_recall.agentic import AgenticAnswer

        mock_answer.return_value = AgenticAnswer(
            ok=False,
            query="missing",
            answer="No indexed sessions matched the question.",
            sources=[],
            error="no_results",
        )

        main(["ask", "missing", "--json"])

        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is False
        assert data["error"] == "no_results"


# ===========================================================================
# search (direct query routing)
# ===========================================================================

class TestSearchCommand:
    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_direct_query(self, mock_search, mock_setup, capsys, isolate_env):
        """Positional args without subcommand should route to search."""
        main(["auth", "middleware", "--no-tui"])
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs[1]["query"] == "auth middleware"

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_search_subcommand(self, mock_search, mock_setup, capsys, isolate_env):
        """'search' subcommand should strip the command and search."""
        main(["search", "debug", "auth", "--no-tui"])
        mock_search.assert_called_once()
        assert mock_search.call_args[1]["query"] == "debug auth"

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_s_alias(self, mock_search, mock_setup, capsys, isolate_env):
        """'s' should be an alias for 'search'."""
        main(["s", "test", "query", "--no-tui"])
        mock_search.assert_called_once()
        assert mock_search.call_args[1]["query"] == "test query"


# ===========================================================================
# --json output
# ===========================================================================

class TestJsonOutput:
    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search")
    def test_json_output_valid(self, mock_search, mock_setup, capsys, isolate_env):
        """--json should output valid JSON."""
        from code_recall.models import SearchResult, Session

        mock_search.return_value = [
            SearchResult(
                session=Session(
                    session_id="test1",
                    project_path="/test",
                    project_dir="test",
                    file_path="/tmp/test.jsonl",
                    summary="Test session",
                    first_prompt="hello",
                    message_count=3,
                    file_size=100,
                    modified="2025-01-01T00:00:00Z",
                ),
                score=0.95,
                snippets=["test snippet"],
            )
        ]
        main(["auth", "--json"])
        output = capsys.readouterr().out
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["session_id"] == "test1"
        assert data[0]["score"] == 0.95

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_json_empty_results(self, mock_search, mock_setup, capsys, isolate_env):
        main(["nonsense", "--json"])
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data == []


# ===========================================================================
# --no-tui
# ===========================================================================

class TestNoTui:
    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_no_tui_flag(self, mock_search, mock_setup, capsys, isolate_env):
        """--no-tui should output plain text."""
        main(["test", "query", "--no-tui"])
        out = capsys.readouterr().out
        assert "No sessions found" in out


# ===========================================================================
# filters passed through
# ===========================================================================

class TestFilterPassthrough:
    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_project_filter(self, mock_search, mock_setup, isolate_env):
        main(["test", "--project", "myapp", "--no-tui"])
        assert mock_search.call_args[1]["project_filter"] == "myapp"

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_after_filter(self, mock_search, mock_setup, isolate_env):
        main(["test", "--after", "2025-01-01", "--no-tui"])
        assert mock_search.call_args[1]["after"] == "2025-01-01"

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_before_filter(self, mock_search, mock_setup, isolate_env):
        main(["test", "--before", "2025-12-31", "--no-tui"])
        assert mock_search.call_args[1]["before"] == "2025-12-31"

    @patch("code_recall.cli._first_run_setup")
    @patch("code_recall.cli.search", return_value=[])
    def test_limit(self, mock_search, mock_setup, isolate_env):
        main(["test", "-n", "5", "--no-tui"])
        assert mock_search.call_args[1]["limit"] == 5


# ===========================================================================
# info subcommand
# ===========================================================================

class TestInfoCommand:
    def test_info_no_db(self, capsys, isolate_env):
        """Info with no DB should print a helpful message."""
        main(["info"])
        out = capsys.readouterr().out
        assert "No index found" in out

    def test_info_with_db(self, capsys, isolate_env):
        """Info with a DB should show stats."""
        from code_recall.db import get_connection, upsert_session
        from code_recall.models import Session

        db_path = isolate_env["db_path"]
        conn = get_connection(db_path)
        s = Session(
            session_id="info1",
            project_path="/test",
            project_dir="test",
            file_path="/tmp/info1.jsonl",
            message_count=5,
            file_size=1024,
            created="2025-01-01T00:00:00Z",
            modified="2025-01-01T01:00:00Z",
        )
        upsert_session(conn, s)
        conn.commit()
        conn.close()

        main(["info"])
        out = capsys.readouterr().out
        assert "Sessions:" in out
        assert "1" in out


# ===========================================================================
# config subcommand
# ===========================================================================

class TestConfigCommand:
    def test_config_view(self, capsys, isolate_env):
        main(["config"])
        out = capsys.readouterr().out
        assert "search_mode" in out

    def test_config_set(self, capsys, isolate_env):
        main(["config", "limit", "25"])
        out = capsys.readouterr().out
        assert "25" in out

    def test_config_set_invalid(self, capsys, isolate_env):
        with pytest.raises(SystemExit):
            main(["config", "search_mode", "bogus"])


# ===========================================================================
# SessionEnd hook sync
# ===========================================================================

class TestSyncIndexHook:
    BIN = "/opt/tools/bin/code-recall"

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        """Fake $HOME with a .claude dir, plus a resolvable binary."""
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr("shutil.which", lambda _: self.BIN)
        return fake_home

    @staticmethod
    def _settings(home):
        return home / ".claude" / "settings.json"

    @staticmethod
    def _write(path, data):
        path.write_text(json.dumps(data))

    def _hook_of(self, home):
        settings = json.loads(self._settings(home).read_text())
        return settings["hooks"]["SessionEnd"][0]["hooks"][0]

    def _legacy_settings(self, home, command):
        self._write(
            self._settings(home),
            {
                "model": "opus",
                "hooks": {
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ]
                },
            },
        )

    @pytest.mark.parametrize("legacy", ["claude-recall", "claude-code-recall"])
    def test_repairs_legacy_command_when_not_first_run(self, home, legacy):
        """Regression: a renamed binary left the hook permanently broken.

        The repair must happen on an ordinary run, because a hook pointing at a
        missing command never executes and so can never fix itself.
        """
        self._legacy_settings(home, f"/old/venv/bin/{legacy} index --quiet")

        status, _ = _sync_index_hook(install_if_missing=False)

        assert status == "repaired"
        assert self._hook_of(home)["command"] == f"{self.BIN} index --quiet"

    def test_repair_preserves_unrelated_settings(self, home):
        self._legacy_settings(home, "/old/venv/bin/claude-recall index --quiet")

        _sync_index_hook(install_if_missing=False)

        assert json.loads(self._settings(home).read_text())["model"] == "opus"

    def test_repair_applies_full_desired_config(self, home):
        self._legacy_settings(home, "/old/venv/bin/claude-recall index --quiet")

        _sync_index_hook(install_if_missing=False)

        hook = self._hook_of(home)
        assert hook["timeout"] == 30
        assert hook["async"] is True

    def test_is_idempotent(self, home):
        self._legacy_settings(home, "/old/venv/bin/claude-recall index --quiet")
        assert _sync_index_hook(install_if_missing=False)[0] == "repaired"

        assert _sync_index_hook(install_if_missing=False)[0] == "unchanged"

    def test_keeps_extra_user_keys(self, home):
        self._write(
            self._settings(home),
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/old/bin/claude-recall index",
                                    "statusMessage": "indexing",
                                }
                            ]
                        }
                    ]
                }
            },
        )

        _sync_index_hook(install_if_missing=False)

        assert self._hook_of(home)["statusMessage"] == "indexing"

    def test_does_not_reinstall_a_hook_the_user_removed(self, home):
        self._write(self._settings(home), {"model": "opus"})

        status, _ = _sync_index_hook(install_if_missing=False)

        assert status == "unchanged"
        assert "hooks" not in json.loads(self._settings(home).read_text())

    def test_installs_when_requested(self, home):
        self._write(self._settings(home), {"model": "opus"})

        status, _ = _sync_index_hook(install_if_missing=True)

        assert status == "installed"
        assert self._hook_of(home)["command"] == f"{self.BIN} index --quiet"

    def test_unreadable_settings_are_left_untouched(self, home):
        """A parse failure must not turn into a rewrite of the whole file."""
        self._settings(home).write_text("{ this is not json")

        status, _ = _sync_index_hook(install_if_missing=True)

        assert status == "skipped"
        assert self._settings(home).read_text() == "{ this is not json"

    def test_skips_when_binary_missing(self, home, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        self._write(self._settings(home), {"model": "opus"})

        status, _ = _sync_index_hook(install_if_missing=True)

        assert status == "skipped"
        assert "hooks" not in json.loads(self._settings(home).read_text())

    @patch("code_recall.cli.search", return_value=[])
    @patch("code_recall.cli.ensure_index")
    def test_ordinary_run_repairs_stale_hook(
        self, mock_index, mock_search, home, isolate_env
    ):
        """The established-install path must actually reach the repair.

        This is the real regression: hook sync used to be gated behind
        "the index does not exist yet", so anyone past their first run kept a
        broken hook forever. Exercised through main() because the bug was in
        the gate, not in the sync itself.
        """
        isolate_env["db_path"].touch()  # an existing install, not a first run
        cli.HOOKS_MARKER.touch()  # and one that already installed hooks once
        self._legacy_settings(home, "/old/venv/bin/claude-recall index --quiet")

        main(["some", "query", "--no-tui"])

        assert self._hook_of(home)["command"] == f"{self.BIN} index --quiet"
