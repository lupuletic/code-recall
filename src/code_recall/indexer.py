"""Session indexer for code-recall."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from code_recall import has_semantic
from code_recall.db import (
    DB_PATH,
    build_session_chains,
    delete_session,
    get_all_session_ids,
    get_connection,
    get_session_mtime,
    setup_vec_table,
    upsert_chunks,
    upsert_graph_edges,
    upsert_session,
    upsert_session_commands,
    upsert_session_files,
)
from code_recall.models import Session
from code_recall.utils import (
    CODEX_DIR,
    PROJECTS_DIR,
    decode_project_path,
    discover_codex_sessions,
    discover_sessions,
    load_sessions_index,
    parse_codex_session_file,
    parse_session_file,
)



def _mtime_to_iso(mtime: float) -> str:
    """Convert a file mtime to ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def build_index(
    projects_dir: Path = PROJECTS_DIR,
    db_path: Path = DB_PATH,
    force: bool = False,
    verbose: bool = True,
    defer_embeddings: bool = False,
    codex_dir: Path | None = None,
) -> dict:
    """Build or update the session index.

    Returns stats dict with counts of indexed/skipped/removed sessions.
    """
    start = time.monotonic()
    conn = get_connection(db_path)

    # Set up vector table if semantic deps available
    if has_semantic():
        setup_vec_table(conn)

    # Discover all session files on disk
    discovered = discover_sessions(projects_dir)
    if codex_dir is not None:
        discovered.extend(discover_codex_sessions(codex_dir))

    if verbose:
        print(f"Found {len(discovered)} session files", file=sys.stderr)

    # Load existing index state
    existing_ids = get_all_session_ids(conn) if not force else set()

    # Cache sessions-index.json data per project
    index_cache: dict[str, dict[str, dict]] = {}

    # Pre-build set of session IDs that are parents of subagents
    parent_ids = {
        s["parent_session"]
        for s in discovered
        if s.get("is_subagent") and s.get("parent_session")
    }

    indexed = 0
    skipped = 0
    errors = 0

    for i, session_info in enumerate(discovered):
        session_id = session_info["session_id"]
        project_dir = session_info["project_dir"]
        file_mtime = session_info["mtime"]
        provider = session_info.get("provider", "claude")

        # Skip if already indexed and file hasn't changed
        if not force and session_id in existing_ids:
            stored_mtime = get_session_mtime(conn, session_id)
            if stored_mtime is not None and abs(stored_mtime - file_mtime) < 0.01:
                skipped += 1
                continue

        # Load provider metadata
        if provider == "codex":
            idx_meta = session_info
        else:
            if project_dir not in index_cache:
                index_cache[project_dir] = load_sessions_index(project_dir, projects_dir)
            idx_meta = index_cache[project_dir].get(session_id, {})

        # Parse the session file
        try:
            if provider == "codex":
                parsed = parse_codex_session_file(session_info["file_path"], idx_meta)
            else:
                parsed = parse_session_file(session_info["file_path"])
        except Exception:
            errors += 1
            continue

        # Skip sessions with no user messages — UNLESS they're parents of subagents
        if parsed["message_count"] == 0 and session_id not in parent_ids:
            skipped += 1
            continue

        # Decode project path
        if provider == "codex":
            project_path = session_info.get("project_path") or project_dir
        else:
            project_path = decode_project_path(project_dir, projects_dir)

        # Use the most descriptive title available, preferring Claude's AI title
        # over the first-prompt fallback when sessions-index has no summary.
        summary = idx_meta.get("summary") or parsed.get("ai_title") or parsed.get("summary")

        import json as _json

        # Append touched file names to messages_text so they're FTS-searchable
        messages_text = parsed["messages_text"]
        file_names = parsed.get("files_touched") or parsed.get("files_modified", [])
        if file_names:
            messages_text += "\n" + " ".join(file_names)

        session = Session(
            session_id=session_id,
            provider=provider,
            provider_session_id=session_info.get("provider_session_id", session_id),
            project_path=project_path,
            project_dir=project_dir,
            file_path=session_info["file_path"],
            summary=summary,
            first_prompt=parsed["first_prompt"],
            first_reply=parsed["first_reply"],
            last_prompt=parsed["last_prompt"],
            last_reply=parsed["last_reply"],
            messages_text=messages_text,
            git_branch=idx_meta.get("gitBranch") or parsed.get("git_branch_detected"),
            files_modified=_json.dumps(parsed.get("files_touched") or parsed.get("files_modified", [])),
            commands_run=_json.dumps(parsed.get("commands_run", [])),
            message_count=parsed["message_count"],
            file_size=session_info["file_size"],
            created=idx_meta.get("created") or parsed.get("first_activity") or _mtime_to_iso(file_mtime),
            modified=idx_meta.get("modified") or parsed.get("last_activity") or _mtime_to_iso(file_mtime),
            last_activity=parsed.get("last_activity") or idx_meta.get("modified") or _mtime_to_iso(file_mtime),
            mtime=file_mtime,
            is_subagent=session_info["is_subagent"],
            parent_session=session_info["parent_session"],
            model=idx_meta.get("model"),
        )

        upsert_session(conn, session)
        upsert_chunks(conn, session_id, parsed["chunks"])

        # Build graph edges and normalized tables from tool calls
        graph_edges = []
        file_records = []

        for file_info in parsed.get("file_actions", []):
            file_path = file_info.get("path", "")
            if not file_path:
                continue
            action = file_info.get("action", "read")
            file_name = file_path.split("/")[-1]
            file_records.append({"path": file_path, "name": file_name, "action": action})
            graph_edges.append({
                "src_type": "session", "src_name": session_id,
                "dst_type": "file", "dst_name": file_path,
                "rel": "edited" if action == "edit" else "read",
            })

        cmd_records = []
        for cmd in parsed.get("commands_run", []):
            cmd_name = cmd.split()[0] if cmd.split() else cmd
            cmd_records.append({"command": cmd[:200], "command_name": cmd_name})
            graph_edges.append({
                "src_type": "session", "src_name": session_id,
                "dst_type": "command", "dst_name": cmd_name,
                "rel": "ran",
            })

        upsert_session_files(conn, session_id, file_records)
        upsert_session_commands(conn, session_id, cmd_records)
        upsert_graph_edges(conn, session_id, graph_edges)

        indexed += 1

        # Commit in batches so a kill doesn't lose everything
        if indexed % 100 == 0:
            conn.commit()

        # Progress indicator
        if verbose and (indexed % 50 == 0 or i == len(discovered) - 1):
            print(
                f"\r  Indexed {indexed} sessions ({skipped} unchanged, {errors} errors)...",
                end="",
                file=sys.stderr,
            )

    # Enrich parent sessions with subagent content
    # This ensures searching for terms that only appear in subagents
    # still finds the parent session
    if indexed > 0:
        _enrich_parents_with_subagent_content(conn, verbose)

    # Build session chains (group related sessions by project/branch/time)
    if indexed > 0:
        try:
            build_session_chains(conn)
        except Exception:
            pass  # Non-critical — don't fail indexing

    # Generate embeddings if semantic is available (unless deferred)
    embeddings_generated = 0
    if has_semantic() and indexed > 0 and not defer_embeddings:
        embeddings_generated = _generate_embeddings(conn, force, verbose)

    # Remove sessions that no longer exist on disk
    discovered_ids = {s["session_id"] for s in discovered}
    removed = 0
    for old_id in existing_ids - discovered_ids:
        if codex_dir is None and old_id.startswith("codex:"):
            continue
        delete_session(conn, old_id)
        removed += 1

    conn.commit()

    elapsed = time.monotonic() - start

    if verbose:
        print(file=sys.stderr)  # newline after progress
        print(
            f"  Done in {elapsed:.1f}s: {indexed} indexed, "
            f"{skipped} unchanged, {removed} removed, {errors} errors",
            file=sys.stderr,
        )
        if embeddings_generated:
            print(
                f"  Generated {embeddings_generated} embeddings",
                file=sys.stderr,
            )

    conn.close()

    return {
        "indexed": indexed,
        "skipped": skipped,
        "removed": removed,
        "errors": errors,
        "embeddings": embeddings_generated,
        "elapsed": elapsed,
        "total_discovered": len(discovered),
    }


