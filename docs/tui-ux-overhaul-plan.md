# code-recall TUI UX Overhaul Plan

## Objective

Make the TUI easier to scan, faster to operate from the keyboard, and clearer about why each result is relevant. The redesign must treat Claude Code and Codex as first-class providers, with room for more providers later.

## Current UX Findings

Source inspected: `src/code_recall/tui.py`, `src/code_recall/models.py`, `src/code_recall/config.py`, and the README TUI documentation.

### What Works

- The app already has a focused search-first shape: input at the top, results below, optional detail preview.
- Empty search loads recent sessions, which is a good browse state.
- Result rows include provider, model, activity date, message count, size, project, score, last prompt, and a snippet.
- The preview panel already contains useful evidence: title, provider/model/branch/project/date, started/left-off prompts, files, commands, matched evidence, related sessions, and resume command intent.
- Search runs in a background worker and dims stale results while waiting.
- Settings are accessible through a modal with keyboard-friendly controls.

### Main Problems

- The first screen does not orient the user. It says "Claude + Codex recall" but does not show provider coverage, index freshness, available filters, or what changed since the last run.
- Results are visually dense but not structured. Important fields compete in one text block, and score bars draw attention without explaining relevance.
- The preview is hidden by default, then auto-opens on highlight. That layout shift can feel surprising, especially on narrower terminals.
- "Why this matched" is too weak. Matched snippets exist, but the row and preview do not clearly map query terms to prompt, path, file, branch, command, semantic chunk, or related-session graph.
- Multi-provider support is surfaced as labels, not as an organizing principle. There is no provider filter, provider count, capability indication, or provider-specific resume affordance beyond the final command.
- The AI summary auto-runs on highlight, uses Claude even for Codex sessions, and appends into the preview. This can make navigation feel busy and can blur "session provider" with "AI answer provider".
- Discoverability is mostly footer bindings. There is no command palette for common actions, filters, help, provider switching, copy command, open transcript, or reindex.
- Settings mix search quality controls and operational toggles, but provider controls and AI behavior are not represented as explicit UX choices.
- Error, loading, empty, no-index, and partial-provider states are not designed as first-class screens.

## Research Anchors

- Textual has a built-in command palette and supports app-specific system commands; this should become the primary discovery layer for actions beyond the obvious search flow: <https://textual.textualize.io/guide/command_palette/>
- Textual supports background workers, loading states, `DataTable`, `ContentSwitcher`, `Markdown`, tabs, and modal screens. Use these instead of manually appending/removing large `Static` blocks for everything: <https://textual.textualize.io/guide/widgets/> and <https://textual.textualize.io/guide/workers/>
- Keyboard UI should give every feature keyboard access, keep navigation order logical, and use simple consistent shortcuts. Microsoft specifically calls out Tab/Shift+Tab for pane traversal, arrows within panes, Enter for default action, Esc for cancel, and Ctrl-letter/F-key shortcuts for frequent actions: <https://learn.microsoft.com/en-us/previous-versions/windows/desktop/dnacc/guidelines-for-keyboard-user-interface-design>
- Search results need contextual snippets that explain why each result is shown; otherwise users lose confidence and bounce between results and details to inspect relevance: <https://baymard.com/blog/search-snippets>
- Filters should expose the primary/common filters, hide rarer filters behind a menu, show applied filters clearly, support clearing, and use live filtering only when results return quickly: <https://designsystem.maersk.com/guidelines/search-filter-and-sort/filter-patterns/>
- Progressive disclosure should separate core task information from secondary detail while still making help, field hints, messages, badges, and AI explainability part of the product information architecture: <https://www.ibm.com/docs/en/technical-content?topic=practices-progressive-disclosure>

## Target Information Architecture

### App Frame

Use a stable three-zone layout:

1. Header/search zone: app name, indexed coverage, search input, active filter chips.
2. Work zone: results pane plus detail pane. Do not unexpectedly resize after first result highlight.
3. Status/action zone: current state, selected action, and the few most relevant shortcuts.

The first view should immediately answer:

- How many sessions are indexed?
- Which providers are available and selected?
- When was the index last updated?
- What can I type or filter by?
- What action will Enter perform right now?

### Search and Filter Model

Default scope should be "All providers". Add visible filter chips:

- Provider: All, Claude, Codex, future providers.
- Project.
- Branch.
- File touched.
- Command run.
- Date range.
- Session type: main, subagent, both.
- Search mode: keyword, hybrid, llm.

Use live search for text query and primary provider chips. Use an "Apply filters" panel for expensive or combined filters if search takes more than about a second.

### Results Pane

Replace the current one-block `ListView` rows with a scannable result item component or `DataTable`-backed dense view. The important change is structure, not the exact widget.

Each result should reserve stable columns/regions:

- Rank and relevance: compact score plus reason label, such as `semantic`, `file`, `command`, `branch`, `related`.
- Provider badge: `Claude`, `Codex`, later provider names.
- Title: cleaned summary or first prompt.
- Project and branch.
- Activity: last activity date and message count.
- Evidence line: contextual snippet with query hit source, for example `file match: src/auth.py`, `command match: pytest`, `prompt match: "...oauth..."`.

Avoid score bars as the dominant visual signal. They are secondary to "why this result is here".

### Detail Pane

Make the preview always present on normal-width terminals, with tabs or a `ContentSwitcher`:

- Overview: title, provider, model, project, branch, date, message count, resume command.
- Why matched: ranked evidence grouped by source type: prompt, transcript, file, command, branch, graph, semantic chunk.
- Files and commands: touched files and commands with counts, not a flattened line.
- Related: related sessions with relationship reason and provider.
- AI: generated summary/investigation output, sources, and error/progress state.

