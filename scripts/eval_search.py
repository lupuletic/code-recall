#!/usr/bin/env python3
"""Run code-recall search quality evals."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from code_recall.db import DB_PATH
from code_recall.eval import generate_eval_cases, load_eval_cases, run_eval_cases, write_eval_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate search recall against JSON cases.")
    parser.add_argument("cases", type=Path, nargs="?", help="Path to eval JSON")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Index database path")
    parser.add_argument("-n", "--limit", type=int, default=None, help="Search limit per query")
    parser.add_argument("--semantic", action="store_true", default=None, help="Force semantic search")
    parser.add_argument("--no-semantic", action="store_true", help="Disable semantic search")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON report")
    parser.add_argument("--generate", type=Path, help="Write generated eval cases to this path")
    parser.add_argument("--min-messages", type=int, default=3, help="Minimum messages for generated cases")
    args = parser.parse_args()

    semantic = True if args.semantic else None
    if args.no_semantic:
        semantic = False

    if args.generate:
        cases = generate_eval_cases(
            db_path=args.db,
            limit=args.limit or 30,
            min_messages=args.min_messages,
        )
        write_eval_cases(cases, args.generate)
        print(f"Wrote {len(cases)} eval cases to {args.generate}")
        return 0

    if args.cases is None:
        parser.error("cases is required unless --generate is used")

    cases = load_eval_cases(args.cases)
    report = run_eval_cases(cases, db_path=args.db, limit=args.limit, semantic=semantic)

    if args.json_output:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0 if report["passed"] == report["total"] else 1

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
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
