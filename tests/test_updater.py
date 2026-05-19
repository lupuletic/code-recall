"""Tests for code_recall.updater."""

from __future__ import annotations

from types import SimpleNamespace

from code_recall import updater


def test_is_newer_compares_dotted_versions():
    assert updater._is_newer("0.1.3", "0.1.2") is True
    assert updater._is_newer("0.2.0", "0.1.9") is True
    assert updater._is_newer("0.1.2", "0.1.2") is False
    assert updater._is_newer("0.1.1", "0.1.2") is False
    assert updater._is_newer("0.1.3rc1", "0.1.2") is False


def test_format_command_quotes_shell_arguments():
    command = ["python", "-m", "pip", "install", "--upgrade", "code-recall[all]"]

    assert updater.format_command(command) == (
        "python -m pip install --upgrade 'code-recall[all]'"
    )


def test_detect_update_command_prefers_current_uv_tool(monkeypatch):
    def fake_which(name):
        return f"/bin/{name}" if name == "uv" else None

    def fake_run(command, **kwargs):
        assert command == ["uv", "tool", "list"]
        return SimpleNamespace(returncode=0, stdout="code-recall v0.1.4\n")

    monkeypatch.setattr(updater.shutil, "which", fake_which)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    detected = updater.detect_update_command()

    assert detected.method == "uv tool"
    assert detected.command == ["uv", "tool", "upgrade", "code-recall"]


def test_detect_update_command_migrates_legacy_uv_tool(monkeypatch):
    def fake_which(name):
        return f"/bin/{name}" if name == "uv" else None

    def fake_run(command, **kwargs):
        assert command == ["uv", "tool", "list"]
        return SimpleNamespace(returncode=0, stdout="claude-code-recall v0.1.8\n")

    monkeypatch.setattr(updater.shutil, "which", fake_which)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    detected = updater.detect_update_command()

    assert detected.method == "uv tool"
    assert detected.command == [
        "uv", "tool", "install", "--force", "code-recall",
        "--with", "textual", "--with", "fastembed", "--with", "sqlite-vec",
    ]


def test_detect_update_command_uses_pipx_when_uv_tool_absent(monkeypatch):
    def fake_which(name):
        return f"/bin/{name}" if name == "pipx" else None

    def fake_run(command, **kwargs):
        assert command == ["pipx", "list", "--json"]
        return SimpleNamespace(
            returncode=0,
            stdout='{"venvs": {"code-recall": {"metadata": {}}}}',
        )

    monkeypatch.setattr(updater.shutil, "which", fake_which)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    detected = updater.detect_update_command()

    assert detected.method == "pipx"
    assert detected.command == ["pipx", "upgrade", "code-recall"]


def test_detect_update_command_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)

    detected = updater.detect_update_command()

    assert detected.method == "pip"
    assert detected.command[-2:] == ["--upgrade", "code-recall[all]"]


def test_run_update_reports_already_current(monkeypatch, capsys):
    monkeypatch.setattr(updater, "get_latest_version", lambda timeout=10: "0.1.3")
    monkeypatch.setattr(updater, "__version__", "0.1.3")

    assert updater.run_update() == 0
    assert "already up to date" in capsys.readouterr().out


def test_run_update_prints_command_without_yes(monkeypatch, capsys):
    monkeypatch.setattr(updater, "get_latest_version", lambda timeout=10: "0.1.4")
    monkeypatch.setattr(updater, "__version__", "0.1.3")
    monkeypatch.setattr(
        updater,
        "detect_update_command",
        lambda: updater.UpdateCommand(["uv", "tool", "upgrade", "code-recall"], "uv tool"),
    )

    assert updater.run_update(yes=False) == 0

    out = capsys.readouterr().out
    assert "Current version: 0.1.3" in out
    assert "Latest version:  0.1.4" in out
    assert "uv tool upgrade code-recall" in out
    assert "Run with --yes" in out


def test_run_update_executes_with_yes(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "get_latest_version", lambda timeout=10: "0.1.4")
    monkeypatch.setattr(updater, "__version__", "0.1.3")
    monkeypatch.setattr(
        updater,
        "detect_update_command",
        lambda: updater.UpdateCommand(["uv", "tool", "upgrade", "code-recall"], "uv tool"),
    )
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda command: calls.append(command) or SimpleNamespace(returncode=0),
    )

    assert updater.run_update(yes=True, quiet=True) == 0
    assert calls == [["uv", "tool", "upgrade", "code-recall"]]
