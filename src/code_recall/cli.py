"""CLI interface for code-recall."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from code_recall import __version__
from code_recall.db import DB_PATH, get_connection, get_stats
from code_recall.indexer import build_index, ensure_index
from code_recall.searcher import search
from code_recall.utils import (
    CODEX_DIR,
    PROJECTS_DIR,
    app_data_dir,
    clean_display_text,
    format_date,
    format_size,
)

COMMAND_NAME = "code-recall"
LEGACY_COMMAND_NAMES = ("claude-code-recall", "claude-recall")
HOOKS_MARKER = app_data_dir() / ".hooks-installed"


def main(argv: list[str] | None = None) -> None:
    try:
        _run(argv)
    except KeyboardInterrupt:
        print("", file=sys.stderr)  # clean newline
        sys.exit(0)


def _run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="code-recall",
        description="Semantic search across local coding-agent sessions.",
        usage="%(prog)s [query ...] [options]\n       %(prog)s <command> [options]",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument(
        "--claude-dir", type=Path, default=PROJECTS_DIR, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--codex-dir", type=Path, default=CODEX_DIR, help=argparse.SUPPRESS
    )
    parser.add_argument("--no-codex", action="store_true", help=argparse.SUPPRESS)

    # Search options (work both with and without 'search' subcommand)
    parser.add_argument("query", nargs="*", help="Search query (or subcommand)")
    parser.add_argument("-n", "--limit", type=int, default=None, help="Max results")
    parser.add_argument("-p", "--project", help="Filter by project path substring")
    parser.add_argument("--after", help="Only sessions after this date (YYYY-MM-DD)")
    parser.add_argument("--before", help="Only sessions before this date (YYYY-MM-DD)")
    parser.add_argument(
        "--semantic", action="store_true", default=None, help="Force semantic search"
    )
    parser.add_argument("--no-semantic", action="store_true", help="Disable semantic search")
    parser.add_argument("--no-tui", action="store_true", help="Plain text output (no TUI)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output")
    parser.add_argument("--min-messages", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    parser.add_argument("--force", action="store_true", help="Force full reindex (with 'index')")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("-y", "--yes", action="store_true", help="Confirm update command")
    parser.add_argument("--ai-model", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--ai-timeout", type=int, default=90, help=argparse.SUPPRESS)
    parser.add_argument("--no-ai-tools", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.no_codex:
        args.codex_dir = None

    # Setup logging
    from code_recall.logger import enable_verbose, get_logger

    log = get_logger()
    if getattr(args, "verbose", False):
        enable_verbose(log)
    log.debug(f"code-recall started, args={vars(args)}")

    # Route to subcommands if the first positional arg is a known command
    command = args.query[0] if args.query else None

    if command in ("index", "i"):
        _cmd_index(args)
    elif command == "info":
        _cmd_info(args)
    elif command == "gc":
        _cmd_gc(args)
    elif command == "config":
        _cmd_config(args)
    elif command == "update":
        _cmd_update(args)
    elif command == "eval":
        _cmd_eval(args)
    elif command in ("ask", "investigate"):
        _cmd_ask(args)
    elif command in ("install-hooks", "setup"):
        _cmd_install_hooks()
    elif command == "search" or command == "s":
        # Explicit 'search' subcommand — strip it from query
        args.query = args.query[1:]
        _cmd_search(args)
    else:
        # Default: treat all positional args as search query
        _cmd_search(args)

    # Non-blocking update check (once per day)
    from code_recall.updater import check_for_update

    check_for_update(quiet=args.quiet or args.json_output or command == "update")


def _first_run_setup(args: argparse.Namespace) -> None:
    """Run on first use: index + install hooks."""
    is_first_run = not args.db.exists()
    show_output = not args.quiet and not args.json_output

    if is_first_run and show_output:
        print("Welcome to code-recall! Setting up...\n", file=sys.stderr)

    # Auto-index (silent on subsequent runs — takes <1s)
    ensure_index(
        args.claude_dir,
        args.db,
        codex_dir=args.codex_dir,
        verbose=is_first_run and show_output,
    )

    # Auto-install hooks on first run
    if is_first_run and not HOOKS_MARKER.exists():
        _auto_install_hooks()


def _auto_install_hooks() -> None:
    """Silently install SessionEnd hooks on first run."""
    from code_recall.config import load_config

    if not load_config().get("auto_index_hook", True):
        return

    import shutil

    settings_path = Path.home() / ".claude" / "settings.json"
    code_recall_bin = shutil.which(COMMAND_NAME)
    if not code_recall_bin:
        return

    hook_command = f"{code_recall_bin} index --quiet"
    desired_hook = _index_hook_config(hook_command)

    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    hooks = settings.get("hooks", {})
    session_end_hooks = hooks.get("SessionEnd", [])

    # Don't install if already present
    for rule in session_end_hooks:
        for hook in rule.get("hooks", []):
            if _hook_mentions_app(hook.get("command", "")):
                hook.update(desired_hook)
                try:
                    settings_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(settings_path, "w") as f:
                        json.dump(settings, f, indent=2)
                    HOOKS_MARKER.parent.mkdir(parents=True, exist_ok=True)
                    HOOKS_MARKER.touch()
                except OSError:
                    pass
                return

    new_hook = {
        "hooks": [desired_hook]
    }
    session_end_hooks.append(new_hook)
    hooks["SessionEnd"] = session_end_hooks
    settings["hooks"] = hooks

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        HOOKS_MARKER.parent.mkdir(parents=True, exist_ok=True)
        HOOKS_MARKER.touch()
        print(
            "  Auto-installed SessionEnd hook for live index updates.\n",
            file=sys.stderr,
        )
    except OSError:
        pass


def _cmd_config(args: argparse.Namespace) -> None:
    """View or set config values."""
    from code_recall.config import print_config, set_value

    config_args = args.query[1:]  # strip "config"
    if len(config_args) >= 2:
        key, value = config_args[0], " ".join(config_args[1:])
        err = set_value(key, value)
        if err:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        print(f"Set {key} = {value}")
    else:
        print_config()


def _cmd_update(args: argparse.Namespace) -> None:
    """Check for and optionally install the latest release."""
    from code_recall.updater import run_update

    code = run_update(yes=args.yes, quiet=args.quiet)
    if code:
        sys.exit(code)


def _cmd_ask(args: argparse.Namespace) -> None:
    """Answer a natural-language question using indexed sessions as evidence."""
    from code_recall.agentic import answer_query
    from code_recall.config import load_config

    ask_args = args.query[1:]  # strip "ask" / "investigate"
    query = " ".join(ask_args).strip()
    if not query:
        print("Usage: code-recall ask <question>", file=sys.stderr)
        sys.exit(1)

    _first_run_setup(args)

    config = load_config()
    limit = args.limit if args.limit is not None else max(config.get("limit", 10), 8)
    semantic = None
    if args.semantic:
        semantic = True
    elif args.no_semantic or config["search_mode"] == "keyword":
        semantic = False

    results = search(
        query=query,
        db_path=args.db,
        limit=limit,
        project_filter=args.project,
        after=args.after,
        before=args.before,
        semantic=semantic,
        min_messages=args.min_messages,
    )
    answer = answer_query(
        query=query,
        results=results,
        db_path=args.db,
        model=args.ai_model,
        timeout=args.ai_timeout,
        max_sessions=min(limit, 12),
        use_read_tools=not args.no_ai_tools,
    )

    if args.json_output:
        json.dump(answer.to_dict(), sys.stdout, indent=2)
        print()
        return

    print(answer.answer)
    if answer.error:
        print(f"\nError: {answer.error}", file=sys.stderr)
    if answer.sources:
        print("\nSources:")
        for source in answer.sources[:8]:
            print(
                f"  {source.rank}. {source.title} "
                f"({source.activity}, score {source.score:.0%})"
            )
            print(f"     {source.project_path}")
            print(f"     {source.resume_command}")


def _cmd_eval(args: argparse.Namespace) -> None:
    """Run search recall eval cases from a JSON file."""
    from code_recall.eval import (
        generate_eval_cases,
        load_eval_cases,
        run_eval_cases,
        write_eval_cases,
    )

    eval_args = args.query[1:]  # strip "eval"
    if not eval_args:
        print(
            "Usage: code-recall eval <cases.json>\n"
            "       code-recall eval generate <output.json> [limit]",
            file=sys.stderr,
        )
        sys.exit(1)

    if eval_args[0] == "generate":
        output = Path(eval_args[1]) if len(eval_args) >= 2 else app_data_dir() / "eval-cases.json"
        limit = int(eval_args[2]) if len(eval_args) >= 3 else (args.limit or 30)
        _first_run_setup(args)
        cases = generate_eval_cases(args.db, limit=limit, min_messages=args.min_messages)
        write_eval_cases(cases, output)
        print(f"Wrote {len(cases)} eval cases to {output}")
        return

    cases_path = Path(eval_args[0])
    if not cases_path.exists():
        print(f"Eval file not found: {cases_path}", file=sys.stderr)
        sys.exit(1)

    _first_run_setup(args)

    semantic = None
    if args.semantic:
        semantic = True
    elif args.no_semantic:
        semantic = False

    report = run_eval_cases(
        load_eval_cases(cases_path),
        db_path=args.db,
        limit=args.limit,
        semantic=semantic,
    )

    if args.json_output:
        json.dump(report, sys.stdout, indent=2)
        print()
        if report["passed"] != report["total"]:
            sys.exit(1)
        return

    print(
        f"Search recall eval: {report['passed']}/{report['total']} "
        f"({report['accuracy']:.0%})"
    )
    for case in report["cases"]:
        marker = "PASS" if case["ok"] else "FAIL"
        print(f"{marker}  {case['query']}")
        for result in case["results"][:3]:
            print(
                f"  {result['rank']}. {result['summary'] or 'Untitled'} "
                f"({result['score']:.0%})"
            )
            print(f"     {result['project_path']} :: {result['session_id']}")

    if report["passed"] != report["total"]:
        sys.exit(1)


def _cmd_search(args: argparse.Namespace) -> None:
    from code_recall.config import load_config

    config = load_config()
    query = " ".join(args.query)

    # Use config limit if user didn't explicitly set -n
    if args.limit is None:
        args.limit = config.get("limit", 10)

    # Determine search mode from config + CLI flags
    semantic = None  # None = auto-detect (use if available)
    if args.semantic:
        semantic = True
    elif args.no_semantic:
        semantic = False
    elif config["search_mode"] == "keyword":
        semantic = False

    # First-run setup (auto-index + hooks)
    _first_run_setup(args)

    # No query + interactive terminal → open TUI for browsing
    if not query and sys.stdout.isatty():
        try:
            from code_recall.tui import run_tui

            run_tui("", [], db_path=args.db)
            return
        except ImportError:
            pass

    if not query:
        print("Usage: code-recall <query>", file=sys.stderr)
        print('  Example: code-recall "debugging auth middleware"', file=sys.stderr)
        sys.exit(1)

    results = search(
        query=query,
        db_path=args.db,
        limit=args.limit,
        project_filter=args.project,
        after=args.after,
        before=args.before,
        semantic=semantic,
        min_messages=args.min_messages,
    )

    if args.json_output:
        _print_json(results)
        return

    # TUI if available and not disabled
    if not args.no_tui and sys.stdout.isatty():
        try:
            from code_recall.tui import run_tui

            run_tui(query, results, db_path=args.db)
            return
        except ImportError:
            pass

    _print_plain(query, results)


def _print_plain(query: str, results: list) -> None:
    """Print results as formatted plain text."""
    if not results:
        print(f'No sessions found for "{query}"')
        return

    print(f'\nFound {len(results)} sessions for "{query}"\n')

    for i, r in enumerate(results, 1):
        s = r.session
        score_str = f"score: {r.score:.2f}"

        title = clean_display_text(s.summary) or clean_display_text(s.first_prompt) or "Untitled"
        if len(title) > 60:
            title = title[:60] + "..."
        print(f" {i:>2}. {title:<52} {score_str}")

        meta_parts = [r.display_project]
        activity = s.last_activity or s.modified
        if activity:
            meta_parts.append(format_date(activity))
        if s.git_branch:
            meta_parts.append(s.git_branch)
        meta_parts.append(f"{s.message_count} msgs")
        if s.file_size:
            meta_parts.append(format_size(s.file_size))
        print(f"     {' · '.join(meta_parts)}")

        # Show last activity and the best retrieval clue when available.
        if s.last_prompt and s.last_prompt != s.first_prompt:
            last = clean_display_text(s.last_prompt)
            if last:
                print(f"     Last: {last[:120]}")
        if r.snippets:
            snippet = clean_display_text(r.snippets[0])
            if snippet:
                print(f"     > {snippet[:120]}")

        print(f"     Resume: cd {r.display_project} && {r.resume_command}")
        print()


def _print_json(results: list) -> None:
    """Print results as JSON."""
    output = []
    for r in results:
        s = r.session
        output.append({
            "session_id": s.session_id,
            "provider": s.provider,
            "provider_session_id": s.provider_session_id,
            "project_path": s.project_path,
            "summary": s.summary,
            "first_prompt": s.first_prompt,
            "last_prompt": s.last_prompt,
            "git_branch": s.git_branch,
            "message_count": s.message_count,
            "file_size": s.file_size,
            "modified": s.modified,
            "last_activity": s.last_activity,
            "model": s.model,
            "score": round(r.score, 4),
            "fts_rank": r.fts_rank,
            "vec_score": round(r.vec_score, 4) if r.vec_score is not None else None,
            "snippets": r.snippets,
            "resume_command": r.resume_command,
        })
    json.dump(output, sys.stdout, indent=2)
    print()


def _cmd_index(args: argparse.Namespace) -> None:
    if not args.quiet:
        print("Building search index...", file=sys.stderr)

    # Pre-download models during explicit index (not during search)
    try:
        from code_recall.embedder import ensure_models_downloaded
        ensure_models_downloaded()
    except Exception:
        pass

    stats = build_index(
        projects_dir=args.claude_dir,
        codex_dir=args.codex_dir,
        db_path=args.db,
        force=args.force,
        verbose=not args.quiet,
    )
    if not args.quiet:
        print(
            f"\nIndex complete: {stats['indexed']} indexed, "
            f"{stats['skipped']} unchanged, "
            f"{stats['removed']} removed",
            file=sys.stderr,
        )


def _cmd_info(args: argparse.Namespace) -> None:
    if not args.db.exists():
        print("No index found. Run 'code-recall' to build it.")
        return

    conn = get_connection(args.db)
    stats = get_stats(conn)

    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    provider_rows = conn.execute(
        "SELECT provider, COUNT(*) AS count FROM sessions GROUP BY provider ORDER BY provider"
    ).fetchall()

    # Graph stats
    try:
        edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        files = conn.execute("SELECT COUNT(DISTINCT file_name) FROM session_files").fetchone()[0]
        chains = conn.execute("SELECT COUNT(DISTINCT chain_id) FROM session_chains").fetchone()[0]
    except Exception:
        edges = files = chains = 0

    conn.close()

    print(f"code-recall v{__version__}")
    print(f"  Database: {args.db}")
    print(f"  Size: {format_size(args.db.stat().st_size)}")
    print(
        f"  Sessions: {stats.get('total', 0)} "
        f"({stats.get('main_sessions', 0)} main, "
        f"{stats.get('subagent_sessions', 0)} subagent)"
    )
    if provider_rows:
        providers = ", ".join(f"{row['provider']}: {row['count']}" for row in provider_rows)
        print(f"  Providers: {providers}")
    print(f"  Chunks: {chunk_count}")
    print(f"  Projects: {stats.get('projects', 0)}")
    print(f"  Messages: {stats.get('total_messages', 0)}")
    print(f"  Graph: {edges} edges, {files} unique files, {chains} session chains")
    print(f"  Source size: {format_size(stats.get('total_size', 0) or 0)}")
    print(
        f"  Date range: {format_date(stats.get('earliest'))} to "
        f"{format_date(stats.get('latest'))}"
    )

    from code_recall import has_semantic, has_tui

    sem = "enabled" if has_semantic() else "not installed (pip install code-recall[semantic])"
    tui = "enabled" if has_tui() else "not installed (pip install code-recall[tui])"
    print(f"  Semantic: {sem}")
    print(f"  TUI: {tui}")


def _cmd_gc(args: argparse.Namespace) -> None:
    """Remove orphaned index entries for deleted session files."""
    if not args.db.exists():
        print("No index found.")
        return

    from code_recall.db import delete_session

    conn = get_connection(args.db)
    rows = conn.execute("SELECT session_id, file_path FROM sessions").fetchall()

    removed = 0
    for row in rows:
        if not Path(row["file_path"]).exists():
            delete_session(conn, row["session_id"])
            removed += 1

    conn.commit()
    conn.close()
    print(f"Removed {removed} orphaned entries")


def _cmd_install_hooks() -> None:
    """Install Claude Code SessionEnd hook for automatic index updates."""
    import shutil

    settings_path = Path.home() / ".claude" / "settings.json"
    code_recall_bin = shutil.which(COMMAND_NAME)

    if not code_recall_bin:
        print(f"Warning: '{COMMAND_NAME}' not found in PATH.")
        code_recall_bin = COMMAND_NAME

    hook_command = f"{code_recall_bin} index --quiet"
    desired_hook = _index_hook_config(hook_command)

    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    hooks = settings.get("hooks", {})
    session_end_hooks = hooks.get("SessionEnd", [])

    for rule in session_end_hooks:
        for hook in rule.get("hooks", []):
            if _hook_mentions_app(hook.get("command", "")):
                hook.update(desired_hook)
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=2)
                print("Hook already installed; refreshed config.")
                print(f"  Command: {desired_hook['command']}")
                return

    new_hook = {
        "hooks": [desired_hook]
    }
    session_end_hooks.append(new_hook)
    hooks["SessionEnd"] = session_end_hooks
    settings["hooks"] = hooks

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    HOOKS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_MARKER.touch()

    print("Installed Claude Code SessionEnd hook!")
    print(f"  Settings: {settings_path}")
    print(f"  Command: {hook_command}")
    print()
    print("Index will auto-update when you exit Claude Code sessions.")


def _index_hook_config(command: str) -> dict:
    """Return Claude Code hook config for non-blocking index updates."""
    return {
        "type": "command",
        "command": command,
        "timeout": 30,
        "async": True,
    }


def _hook_mentions_app(command: str) -> bool:
    return COMMAND_NAME in command or any(name in command for name in LEGACY_COMMAND_NAMES)


if __name__ == "__main__":
    main()