On narrow terminals, collapse the detail pane into a full-screen detail view opened with Enter or Right Arrow. Keep Left Arrow/Esc as an obvious way back.

### Provider Model

Introduce a provider display registry used by TUI, CLI output, and docs:

- `id`: `claude`, `codex`, future provider ids.
- Label and short label.
- Color/token style.
- Resume command builder.
- Transcript/source path label.
- Capabilities: can resume, has model, has branch, has files, has commands, can open source transcript.

The UI should distinguish:

- Session provider: where the indexed session came from.
- Assistant provider: what powers AI investigation or summaries.

For example, a Codex session summarized by Claude should be shown as `Session: Codex` and `AI: Claude`, not just "Claude".

## Proposed Interaction Model

### Keyboard

- `/` or focus on start: search.
- `Tab` / `Shift+Tab`: move between search, filters, results, detail tabs.
- `Up` / `Down`: move within results.
- `Right`: open/focus detail; on narrow screens, enter detail view.
- `Left` or `Esc`: return from detail to results, then clear transient UI, then quit.
- `Enter`: primary action for focused control. In results, open/resume choice should be explicit.
- `r`: resume selected session.
- `c`: copy resume command.
- `o`: open source transcript or project folder when available.
- `f`: filters.
- `p`: provider filter cycle or provider picker.
- `?`: help/shortcuts.
- `Ctrl+K` or Textual palette binding: command palette.
- `Ctrl+A`: AI investigation, but only when the current query/result context is clear.

Avoid overloading `Ctrl+P` for preview if Textual users expect it to open the command palette. Make preview a pane/tab behavior, not a hidden mode.

### Command Palette

Add commands:

- Search all providers.
- Filter provider: Claude / Codex / All.
- Toggle subagents.
- Copy resume command.
- Resume selected session.
- Open transcript.
- Reindex now.
- Open settings.
- Ask AI about selected session.
- Ask AI about current result set.
- Show help.
- Change theme.

### AI Behavior

Do not auto-run AI summary on every highlight by default. Replace it with:

- Fast deterministic summary from indexed data in Overview.
- Explicit `AI` tab or action for expensive summaries.
- Clear progress state with cancellability.
- Source list always visible after answer.
- Provider label for the answering model.
- Setting: auto-summary off by default, or only after a delay and only once per selected session.

## Screen States To Design

- First run/no index: show detected providers, missing providers, index action, and what will be indexed.
- Recent sessions: provider counts, recent sessions, index freshness, quick filters.
- Searching: non-jumpy loading indicator and stale-results marker.
- Results: count, active filters, sort/search mode, selected result details.
- No results: query-preserving suggestions, clear filters, broaden provider scope, run index.
- Provider missing: explain which source is absent, without treating it as an error.
- Search error/database error: show actionable repair path and log location.
- AI running: progress, cancel, source scope.
- AI failed: keep deterministic evidence visible; do not wipe the selected session detail.

## Implementation Plan

### Phase 1: UX Skeleton and State

- Split `src/code_recall/tui.py` into widgets/screens:
  - `tui/app.py`
  - `tui/results.py`
  - `tui/detail.py`
  - `tui/filters.py`
  - `tui/settings.py`
  - `tui/providers.py`
- Add an explicit TUI state object: query, selected result id, active filters, visible pane, detail tab, provider scope, loading state, error state.
- Add provider display registry and use it anywhere provider-specific labels or resume commands are rendered.

### Phase 2: Results and Evidence

- Redesign result rows around title, provider badge, project/branch, activity, and contextual evidence.
- Add reason tags from existing search signals where available: FTS, vector, graph, structured prefixes.
- Add result count and active filter chips above the results.
- Keep row heights stable enough that scrolling feels predictable.

### Phase 3: Detail Pane

- Replace append-only preview rendering with a detail component and tabs/content switcher.
- Group details by user task: overview, why matched, files/commands, related, AI.
- Keep deterministic evidence visible even when AI is loading or failed.

### Phase 4: Filters, Command Palette, and Help

- Add visible provider filters and a filter panel.
- Add command palette commands for common actions.
- Add a help screen that mirrors actual bindings and pane focus behavior.
- Rework shortcuts around standard keyboard navigation.

### Phase 5: Responsive and Failure States

- Add breakpoints for narrow terminals:
  - Wide: results + detail side by side.
  - Medium: detail collapsible but no automatic layout shift.
  - Narrow: one pane at a time with explicit navigation.
- Implement no-index, no-results, partial-provider, loading, and error screens.

### Phase 6: Test and Validate

- Add Textual Pilot tests for:
  - first-run/recent screen renders,
  - search updates results,
  - provider filter changes result scope,
  - selecting a result updates detail without launching resume,
  - copy/resume command displays correct provider command,
  - settings save and restore,
  - no-results and error states.
- Add screenshot or snapshot checks for wide, medium, and narrow terminal sizes.
- Keep CLI/non-TUI output unchanged unless explicitly redesigned.

## Acceptance Criteria

- A new user can understand what is indexed and what to do without reading the README.
- Results explain relevance before the user opens a detail view.
- Provider scope is visible and controllable.
- Detail content is grouped by task, not by implementation field order.
- AI features are explicit, cancellable, and do not obscure deterministic evidence.
- All major actions are reachable by keyboard and discoverable through help or command palette.
- Narrow terminals remain usable without overlapping or surprising pane changes.
- Claude Code and Codex sessions show correct provider labels, metadata, and resume commands.
