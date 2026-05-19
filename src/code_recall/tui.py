"""Interactive TUI for code-recall using Textual."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Iterable

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    OptionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from code_recall import __version__
from code_recall.models import SearchResult
from code_recall.utils import clean_display_text, format_date, format_size


@dataclass(frozen=True)
class ProviderDisplay:
    """Display metadata for an indexed coding-agent provider."""

    id: str
    label: str
    short_label: str
    style: str
    resume_binary: str
    resume_args: tuple[str, ...]
    transcript_label: str
    capabilities: tuple[str, ...]

    def resume_command(self, session_id: str) -> str:
        return " ".join((self.resume_binary, *self.resume_args, session_id))


PROVIDERS = {
    "claude": ProviderDisplay(
        id="claude",
        label="Claude Code",
        short_label="Claude",
        style="cyan",
        resume_binary="claude",
        resume_args=("--resume",),
        transcript_label="Claude transcript",
        capabilities=("resume", "model", "branch", "files", "commands", "transcript"),
    ),
    "codex": ProviderDisplay(
        id="codex",
        label="Codex",
        short_label="Codex",
        style="magenta",
        resume_binary="codex",
        resume_args=("resume",),
        transcript_label="Codex rollout",
        capabilities=("resume", "model", "branch", "files", "commands", "transcript"),
    ),
}

DETAIL_TABS = ("overview", "why", "activity", "related", "ai")


def provider_display(provider: str | None) -> ProviderDisplay:
    """Return display metadata for a provider id."""
    if provider in PROVIDERS:
        return PROVIDERS[provider]
    provider_id = provider or "unknown"
    label = provider_id.replace("_", " ").title()
    return ProviderDisplay(
        id=provider_id,
        label=label,
        short_label=label,
        style="yellow",
        resume_binary=provider_id,
        resume_args=("resume",),
        transcript_label=f"{label} transcript",
        capabilities=("transcript",),
    )


def _session_title(result: SearchResult, max_len: int = 90) -> str:
    session = result.session
    title = clean_display_text(session.summary) or clean_display_text(session.first_prompt) or "Untitled session"
    if len(title) > max_len:
        return title[: max_len - 3].rstrip() + "..."
    return title


def _shorten(text: str | None, max_len: int = 120) -> str:
    cleaned = clean_display_text(text) or ""
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _activity(result: SearchResult) -> str:
    value = result.session.last_activity or result.session.modified or result.session.created
    return format_date(value) if value else "unknown"


def _provider_badge(provider: str) -> str:
    display = provider_display(provider)
    return f"[{display.style}]{display.short_label}[/{display.style}]"


def _assistant_label_for_session(provider: str | None) -> str:
    """Best-effort label for the assistant backend that will answer."""
    if provider and shutil.which(provider):
        return provider_display(provider).label
    for candidate in ("claude", "codex"):
        if shutil.which(candidate):
            return provider_display(candidate).label
    return "Unavailable"


def _score_label(score: float) -> str:
    if score >= 0.85:
        return "strong"
    if score >= 0.55:
        return "good"
    if score >= 0.25:
        return "possible"
    return "weak"


def _match_reason(result: SearchResult, query: str = "") -> str:
    """Return the most useful short reason for why a result appears."""
    normalized = query.strip().lower()
    snippets = [clean_display_text(snippet) for snippet in result.snippets if clean_display_text(snippet)]
    session = result.session
    files = _json_list(session.files_modified)
    commands = _json_list(session.commands_run)

    if normalized.startswith("file:"):
        wanted = normalized.split(":", 1)[1].split()[0]
        match = next((path for path in files if wanted in path.lower()), None)
        return f"file match: {match or wanted}"
    if normalized.startswith("cmd:"):
        wanted = normalized.split(":", 1)[1].split()[0]
        match = next((cmd for cmd in commands if wanted in cmd.lower()), None)
        return f"command match: {match or wanted}"
    if normalized.startswith("branch:"):
        return f"branch match: {session.git_branch or session.git_branch_detected or normalized.split(':', 1)[1]}"

    if snippets:
        return f"matched text: {_shorten(snippets[0], 90)}"
    if result.fts_rank is not None and result.vec_score is not None:
        return "hybrid keyword + semantic match"
    if result.fts_rank is not None:
        return "keyword match"
    if result.vec_score is not None:
        return "semantic match"
    return f"{_score_label(result.score)} relevance"


def _provider_counts(results: list[SearchResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        provider = result.session.provider or "unknown"
        counts[provider] = counts.get(provider, 0) + 1
    return counts


class FilterBar(Static):
    """Compact active filter summary."""

    def update_filters(self, provider_scope: str, search_mode: str, results: list[SearchResult]) -> None:
        provider = "All providers" if provider_scope == "all" else provider_display(provider_scope).label
        counts = _provider_counts(results)
        provider_bits = []
        for provider_id, count in sorted(counts.items()):
            display = provider_display(provider_id)
            provider_bits.append(f"[{display.style}]{display.short_label} {count}[/{display.style}]")
        provider_text = " ".join(provider_bits) if provider_bits else "No providers in result set"
        self.update(
            f"[bold]Scope:[/bold] {provider}   "
            f"[bold]Mode:[/bold] {search_mode}   "
            f"[bold]Visible:[/bold] {provider_text}   "
            "[dim]p provider  f filters  ? help  Ctrl+K commands[/dim]"
        )


class SessionItem(ListItem):
    """A scannable search result row."""

    def __init__(self, result: SearchResult, rank: int, query: str = "") -> None:
        super().__init__()
        self.result = result
        self.rank = rank
        self.query = query

    def compose(self) -> ComposeResult:
        session = self.result.session
        display = provider_display(session.provider)
        title = escape(_session_title(self.result, 92))
        project = escape(self.result.display_project)
        branch = escape(session.git_branch or session.git_branch_detected or "no branch")
        model = f" · {escape(session.model)}" if session.model else ""
        activity = _activity(self.result)
        reason = escape(_match_reason(self.result, self.query))
        score = f"{self.result.score:.0%}"
        file_count = len(_json_list(session.files_modified))
        cmd_count = len(_json_list(session.commands_run))

        meta = (
            f"[dim]{project} · {branch} · {activity} · "
            f"{session.message_count} msgs · {format_size(session.file_size) if session.file_size else 'unknown size'}"
            f"{model}[/dim]"
        )
        telemetry = f"[dim]{file_count} files · {cmd_count} cmds[/dim]" if file_count or cmd_count else "[dim]no files/cmds[/dim]"

        yield Static(
            "\n".join(
                [
                    f"[bold]{self.rank:>2}[/bold] {_provider_badge(session.provider)} [bold]{title}[/bold]",
                    f"   [{display.style}]{_score_label(self.result.score)} {score}[/{display.style}]  {meta}",
                    f"   [green]why:[/green] {reason}   {telemetry}",
                ]
            ),
            markup=True,
        )


class DetailPanel(VerticalScroll):
    """Selected-session detail panel with task-based tabs."""

    active_tab = "overview"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.result: SearchResult | None = None
        self.query_text = ""
        self.db_path = None
        self.ai_content: str | None = None
        self.ai_state = "idle"
        self.ai_assistant_label = "Auto"

    def set_result(self, result: SearchResult | None, query: str = "", db_path=None) -> None:
        self.result = result
        self.query_text = query
        self.db_path = db_path
        self.refresh_content()

    def set_tab(self, tab: str) -> None:
        if tab in DETAIL_TABS:
            self.active_tab = tab
            self.refresh_content()

    def set_ai_loading(self, query: str, assistant_label: str = "Auto") -> None:
        self.active_tab = "ai"
        self.ai_state = "loading"
        self.ai_assistant_label = assistant_label
        self.ai_content = (
            f"[bold]AI investigation[/bold]\n"
            f"[dim]Assistant provider:[/dim] {escape(assistant_label)}\n"
            f"[dim]Question:[/dim] {escape(query)}\n\n"
            "[dim]Reading indexed evidence and source transcripts...[/dim]"
        )
        self.refresh_content()

    def set_ai_answer(self, content: str, ok: bool = True, assistant_label: str = "Auto") -> None:
        self.active_tab = "ai"
        self.ai_state = "ready" if ok else "error"
        self.ai_assistant_label = assistant_label
        self.ai_content = content
        self.refresh_content()

    def set_ai_chat(
        self,
        messages: list[tuple[str, str]],
        busy: bool = False,
        assistant_label: str = "Auto",
    ) -> None:
        self.active_tab = "ai"
        self.ai_state = "loading" if busy else "ready"
        self.ai_assistant_label = assistant_label
        self.ai_content = self._format_ai_chat(messages, busy)
        self.refresh_content()

    def render_empty(self, message: str) -> None:
        self.remove_children()
        self.mount(Static(message, markup=True))
        self.scroll_home(animate=False)

    def refresh_content(self) -> None:
        if self.result is None:
            self.render_empty(
                "[bold]No session selected[/bold]\n\n"
                "[dim]Search or browse recent sessions, then select a result to see why it matched.[/dim]\n\n"
                "[bold]Provider scope[/bold]\n"
                "Use [cyan]p[/cyan] to cycle All / Claude / Codex.\n\n"
                "[bold]Actions[/bold]\n"
                "[cyan]r[/cyan] resume  [cyan]c[/cyan] copy command  [cyan]Ctrl+A[/cyan] ask AI  [cyan]?[/cyan] help"
            )
            return

        self.remove_children()
        self.mount(Static(self._content(), markup=True))
        self.scroll_home(animate=False)

    def _tab_line(self) -> str:
        labels = []
        for index, tab in enumerate(DETAIL_TABS, 1):
            label = tab.title()
            labels.append(f"[reverse]{index} {label}[/reverse]" if tab == self.active_tab else f"{index} {label}")
        return "   ".join(labels)

    def _content(self) -> str:
        result = self.result
        assert result is not None
        session = result.session
        display = provider_display(session.provider)
        title = escape(_session_title(result, 120))
        header = [
            f"[bold]{title}[/bold]",
            (
                f"Session: [{display.style}]{display.label}[/{display.style}]"
                f" · Project: [cyan]{escape(result.display_project)}[/cyan]"
                f" · Activity: {_activity(result)}"
            ),
            self._tab_line(),
            "",
        ]

        if self.active_tab == "overview":
            body = self._overview(result)
        elif self.active_tab == "why":
            body = self._why(result)
        elif self.active_tab == "activity":
            body = self._activity_detail(result)
        elif self.active_tab == "related":
            body = self._related(result)
        else:
            body = self._ai(result)

        return "\n".join(header + body)

    def _overview(self, result: SearchResult) -> list[str]:
        session = result.session
        display = provider_display(session.provider)
        fields = [
            f"[bold]Score:[/bold] {_score_label(result.score)} ({result.score:.0%})",
            f"[bold]Resume:[/bold] [green]{escape(result.resume_command)}[/green]",
            f"[bold]Provider capabilities:[/bold] {', '.join(display.capabilities)}",
            f"[bold]Model:[/bold] {escape(session.model or 'unknown')}",
            f"[bold]Branch:[/bold] {escape(session.git_branch or session.git_branch_detected or 'unknown')}",
            f"[bold]Messages:[/bold] {session.message_count}",
            f"[bold]Source:[/bold] {escape(display.transcript_label)} · {escape(session.file_path)}",
        ]
        if session.first_prompt:
            fields.extend(["", "[bold]Started[/bold]", f"[dim]{escape(_shorten(session.first_prompt, 260))}[/dim]"])
        if session.last_prompt and session.last_prompt != session.first_prompt:
            fields.extend(["", "[bold]Left off[/bold]", f"[dim]{escape(_shorten(session.last_prompt, 260))}[/dim]"])
        return fields

    def _why(self, result: SearchResult) -> list[str]:
        lines = [
            "[bold]Why this result matched[/bold]",
            f"[green]Primary reason:[/green] {escape(_match_reason(result, self.query_text))}",
        ]
        if result.fts_rank is not None:
            lines.append(f"[cyan]Keyword signal:[/cyan] rank {result.fts_rank:.3f}")
        if result.vec_score is not None:
            lines.append(f"[cyan]Semantic signal:[/cyan] distance/score {result.vec_score:.3f}")
        snippets = [_shorten(snippet, 260) for snippet in result.snippets if _shorten(snippet, 260)]
        if snippets:
            lines.extend(["", "[bold]Matched evidence[/bold]"])
            for snippet in snippets[:5]:
                lines.append(f"- [dim]{escape(snippet)}[/dim]")
        else:
            lines.extend(["", "[dim]No textual snippets were returned for this result.[/dim]"])
        return lines

    def _activity_detail(self, result: SearchResult) -> list[str]:
        session = result.session
        files = _json_list(session.files_modified)
        commands = _json_list(session.commands_run)
        lines = [
            "[bold]Files touched[/bold]",
            *([f"- [green]{escape(path)}[/green]" for path in files[:20]] or ["[dim]No files recorded[/dim]"]),
        ]
        if len(files) > 20:
            lines.append(f"[dim]+{len(files) - 20} more files[/dim]")
        lines.extend(["", "[bold]Commands run[/bold]"])
        lines.extend([f"- [yellow]{escape(cmd)}[/yellow]" for cmd in commands[:20]] or ["[dim]No commands recorded[/dim]"])
        if len(commands) > 20:
            lines.append(f"[dim]+{len(commands) - 20} more commands[/dim]")
        return lines

    def _related(self, result: SearchResult) -> list[str]:
        try:
            from code_recall.db import DB_PATH, get_connection, get_related_sessions

            conn = get_connection(self.db_path or DB_PATH)
            related = get_related_sessions(conn, result.session.session_id, limit=8)
            conn.close()
        except Exception as exc:
            return ["[bold]Related sessions[/bold]", f"[dim]Unavailable: {escape(str(exc))}[/dim]"]

        lines = ["[bold]Related sessions[/bold]"]
        if not related:
            lines.append("[dim]No related sessions found.[/dim]")
            return lines
        for item in related:
            name = escape(_shorten(str(item["summary"] or "Untitled"), 80))
            lines.append(f"- [cyan]{name}[/cyan] [dim]shared files: {item['shared_files']}[/dim]")
        return lines

    def _ai(self, result: SearchResult) -> list[str]:
        if self.ai_content:
            return [self.ai_content]
        return [
            "[bold]Transcript chat[/bold]",
            "[dim]Ask a question about this selected session in the prompt below.[/dim]",
            "",
            f"[bold]Session provider:[/bold] {provider_display(result.session.provider).label}",
            f"[bold]Assistant provider:[/bold] {_assistant_label_for_session(result.session.provider)}",
            "",
            "[dim]The chat is scoped to this transcript, with read-only access to the source file when available.[/dim]",
        ]

    def _format_ai_chat(self, messages: list[tuple[str, str]], busy: bool) -> str:
        lines = [
            "[bold]Transcript chat[/bold]",
            f"[dim]Assistant provider:[/dim] {escape(self.ai_assistant_label)}",
            "",
        ]
        if not messages:
            lines.extend([
                "[dim]Ask about decisions, files touched, commands run, outcomes, or what to resume next.[/dim]",
                "[dim]This chat is scoped to the selected session transcript.[/dim]",
            ])
            return "\n".join(lines)

        for role, text in messages[-8:]:
            if role == "user":
                lines.append(f"[cyan]You:[/cyan] {escape(text)}")
            else:
                lines.append(f"[green]{escape(self.ai_assistant_label)}:[/green] {escape(text)}")
            lines.append("")
        if busy:
            lines.append("[dim]Reading transcript evidence...[/dim]")
        return "\n".join(lines).rstrip()


class SettingsScreen(ModalScreen):
    """Settings modal overlay with arrow-key navigation."""

    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        width: 82;
        max-height: 90%;
        border: tall $primary;
        background: $surface;
        padding: 1 2;
    }
    #settings-title {
        text-align: center;
        text-style: bold;
        padding: 0 0 1 0;
    }
    .section-label {
        text-style: bold;
        padding: 1 0 0 0;
    }
    #mode-list {
        height: 8;
        margin: 0 0 0 2;
        border: tall $border-blurred;
    }
    #mode-list:focus {
        border: tall $border;
    }
    .setting-row,
    .toggle-row {
        height: 3;
    }
    .setting-key {
        width: 30;
        padding: 1 1 0 0;
    }
    .setting-value {
        width: 1fr;
    }
    .toggle-label {
        width: 1fr;
        padding: 1 0 0 0;
    }
    #settings-buttons {
        height: 3;
        align: center middle;
        padding: 1 0 0 0;
    }
    #settings-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def compose(self) -> ComposeResult:
        from code_recall.config import SEARCH_MODES, load_config

        self._config = load_config()
        self._mode_keys = list(SEARCH_MODES.keys())

        with VerticalScroll(id="settings-dialog"):
            yield Static("Settings", id="settings-title")
            yield Static("Search Mode", classes="section-label")
            options = []
            for mode, desc in SEARCH_MODES.items():
                options.append(Option(f"{mode}  -  {desc}"))
            yield OptionList(*options, id="mode-list")

            yield Static("Search Options", classes="section-label")
            with Horizontal(classes="setting-row"):
                yield Static("Results Limit", classes="setting-key")
                yield Input(value=str(self._config["limit"]), type="integer", id="limit-input", classes="setting-value")
            with Horizontal(classes="setting-row"):
                yield Static("Relevance Cutoff (0.0-1.0)", classes="setting-key")
                yield Input(value=str(self._config["relevance_cutoff"]), id="cutoff-input", classes="setting-value")

            yield Static("Toggles", classes="section-label")
            with Horizontal(classes="toggle-row"):
                yield Static("Show subagent sessions", classes="toggle-label")
                yield Switch(value=self._config["show_subagents"], id="switch-subagents")
            with Horizontal(classes="toggle-row"):
                yield Static("Auto-install SessionEnd hook", classes="toggle-label")
                yield Switch(value=self._config["auto_index_hook"], id="switch-hook")
            with Horizontal(classes="toggle-row"):
                yield Static("Auto-generate AI summaries", classes="toggle-label")
                yield Switch(value=self._config["auto_ai_summary"], id="switch-ai-summary")

            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def on_mount(self) -> None:
        option_list = self.query_one("#mode-list", OptionList)
        try:
            option_list.highlighted = self._mode_keys.index(self._config["search_mode"])
        except ValueError:
            option_list.highlighted = 0
        option_list.focus()

    @on(Button.Pressed, "#save-btn")
    def on_save(self, event: Button.Pressed) -> None:
        self._save_settings()

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _save_settings(self) -> None:
        from code_recall.config import save_config

        option_list = self.query_one("#mode-list", OptionList)
        if option_list.highlighted is not None and option_list.highlighted < len(self._mode_keys):
            self._config["search_mode"] = self._mode_keys[option_list.highlighted]

        try:
            self._config["limit"] = int(self.query_one("#limit-input", Input).value)
        except ValueError:
            pass
        try:
            self._config["relevance_cutoff"] = float(self.query_one("#cutoff-input", Input).value)
        except ValueError:
            pass

        self._config["show_subagents"] = self.query_one("#switch-subagents", Switch).value
        self._config["auto_index_hook"] = self.query_one("#switch-hook", Switch).value
        self._config["auto_ai_summary"] = self.query_one("#switch-ai-summary", Switch).value

        save_config(self._config)
        self.dismiss(True)


class HelpScreen(ModalScreen):
    """Keyboard help and interaction model."""

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 86;
        max-height: 90%;
        border: tall $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Close", show=True)]

    def compose(self) -> ComposeResult:
        yield Static(
            "\n".join(
                [
                    "[bold]code-recall keyboard model[/bold]",
                    "",
                    "[cyan]/[/cyan] search   [cyan]Tab[/cyan] next pane   [cyan]Shift+Tab[/cyan] previous pane",
                    "[cyan]Up/Down[/cyan] move results   [cyan]Right[/cyan] focus detail   [cyan]Left[/cyan] focus results",
                    "[cyan]1-5[/cyan] detail tabs   [cyan]p[/cyan] provider scope   [cyan]f[/cyan] filters/settings",
                    "[cyan]r[/cyan] resume   [cyan]c[/cyan] copy resume command   [cyan]o[/cyan] open project",
                    "[cyan]Ctrl+A[/cyan] ask AI   [cyan]Ctrl+K[/cyan] command palette   [cyan]Esc[/cyan] close/quit",
                    "",
                    "[bold]Provider model[/bold]",
                    "The session provider is shown separately from the AI assistant provider.",
                    "Claude Code and Codex have their own labels, counts, and resume commands.",
                ]
            ),
            id="help-dialog",
            markup=True,
        )

    def action_dismiss(self) -> None:
        self.dismiss()


class RecallApp(App):
    """code-recall interactive session search."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        dock: top;
        height: 7;
        padding: 0 1;
        border-bottom: tall $primary;
    }
    #summary {
        height: 1;
        color: $text-muted;
    }
    #search-input {
        height: 3;
        width: 1fr;
    }
    #filter-bar {
        height: 2;
        color: $text-muted;
    }
    #main {
        height: 1fr;
    }
    #results-column {
        width: 58%;
        min-width: 48;
        border-right: tall $primary;
    }
    #results-meta {
        height: 2;
        padding: 0 1;
        color: $text-muted;
    }
    #results {
        height: 1fr;
    }
    #detail-column {
        width: 42%;
        min-width: 42;
    }
    #detail {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    #ai-chat-input {
        display: none;
        height: 3;
        margin: 0 1 1 1;
    }
    #ai-chat-input.visible {
        display: block;
    }
    #status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SessionItem {
        padding: 0 1;
        height: auto;
        margin: 0 0 1 0;
    }
    SessionItem:hover {
        background: $surface-lighten-1;
    }
    SessionItem.-highlight {
        background: $primary-darken-2;
    }
    .loading-results {
        opacity: 0.45;
    }
    Screen.narrow #results-column {
        width: 1fr;
        min-width: 0;
        border-right: none;
    }
    Screen.narrow #detail-column {
        display: none;
    }
    Screen.narrow #detail-column.detail-visible {
        display: block;
        width: 1fr;
        min-width: 0;
    }
    Screen.narrow #results-column.detail-visible {
        display: none;
    }
    """

    COMMAND_PALETTE_BINDING = "ctrl+k"

    BINDINGS = [
        Binding("escape", "escape", "Close/Quit", show=True, priority=True),
        Binding("right", "focus_detail", "Detail", show=False),
        Binding("left", "focus_results", "Results", show=False),
        Binding("r", "resume_selected", "Resume", show=True),
        Binding("c", "copy_resume", "Copy", show=True),
        Binding("o", "open_project", "Open", show=True),
        Binding("p", "cycle_provider", "Provider", show=True),
        Binding("f", "open_settings", "Filters", show=True),
        Binding("?", "show_help", "Help", show=True),
        Binding("ctrl+a", "ask_ai", "Ask AI", show=True),
        Binding("ctrl+o", "open_settings", "Settings", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("1", "detail_tab('overview')", "Overview", show=False),
        Binding("2", "detail_tab('why')", "Why", show=False),
        Binding("3", "detail_tab('activity')", "Activity", show=False),
        Binding("4", "detail_tab('related')", "Related", show=False),
        Binding("5", "detail_tab('ai')", "AI", show=False),
    ]

    def __init__(self, initial_query: str = "", initial_results: list[SearchResult] | None = None, db_path=None):
        super().__init__()
        self.initial_query = initial_query
        self._all_results = initial_results or []
        self._visible_results: list[SearchResult] = []
        self._selected_result: SearchResult | None = None
        self._provider_scope = "all"
        self._db_path = db_path
        self._last_query = initial_query
        self._index_summary = "Index status unknown"
        self._ai_chats: dict[str, list[tuple[str, str]]] = {}

    def _apply_responsive_layout(self, width: int) -> None:
        if width < 110:
            self.screen.add_class("narrow")
        else:
            self.screen.remove_class("narrow")
            self.query_one("#detail-column", Vertical).remove_class("detail-visible")
            self.query_one("#results-column", Vertical).remove_class("detail-visible")

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static("Loading index summary...", id="summary")
            yield Input(
                placeholder="Search all sessions... try file:auth.py  cmd:pytest  branch:main",
                value=self.initial_query,
                id="search-input",
            )
            yield FilterBar(id="filter-bar")
        with Horizontal(id="main"):
            with Vertical(id="results-column"):
                yield Static("", id="results-meta")
                yield ListView(id="results")
            with Vertical(id="detail-column"):
                yield DetailPanel(id="detail")
                yield Input(
                    placeholder="Ask this transcript... e.g. what did we conclude about cost?",
                    id="ai-chat-input",
                )
        yield Label("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"code-recall v{__version__}"
        self._apply_responsive_layout(self.size.width)
        self._refresh_index_summary()
        self.query_one("#search-input", Input).focus()
        if self._all_results and self.initial_query.strip():
            self._display_results(self._all_results, self.initial_query)
        elif not self.initial_query.strip():
            self._load_recent()
        else:
            self._display_results([], self.initial_query)

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("Search all providers", "Clear provider filter", partial(self._set_provider_scope, "all"))
        yield SystemCommand("Filter provider: Claude", "Show Claude Code sessions", partial(self._set_provider_scope, "claude"))
        yield SystemCommand("Filter provider: Codex", "Show Codex sessions", partial(self._set_provider_scope, "codex"))
        yield SystemCommand("Copy resume command", "Copy selected session resume command", self.action_copy_resume)
        yield SystemCommand("Resume selected session", "Exit and resume the selected session", self.action_resume_selected)
        yield SystemCommand("Ask AI", "Investigate current query/result set", self.action_ask_ai)
        yield SystemCommand("Reindex now", "Run code-recall index in the background", self.action_reindex)
        yield SystemCommand("Open settings", "Edit search mode, limits, and toggles", self.action_open_settings)
        yield SystemCommand("Show help", "Show TUI keyboard help", self.action_show_help)

    def on_key(self, event) -> None:
        search = self.query_one("#search-input", Input)

        if event.key == "/" and self.focused != search:
            self.action_focus_search()
            event.prevent_default()
            return

        if self.focused == search and event.key in ("ctrl+w", "ctrl+backspace"):
            text = search.value
            stripped = text.rstrip()
            if " " in stripped:
                search.value = stripped[: stripped.rfind(" ") + 1]
            else:
                search.value = ""
            search.cursor_position = len(search.value)
            event.prevent_default()
            return

        if self.focused == search and event.key in ("super+backspace", "cmd+backspace"):
            search.value = ""
            event.prevent_default()

    @on(events.Resize)
    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        self._last_query = query
        if not query:
            self._set_status("Showing recent sessions across selected providers")
            self._load_recent()
            return
        self._set_status(f'Searching for "{query}"...')
        self.query_one("#results", ListView).add_class("loading-results")
        self._debounced_search(query)

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        self.action_focus_results()

    @on(Input.Submitted, "#ai-chat-input")
    def on_ai_chat_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        event.input.value = ""
        self._submit_ai_chat(question)

    @on(ListView.Highlighted, "#results")
    def on_result_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and isinstance(event.item, SessionItem):
            if event.item.result not in self._visible_results:
                return
            self._selected_result = event.item.result
            self.query_one("#detail", DetailPanel).set_result(event.item.result, self._last_query, self._db_path)
            self._sync_ai_chat_for_selected()
            self._sync_ai_input_visibility()
            self._set_status("r resume | c copy command | 1-5 detail tabs | Ctrl+A ask AI")

    @on(ListView.Selected, "#results")
    def on_result_selected(self, event: ListView.Selected) -> None:
        if event.item and isinstance(event.item, SessionItem):
            self._selected_result = event.item.result
            self.action_focus_detail()

    def _refresh_index_summary(self) -> None:
        try:
            from code_recall.db import DB_PATH, get_connection

            db_path = self._db_path or DB_PATH
            if not Path(db_path).exists():
                self._index_summary = "No index yet - run code-recall index"
            else:
                conn = get_connection(db_path)
                rows = conn.execute(
                    "SELECT provider, COUNT(*) AS count, MAX(last_activity) AS last_activity "
                    "FROM sessions WHERE is_subagent = 0 GROUP BY provider ORDER BY provider"
                ).fetchall()
                conn.close()
                if rows:
                    total = sum(row["count"] for row in rows)
                    counts = ", ".join(
                        f"{provider_display(row['provider']).short_label} {row['count']}" for row in rows
                    )
                    latest = max((row["last_activity"] for row in rows if row["last_activity"]), default=None)
                    self._index_summary = f"{total} sessions indexed | {counts} | latest {format_date(latest)}"
                else:
                    self._index_summary = "Index exists but has no sessions"
        except Exception as exc:
            self._index_summary = f"Index status unavailable: {exc}"

        self.query_one("#summary", Static).update(
            f"[bold]code-recall v{__version__}[/bold]  {escape(self._index_summary)}"
        )

    @work(exclusive=True, thread=True)
    def _load_recent(self) -> None:
        from code_recall.config import load_config
        from code_recall.searcher import recent_sessions

        limit = load_config().get("limit", 20)
        kwargs = {"limit": limit}
        if self._db_path:
            kwargs["db_path"] = self._db_path
        try:
            results = recent_sessions(**kwargs)
            self.call_from_thread(self._display_results, results, "")
        except Exception as exc:
            self.call_from_thread(self._display_error, "Could not load recent sessions", exc)

    @work(exclusive=True, thread=True)
    def _debounced_search(self, query: str) -> None:
        import time

        time.sleep(0.35)
        current = self.query_one("#search-input", Input).value.strip()
        if current != query:
            return

        from code_recall.config import load_config
        from code_recall.searcher import search as do_search

        limit = load_config().get("limit", 20)
        kwargs = {"query": query, "limit": limit}
        if self._db_path:
            kwargs["db_path"] = self._db_path
        try:
            results = do_search(**kwargs)
            self.call_from_thread(self._display_results, results, query)
        except Exception as exc:
            self.call_from_thread(self._display_error, f'Could not search for "{query}"', exc)

    def _display_results(self, results: list[SearchResult], query: str = "") -> None:
        from code_recall.config import load_config

        self._all_results = results
        self._last_query = query
        visible = self._filter_results(results)
        self._visible_results = visible

        list_view = self.query_one("#results", ListView)
        list_view.remove_class("loading-results")
        list_view.index = None
        list_view.clear()

        for rank, result in enumerate(visible, 1):
            list_view.append(SessionItem(result, rank, query))

        search_mode = load_config().get("search_mode", "hybrid")
        self.query_one("#filter-bar", FilterBar).update_filters(self._provider_scope, search_mode, visible)
        self._update_results_meta(results, visible, query)

        if visible:
            list_view.index = 0
            first = visible[0]
            self._selected_result = first
            self.query_one("#detail", DetailPanel).set_result(first, query, self._db_path)
            self._sync_ai_chat_for_selected()
            self._sync_ai_input_visibility()
            self._set_status("Browse results: Up/Down select, Right detail, r resume, p provider")
        else:
            self._selected_result = None
            self.query_one("#detail", DetailPanel).set_result(None, query, self._db_path)
            self._sync_ai_input_visibility()
            if results and self._provider_scope != "all":
                self._set_status("No sessions in this provider scope. Press p to broaden.")
            elif query:
                self._set_status("No results. Clear filters, broaden provider scope, or run code-recall index.")
            else:
                self._set_status("No indexed sessions found. Run code-recall index.")

    def _display_error(self, title: str, exc: Exception) -> None:
        self.query_one("#results", ListView).clear()
        self.query_one("#results", ListView).remove_class("loading-results")
        self.query_one("#results-meta", Static).update(f"[red]{escape(title)}[/red]")
        self.query_one("#detail", DetailPanel).render_empty(
            f"[bold red]{escape(title)}[/bold red]\n\n"
            f"{escape(str(exc))}\n\n"
            "[dim]Try running code-recall index, or inspect ~/.code-recall for the database and logs.[/dim]"
        )
        self._set_status(title)

    def _filter_results(self, results: list[SearchResult]) -> list[SearchResult]:
        if self._provider_scope == "all":
            return results
        return [result for result in results if result.session.provider == self._provider_scope]

    def _update_results_meta(self, all_results: list[SearchResult], visible: list[SearchResult], query: str) -> None:
        if query:
            prefix = f'{len(visible)} of {len(all_results)} results for "{escape(query)}"'
        else:
            prefix = f"{len(visible)} recent sessions"
        scope = "all providers" if self._provider_scope == "all" else provider_display(self._provider_scope).label
        self.query_one("#results-meta", Static).update(
            f"[bold]{prefix}[/bold] [dim]in {scope}[/dim]"
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Label).update(message)

    def _sync_ai_input_visibility(self) -> None:
        ai_input = self.query_one("#ai-chat-input", Input)
        detail = self.query_one("#detail", DetailPanel)
        if detail.active_tab == "ai" and self._selected_result:
            ai_input.add_class("visible")
        else:
            ai_input.remove_class("visible")

    def _chat_messages_for_selected(self) -> list[tuple[str, str]]:
        if not self._selected_result:
            return []
        return self._ai_chats.setdefault(self._selected_result.session.session_id, [])

    def _sync_ai_chat_for_selected(self, busy: bool = False) -> None:
        detail = self.query_one("#detail", DetailPanel)
        if detail.active_tab != "ai" or not self._selected_result:
            return
        detail.set_ai_chat(
            self._chat_messages_for_selected(),
            busy=busy,
            assistant_label=_assistant_label_for_session(self._selected_result.session.provider),
        )

    def _submit_ai_chat(self, question: str) -> None:
        if not self._selected_result:
            self._set_status("Select a session before chatting with its transcript")
            return
        result = self._selected_result
        messages = self._chat_messages_for_selected()
        messages.append(("user", question))
        self.query_one("#detail", DetailPanel).set_ai_chat(
            messages,
            busy=True,
            assistant_label=_assistant_label_for_session(result.session.provider),
        )
        self._sync_ai_input_visibility()
        self._set_status(f"Transcript chat running with {_assistant_label_for_session(result.session.provider)}...")
        self._ask_transcript_chat(result.session.session_id, question, result, list(messages[:-1]))

    def _set_provider_scope(self, provider_scope: str) -> None:
        self._provider_scope = provider_scope
        self._display_results(self._all_results, self._last_query)

    def action_cycle_provider(self) -> None:
        order = ("all", "claude", "codex")
        current = order.index(self._provider_scope) if self._provider_scope in order else 0
        self._set_provider_scope(order[(current + 1) % len(order)])

    def action_focus_search(self) -> None:
        search = self.query_one("#search-input", Input)
        search.focus()
        search.cursor_position = len(search.value)

    def action_focus_results(self) -> None:
        results = self.query_one("#results", ListView)
        if results.children:
            results.focus()
            if results.index is None:
                results.index = 0

    def action_focus_detail(self) -> None:
        detail_column = self.query_one("#detail-column", Vertical)
        detail = self.query_one("#detail", DetailPanel)
        results_column = self.query_one("#results-column", Vertical)
        detail_column.add_class("detail-visible")
        results_column.add_class("detail-visible")
        ai_input = self.query_one("#ai-chat-input", Input)
        if detail.active_tab == "ai" and self._selected_result:
            ai_input.focus()
        else:
            detail.focus()

    def action_focus_results_from_detail(self) -> None:
        self.action_focus_results()

    def action_detail_tab(self, tab: str) -> None:
        detail = self.query_one("#detail", DetailPanel)
        detail.set_tab(tab)
        if tab == "ai":
            self._sync_ai_chat_for_selected()
        self.query_one("#detail-column", Vertical).add_class("detail-visible")
        self.query_one("#results-column", Vertical).add_class("detail-visible")
        self._sync_ai_input_visibility()
        if tab == "ai" and self._selected_result:
            self.query_one("#ai-chat-input", Input).focus()

    def action_escape(self) -> None:
        detail_column = self.query_one("#detail-column", Vertical)
        results_column = self.query_one("#results-column", Vertical)
        if "detail-visible" in detail_column.classes:
            detail_column.remove_class("detail-visible")
            results_column.remove_class("detail-visible")
            self.action_focus_results()
            return
        if self.focused and self.focused.id == "search-input" and self.query_one("#search-input", Input).value:
            self.query_one("#search-input", Input).value = ""
            return
        self.exit()

    def action_resume_selected(self) -> None:
        if not self._selected_result:
            self._set_status("No session selected")
            return
        self.exit(self._selected_result.session.session_id)

    def action_copy_resume(self) -> None:
        if not self._selected_result:
            self._set_status("No session selected")
            return
        self.copy_to_clipboard(self._selected_result.resume_command)
        self._set_status(f"Copied: {self._selected_result.resume_command}")

    def action_open_project(self) -> None:
        if not self._selected_result:
            self._set_status("No session selected")
            return
        project_path = self._selected_result.session.project_path
        open_path = project_path
        while open_path and not os.path.exists(open_path):
            open_path = os.path.dirname(open_path)
        if not open_path:
            self._set_status(f"Project path no longer exists: {project_path}")
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener):
            subprocess.Popen([opener, open_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._set_status(f"Opened {open_path}")
        else:
            self._set_status(f"Project: {open_path}")

    def action_open_settings(self) -> None:
        def on_dismiss(changed: bool | None) -> None:
            if changed:
                self._refresh_index_summary()
                self._display_results(self._all_results, self._last_query)
                self._set_status("Settings saved")

        self.push_screen(SettingsScreen(), callback=on_dismiss)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reindex(self) -> None:
        self._set_status("Indexing in background...")
        self._reindex()

    @work(thread=True, group="index", exclusive=True)
    def _reindex(self) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "code_recall.cli", "index", "--quiet"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "index failed")
            self.call_from_thread(self._refresh_index_summary)
            self.call_from_thread(self._load_recent)
            self.call_from_thread(self._set_status, "Index updated")
        except Exception as exc:
            self.call_from_thread(self._display_error, "Index failed", exc)

    def action_ask_ai(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query and self._selected_result:
            query = _session_title(self._selected_result, 120)
        if not query:
            self._set_status("Type a question or select a session before asking AI")
            return
        if not self._visible_results:
            self._set_status("No visible results to investigate")
            return
        detail = self.query_one("#detail", DetailPanel)
        preferred_provider = self._selected_result.session.provider if self._selected_result else None
        detail.set_ai_loading(query, assistant_label=_assistant_label_for_session(preferred_provider))
        self.action_focus_detail()
        self._set_status(f"AI investigation running with {_assistant_label_for_session(preferred_provider)}...")
        self._ask_ai(query, list(self._visible_results), preferred_provider)

    @work(thread=True, group="transcript-chat", exclusive=True)
    def _ask_transcript_chat(
        self,
        session_id: str,
        question: str,
        result: SearchResult,
        history: list[tuple[str, str]],
    ) -> None:
        from code_recall.agentic import AgenticAnswer, answer_query
        from code_recall.db import DB_PATH

        recent_history = "\n".join(
            f"{'User' if role == 'user' else 'Assistant'}: {text}"
            for role, text in history[-6:]
        )
        prompt = (
            "You are chatting with the user about one selected coding-agent transcript.\n"
            "Use only this selected session and its transcript/source file as evidence. "
            "Do not answer from unrelated sessions. Cite the session id when making concrete claims.\n\n"
            f"Selected session id: {result.session.session_id}\n"
            f"Selected project: {result.session.project_path}\n"
            f"Selected provider: {provider_display(result.session.provider).label}\n\n"
            f"Previous chat:\n{recent_history or '(none)'}\n\n"
            f"New user question:\n{question}"
        )
        try:
            answer = answer_query(
                query=prompt,
                results=[result],
                db_path=self._db_path or DB_PATH,
                max_sessions=1,
                preferred_provider=result.session.provider,
            )
        except Exception as exc:
            answer = AgenticAnswer(
                ok=False,
                query=question,
                answer="Transcript chat failed before the assistant returned an answer.",
                sources=[],
                error=str(exc),
            )
        self.call_from_thread(self._show_transcript_chat_answer, session_id, answer)

    @work(thread=True, group="ask-ai", exclusive=True)
    def _ask_ai(self, query: str, results: list[SearchResult], preferred_provider: str | None = None) -> None:
        from code_recall.agentic import AgenticAnswer, answer_query
        from code_recall.db import DB_PATH

        try:
            answer = answer_query(
                query=query,
                results=results,
                db_path=self._db_path or DB_PATH,
                max_sessions=min(len(results), 10),
                preferred_provider=preferred_provider,
            )
        except Exception as exc:
            answer = AgenticAnswer(
                ok=False,
                query=query,
                answer="AI investigation failed before the assistant returned an answer.",
                sources=[],
                error=str(exc),
            )
        self.call_from_thread(self._show_ai_answer, answer)

    def _show_ai_answer(self, answer) -> None:
        source_lines = []
        for source in answer.sources[:8]:
            source_lines.append(
                f"[dim]{source.rank}.[/dim] [cyan]{escape(source.title)}[/cyan]\n"
                f"   [dim]{escape(source.project_path)} · {source.activity} · {source.score:.0%}[/dim]\n"
                f"   [green]{escape(source.resume_command)}[/green]"
            )
        error_line = f"\n\n[red]{escape(answer.error)}[/red]" if answer.error else ""
        content = (
            f"[bold]AI investigation[/bold]\n"
            f"[dim]Assistant provider:[/dim] {escape(provider_display(answer.assistant_provider).label if answer.assistant_provider else 'Unavailable')}\n"
            f"[dim]Question:[/dim] {escape(answer.query)}\n\n"
            f"{escape(answer.answer)}"
            f"{error_line}\n\n"
            f"[bold]Evidence sources:[/bold]\n"
            f"{chr(10).join(source_lines) if source_lines else '[dim]No sources[/dim]'}"
        )
        assistant_label = provider_display(answer.assistant_provider).label if answer.assistant_provider else "Unavailable"
        self.query_one("#detail", DetailPanel).set_ai_answer(content, ok=answer.ok, assistant_label=assistant_label)
        self._sync_ai_input_visibility()
        self._set_status("AI investigation complete" if answer.ok else "AI investigation failed")

    def _show_transcript_chat_answer(self, session_id: str, answer) -> None:
        messages = self._ai_chats.setdefault(session_id, [])
        response = answer.answer
        if answer.error:
            response = f"{response}\n\nError: {answer.error}"
        messages.append(("assistant", response))
        if self._selected_result and self._selected_result.session.session_id == session_id:
            assistant_label = provider_display(answer.assistant_provider).label if answer.assistant_provider else "Unavailable"
            self.query_one("#detail", DetailPanel).set_ai_chat(messages, busy=False, assistant_label=assistant_label)
            self._sync_ai_input_visibility()
        self._set_status("Transcript chat complete" if answer.ok else "Transcript chat failed")


def run_tui(query: str, results: list[SearchResult], db_path=None) -> None:
    """Launch the TUI and resume the selected session if requested."""
    app = RecallApp(initial_query=query, initial_results=results, db_path=db_path)
    session_id = app.run()

    if not session_id:
        return

    result = next((item for item in app._all_results if item.session.session_id == session_id), None)
    if result is None:
        return

    project_path = result.session.project_path
    resume_dir = project_path
    while resume_dir and not os.path.isdir(resume_dir):
        resume_dir = os.path.dirname(resume_dir)

    if resume_dir and os.path.isdir(resume_dir):
        os.chdir(resume_dir)
        if resume_dir != project_path:
            print(f"\nNote: {project_path} no longer exists", file=sys.stderr)
            print(f"cd {resume_dir} (nearest parent)", file=sys.stderr)
        else:
            print(f"\ncd {resume_dir}", file=sys.stderr)

    resume_id = result.session.provider_session_id or result.session.session_id
    print(result.resume_command, file=sys.stderr)
    display = provider_display(result.session.provider)
    argv = [display.resume_binary, *display.resume_args, resume_id]
    if sys.platform == "win32":
        subprocess.run(argv)
    else:
        os.execvp(argv[0], argv)