def ensure_index(
    projects_dir: Path = PROJECTS_DIR,
    db_path: Path = DB_PATH,
    codex_dir: Path | None = CODEX_DIR,
    verbose: bool = True,
) -> None:
    """Ensure the index exists and is reasonably up-to-date.

    Called automatically before search. Builds FTS index immediately,
    defers embedding generation to a background process on first run.
    """
    is_first_run = not db_path.exists()

    if is_first_run:
        if verbose:
            print("Building index for the first time...", file=sys.stderr)
        # Build FTS index immediately (fast, ~2s), skip embeddings
        build_index(
            projects_dir=projects_dir, codex_dir=codex_dir, db_path=db_path, force=False, verbose=verbose,
            defer_embeddings=True,
        )
        # Generate embeddings in background
        if has_semantic():
            _spawn_background_embeddings(db_path, projects_dir, codex_dir, verbose)
    else:
        # Quick incremental check — always defer embeddings (only generate during explicit `index`)
        try:
            build_index(
                projects_dir=projects_dir, codex_dir=codex_dir, db_path=db_path, force=False, verbose=False,
                defer_embeddings=True,
            )
        except Exception:
            pass  # DB locked by background embeddings — search with existing index


def _spawn_background_embeddings(
    db_path: Path,
    projects_dir: Path,
    codex_dir: Path | None,
    verbose: bool,
) -> None:
    """Spawn a background process to generate embeddings."""
    import subprocess as sp

    if verbose:
        print(
            "  Generating embeddings in background (search works now with keywords)...",
            file=sys.stderr,
        )

    # Run `code-recall index --quiet` in background, preserving custom paths
    import shutil

    code_recall_bin = shutil.which("code-recall")
    base_cmd = [code_recall_bin, "index", "--quiet"] if code_recall_bin else \
               [sys.executable, "-m", "code_recall", "index", "--quiet"]

    # Pass custom paths so the background process uses the same DB/source
    if db_path != DB_PATH:
        base_cmd.extend(["--db", str(db_path)])
    if projects_dir != PROJECTS_DIR:
        base_cmd.extend(["--claude-dir", str(projects_dir)])
    if codex_dir is None:
        base_cmd.append("--no-codex")
    elif codex_dir != CODEX_DIR:
        base_cmd.extend(["--codex-dir", str(codex_dir)])

    sp.Popen(
        base_cmd,
        stdout=sp.DEVNULL,
        stderr=sp.DEVNULL,
        start_new_session=True,
    )


