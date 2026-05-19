"""Generate sanitized README screenshots from synthetic code-recall data.

The screenshots in docs/assets must never be captured from a real developer
index. This script builds a temporary SQLite index with fake projects and fake
transcripts, renders the TUI with Textual, and writes SVG assets for the README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from code_recall.db import (
    build_session_chains,
    get_connection,
    upsert_chunks,
    upsert_graph_edges,
    upsert_session,
    upsert_session_commands,
    upsert_session_files,
)
from code_recall.models import SearchResult, Session
from code_recall.searcher import search
from code_recall.tui import DetailPanel, RecallApp


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DIR = ROOT / "docs" / "assets"
DEMO_QUERY = "stripe webhook signature"


@dataclass(frozen=True)
class DemoSession:
    session_id: str
    provider: str
    project_dir: str
    summary: str
    first_prompt: str
    last_prompt: str
    messages: str
    branch: str
    created: str
    files: tuple[str, ...]
    commands: tuple[str, ...]
    model: str


DEMO_SESSIONS: tuple[DemoSession, ...] = (
    DemoSession(
        session_id="demo-claude-001",
        provider="claude",
        project_dir="payments-api",
        summary="Fix Stripe webhook signature verification",
        first_prompt="Production webhooks are failing signature verification after a framework upgrade.",
        last_prompt="Added raw body parsing and replay tests for Stripe webhook signature checks.",
        messages=(
            "Debugged Stripe webhook signature validation. The middleware parsed JSON before "
            "constructing the event, so the signed payload bytes no longer matched. Updated "
            "src/webhooks/stripe.ts to use the raw request body, added timestamp tolerance, "
            "and covered valid, tampered, and replayed signatures in tests."
        ),
        branch="fix/stripe-webhook-signature",
        created="2026-05-18T09:15:00Z",
        files=(
            "src/webhooks/stripe.ts",
            "src/routes/webhooks.ts",
            "tests/webhooks/stripe-signature.test.ts",
        ),
        commands=(
            "pnpm test tests/webhooks/stripe-signature.test.ts",
            "stripe listen --forward-to localhost:3000/api/webhooks/stripe",
        ),
        model="claude-sonnet-4",
    ),
    DemoSession(
        session_id="demo-codex-002",
        provider="codex",
        project_dir="payments-api",
        summary="Debug checkout webhook retry handling",
        first_prompt="Find why checkout webhooks are being processed twice during retry storms.",
        last_prompt="Made webhook handling idempotent and documented the retry behavior.",
        messages=(
            "Investigated checkout webhook retries and duplicate payment events. Added an "
            "idempotency key check, stored webhook event ids, and verified the Stripe CLI retry "
            "flow against the local webhook endpoint."
        ),
        branch="fix/stripe-webhook-signature",
        created="2026-05-18T11:05:00Z",
        files=(
            "src/webhooks/stripe.ts",
            "src/payments/idempotency.ts",
            "tests/webhooks/retry-handling.test.ts",
        ),
        commands=(
            "pnpm test tests/webhooks/retry-handling.test.ts",
            "pnpm lint src/webhooks/stripe.ts",
        ),
        model="gpt-5.1-codex",
    ),
    DemoSession(
        session_id="demo-claude-003",
        provider="claude",
        project_dir="customer-portal",
        summary="Add OAuth callback state validation",
        first_prompt="Harden OAuth callback handling and verify state validation.",
        last_prompt="Rejected missing state and added callback regression tests.",
        messages=(
            "Reviewed the OAuth callback controller, added state nonce validation, rotated "
            "the session cookie after login, and tested invalid callback parameters."
        ),
        branch="security/oauth-state",
        created="2026-05-17T16:20:00Z",
        files=("src/auth/oauth-callback.ts", "tests/auth/oauth-callback.test.ts"),
        commands=("pnpm test tests/auth/oauth-callback.test.ts",),
        model="claude-sonnet-4",
    ),
    DemoSession(
        session_id="demo-codex-004",
        provider="codex",
        project_dir="customer-portal",
        summary="Investigate slow account dashboard Prisma query",
        first_prompt="The account dashboard is slow for customers with many invoices.",
        last_prompt="Added a covering index and replaced nested includes with a paginated query.",
        messages=(
            "Profiled Prisma queries for the account dashboard. The expensive include pulled all "
            "invoice rows and line items. Replaced it with a paginated summary query and migration."
        ),
        branch="perf/dashboard-query",
        created="2026-05-16T14:10:00Z",
        files=("src/dashboard/account-query.ts", "prisma/migrations/20260516_add_invoice_index.sql"),
        commands=("pnpm prisma migrate dev", "pnpm test tests/dashboard/account-query.test.ts"),
        model="gpt-5.1-codex",
    ),
    DemoSession(
        session_id="demo-claude-005",
        provider="claude",
        project_dir="infra-deploy",
        summary="Repair Docker healthcheck for worker deploy",
        first_prompt="The worker service rolls back because its container healthcheck never passes.",
        last_prompt="Changed the healthcheck to hit the worker status endpoint and updated compose.",
        messages=(
            "Traced the deploy failure to a healthcheck that assumed an HTTP server on the wrong "
            "port. Added a lightweight status endpoint and updated docker-compose healthcheck."
        ),
        branch="deploy/worker-healthcheck",
        created="2026-05-15T10:40:00Z",
        files=("Dockerfile.worker", "docker-compose.yml", "src/worker/status.ts"),
        commands=("docker compose up worker", "docker inspect demo-worker-1"),
        model="claude-sonnet-4",
    ),
    DemoSession(
        session_id="demo-codex-006",
        provider="codex",
        project_dir="mobile-checkout",
        summary="Trace session cookie expiry bug",
        first_prompt="Users are being logged out after returning from mobile checkout.",
        last_prompt="Aligned cookie max age with refresh token rotation and added e2e coverage.",
        messages=(
            "Compared the mobile checkout callback, session cookie settings, and refresh token "
            "rotation. Fixed mismatched max age and added a regression test for returning users."
        ),
        branch="fix/mobile-session-expiry",
        created="2026-05-14T13:25:00Z",
        files=("src/session/cookies.ts", "tests/e2e/mobile-checkout.spec.ts"),
        commands=("pnpm playwright test tests/e2e/mobile-checkout.spec.ts",),
        model="gpt-5.1-codex",
    ),
    DemoSession(
        session_id="demo-claude-007",
        provider="claude",
        project_dir="release-tools",
        summary="Set up GitHub Actions release workflow",
        first_prompt="Create a tagged release workflow that builds wheels and publishes to PyPI.",
        last_prompt="Added trusted publishing config and release validation commands.",
        messages=(
            "Created a release workflow triggered by v* tags, added build verification, and "
            "documented the PyPI trusted publisher settings."
        ),
        branch="ci/release-workflow",
        created="2026-05-13T09:30:00Z",
        files=(".github/workflows/release.yml", "pyproject.toml", "README.md"),
        commands=("uv build --no-sources", "uv run pytest -q"),
        model="claude-sonnet-4",
    ),
    DemoSession(
        session_id="demo-codex-008",
        provider="codex",
        project_dir="media-service",
        summary="Compare image upload costs across providers",
        first_prompt="Estimate the monthly cost difference between two image upload providers.",
        last_prompt="Added a cost estimator and documented break-even assumptions.",
        messages=(
            "Built a small estimator for upload volume, transform count, and storage tiers. "
            "Documented assumptions and compared provider pricing with configurable inputs."
        ),
        branch="analysis/image-upload-costs",
        created="2026-05-12T15:45:00Z",
        files=("src/costs/image-upload-estimator.ts", "docs/image-upload-costs.md"),
        commands=("pnpm tsx src/costs/image-upload-estimator.ts",),
        model="gpt-5.1-codex",
    ),
)


def _session_to_model(item: DemoSession) -> Session:
    project_path = f"/Users/demo/Projects/{item.project_dir}"
    transcript_path = f"/Users/demo/.code-agent-history/{item.session_id}.jsonl"
    provider_session_id = f"{item.provider}-{item.session_id}"
    modified = item.created.replace("T", " ").replace("Z", "")
    return Session(
        session_id=item.session_id,
        provider=item.provider,
        provider_session_id=provider_session_id,
        project_path=project_path,
        project_dir=item.project_dir,
        file_path=transcript_path,
        summary=item.summary,
        first_prompt=item.first_prompt,
        first_reply=f"I inspected {item.project_dir} and identified the relevant files.",
        last_prompt=item.last_prompt,
        last_reply="Ready to resume from the current branch with tests passing.",
        messages_text=item.messages,
        git_branch=item.branch,
        message_count=18 + len(item.files) + len(item.commands),
        file_size=512_000 + len(item.messages) * 120,
        created=item.created,
        modified=item.created,
        last_activity=item.created,
        mtime=1_779_000_000.0,
        files_modified=json.dumps(list(item.files)),
        commands_run=json.dumps(list(item.commands)),
        git_branch_detected=item.branch,
        model=item.model,
    )


def build_demo_db(db_path: Path) -> None:
    """Create a temporary demo index with fake sessions only."""
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    conn = get_connection(db_path)
    try:
        for item in DEMO_SESSIONS:
            session = _session_to_model(item)
            upsert_session(conn, session)
            upsert_chunks(
                conn,
                session.session_id,
                [
                    item.first_prompt,
                    item.messages,
                    item.last_prompt,
                ],
            )
            files = [
                {
                    "path": file_path,
                    "name": Path(file_path).name,
                    "action": "edit" if index == 0 else "read",
                }
                for index, file_path in enumerate(item.files)
            ]
            upsert_session_files(conn, session.session_id, files)
            upsert_session_commands(
                conn,
                session.session_id,
                [
                    {"command": command, "command_name": command.split()[0]}
                    for command in item.commands
                ],
            )
            upsert_graph_edges(
                conn,
                session.session_id,
                [
                    {
                        "src_type": "session",
                        "src_name": session.session_id,
                        "dst_type": "file",
                        "dst_name": file_path,
                        "rel": "edited" if index == 0 else "read",
                    }
                    for index, file_path in enumerate(item.files)
                ],
            )
        build_session_chains(conn)
        conn.commit()
    finally:
        conn.close()


async def _render_screenshot(
    *,
    db_path: Path,
    results: list[SearchResult],
    asset_dir: Path,
    filename: str,
    tab: str | None = None,
    chat_messages: list[tuple[str, str]] | None = None,
) -> None:
    from code_recall import tui as tui_module

    # Keep assistant labels deterministic when rendering on CI or contributors'
    # machines where only one provider CLI may be installed.
    tui_module.shutil.which = lambda name: f"/usr/local/bin/{name}" if name in {"claude", "codex"} else None

    app = RecallApp(initial_query=DEMO_QUERY, initial_results=results, db_path=db_path)
    async with app.run_test(size=(158, 42)) as pilot:
        await pilot.pause()
        if tab:
            app.action_detail_tab(tab)
            await pilot.pause()
        if chat_messages:
            app._ai_chats[results[0].session.session_id] = chat_messages
            app.query_one("#detail", DetailPanel).set_ai_chat(
                chat_messages,
                busy=False,
                assistant_label="Claude Code",
            )
            await pilot.pause()
        app.save_screenshot(filename=filename, path=str(asset_dir))


async def render_assets(asset_dir: Path) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="code-recall-demo-") as tmp:
        db_path = Path(tmp) / "demo-index.db"
        build_demo_db(db_path)
        results = search(DEMO_QUERY, db_path=db_path, limit=10, semantic=False)
        if not results:
            raise RuntimeError(f"demo query returned no results: {DEMO_QUERY!r}")

        await _render_screenshot(
            db_path=db_path,
            results=results,
            asset_dir=asset_dir,
            filename="code-recall-search.svg",
        )
        await _render_screenshot(
            db_path=db_path,
            results=results,
            asset_dir=asset_dir,
            filename="code-recall-why.svg",
            tab="why",
        )
        await _render_screenshot(
            db_path=db_path,
            results=results,
            asset_dir=asset_dir,
            filename="code-recall-ai-chat.svg",
            tab="ai",
            chat_messages=[
                ("user", "What changed and what should I verify before resuming?"),
                (
                    "assistant",
                    "Session demo-claude-001 switched Stripe verification to the raw request "
                    "body, added timestamp tolerance, and covered valid, tampered, and replayed "
                    "signatures. Resume with `claude --resume claude-demo-claude-001`, then run "
                    "`pnpm test tests/webhooks/stripe-signature.test.ts` and the Stripe CLI "
                    "forwarding command.",
                ),
            ],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=DEFAULT_ASSET_DIR,
        help="Directory where SVG screenshots should be written.",
    )
    args = parser.parse_args()
    asyncio.run(render_assets(args.asset_dir))
    print(f"Generated demo screenshots in {args.asset_dir}")


if __name__ == "__main__":
    main()
