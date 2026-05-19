"""LLM-based reranking using an available assistant CLI."""

from __future__ import annotations

import json
import subprocess
import sys


def llm_rerank(
    query: str,
    candidates: list[dict],
    model: str | None = None,
    top_k: int = 10,
    assistant_provider: str = "auto",
    preferred_provider: str | None = None,
) -> list[int]:
    """Rerank search candidates using Claude Code or Codex.

    Sends the query + candidate summaries to an available assistant and asks it
    to rank them by relevance. Returns ordered list of candidate indices.

    Args:
        query: User's search query
        candidates: List of dicts with session_id, summary, first_prompt, etc.
        model: Optional model override for the selected assistant
        top_k: Number of top results to return

    Returns:
        List of original candidate indices, ordered by relevance.
        Original order if no supported assistant is available.
    """
    from code_recall.agentic import _assistant_command, _select_assistant

    assistant = _select_assistant(assistant_provider, preferred_provider, [])
    if assistant is None:
        return list(range(min(top_k, len(candidates))))

    # Build compact candidate descriptions
    candidate_text = ""
    for i, c in enumerate(candidates):
        summary = c.get("summary") or c.get("first_prompt") or "No description"
        summary = summary[:150]
        project = c.get("project_path", "").split("/")[-1] or "unknown"
        msgs = c.get("message_count", 0)
        last = (c.get("last_prompt") or "")[:100]

        candidate_text += f"[{i}] {summary}\n"
        candidate_text += f"    Project: {project} | {msgs} msgs\n"
        if last:
            candidate_text += f"    Last: {last}\n"

    prompt = f"""You are ranking coding-agent session search results by relevance.

The user is trying to find a past Claude Code or Codex conversation. They might use project names, topics, or partial descriptions. A "session" is a conversation with a coding agent about a coding task.

Query: "{query}"

Candidates:
{candidate_text}

Rank ALL candidates by relevance to the query. Return a JSON array of ALL indices ordered from most to least relevant.
Consider: project names, topic similarity, keywords in summary/description.
Even partial matches should be included — the user may not remember exact terms.

Return ONLY a JSON array, e.g. [3, 0, 7, 1, 2]. No explanation."""

    try:
        result = subprocess.run(
            _assistant_command(assistant[0], assistant[1], model, use_read_tools=False),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return list(range(min(top_k, len(candidates))))

        # Parse the JSON array from output
        output = result.stdout.strip()
        # Find the JSON array in the output (Claude might add text around it)
        start = output.find("[")
        end = output.rfind("]") + 1
        if start >= 0 and end > start:
            indices = json.loads(output[start:end])
            # Validate indices
            valid = [i for i in indices if isinstance(i, int) and 0 <= i < len(candidates)]
            return valid[:top_k]

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"LLM rerank failed: {e}", file=sys.stderr)

    return list(range(min(top_k, len(candidates))))