def _enrich_parents_with_subagent_content(conn, verbose: bool = False) -> None:
    """Append subagent first_prompts to parent session messages_text.

    This ensures that searching for terms only used in subagent sessions
    (e.g. project names, specific tools) still finds the parent session.

    Uses a marker to strip previous enrichment before re-adding, preventing
    duplication on incremental runs.
    """
    MARKER = "\n--- SUBAGENT CONTENT ---\n"

    # Strip old enrichment from all parents before re-adding
    conn.execute(
        """UPDATE sessions
           SET messages_text = SUBSTR(messages_text, 1,
               CASE WHEN INSTR(messages_text, ?) > 0
               THEN INSTR(messages_text, ?) - 1
               ELSE LENGTH(messages_text) END)
           WHERE is_subagent = 0""",
        (MARKER, MARKER),
    )

    rows = conn.execute(
        """SELECT s.session_id AS sub_id, s.parent_session, s.first_prompt
           FROM sessions s
           WHERE s.is_subagent = 1 AND s.parent_session IS NOT NULL
           AND s.first_prompt IS NOT NULL"""
    ).fetchall()

    if not rows:
        conn.commit()
        return

    # Group subagent prompts by parent
    parent_extras: dict[str, list[str]] = {}
    for row in rows:
        parent_id = row["parent_session"]
        prompt = row["first_prompt"]
        if prompt and prompt.strip():
            parent_extras.setdefault(parent_id, []).append(prompt[:200])

    enriched = 0
    for parent_id, extras in parent_extras.items():
        extra_text = MARKER + "\n".join(extras)
        # Append to the parent's messages_text
        conn.execute(
            """UPDATE sessions
               SET messages_text = COALESCE(messages_text, '') || ?
               WHERE session_id = ? AND is_subagent = 0""",
            (extra_text, parent_id),
        )
        enriched += 1

    if enriched:
        conn.commit()
        # Rebuild FTS for enriched parents
        conn.execute("INSERT INTO sessions_fts(sessions_fts) VALUES('rebuild')")
        conn.commit()

    if verbose and enriched:
        print(f"\n  Enriched {enriched} parent sessions with subagent content", file=sys.stderr)


def _generate_embeddings(
    conn, force: bool = False, verbose: bool = True
) -> int:
    """Generate embeddings for sessions that don't have them yet."""
    try:
        from code_recall.embedder import get_embedder
    except ImportError:
        return 0

    embedder = get_embedder()
    if embedder is None:
        return 0

    from code_recall.db import load_vec_extension

    load_vec_extension(conn)

    # Find chunks needing embeddings (chunks without vec entries)
    if force:
        conn.execute("DELETE FROM chunks_vec")
        rows = conn.execute(
            "SELECT chunk_id, chunk_text FROM chunks"
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.chunk_id, c.chunk_text
               FROM chunks c
               WHERE c.chunk_id NOT IN (
                   SELECT chunk_rowid FROM chunks_vec
               )"""
        ).fetchall()

    if not rows:
        return 0

    # Prepare texts and IDs
    texts = [row["chunk_text"] for row in rows if row["chunk_text"].strip()]
    chunk_ids = [row["chunk_id"] for row in rows if row["chunk_text"].strip()]

    if not texts:
        return 0

    # Embed in mini-batches so the user sees steady progress; one giant
    # batched call produces a long silent stretch on heavier models.
    batch_size = 64
    use_tqdm = verbose and sys.stderr.isatty()
    progress = None
    if use_tqdm:
        try:
            from tqdm import tqdm

            progress = tqdm(
                total=len(texts),
                unit="chunk",
                desc="  Embedding",
                file=sys.stderr,
                leave=True,
            )
        except ImportError:
            progress = None
    elif verbose:
        print(f"\n  Generating embeddings for {len(texts)} chunks...", file=sys.stderr)

    embedded = 0
    for start in range(0, len(texts), batch_size):
        batch_texts = texts[start : start + batch_size]
        batch_ids = chunk_ids[start : start + batch_size]
        batch_embeddings = embedder.embed(batch_texts)
        for chunk_id, embedding in zip(batch_ids, batch_embeddings):
            conn.execute(
                "INSERT OR REPLACE INTO chunks_vec (chunk_rowid, embedding) VALUES (?, ?)",
                (chunk_id, embedding.tobytes()),
            )
            embedded += 1
        conn.commit()
        if progress is not None:
            progress.update(len(batch_texts))
        elif verbose:
            print(
                f"\r  Embedded {embedded}/{len(texts)} chunks...",
                end="",
                file=sys.stderr,
            )

    if progress is not None:
        progress.close()
    elif verbose:
        print(file=sys.stderr)

    return len(chunk_ids)
