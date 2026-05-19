"""Tests for search recall eval helpers."""

from __future__ import annotations

import json

from code_recall.db import get_connection, upsert_session
from code_recall.db import upsert_session_commands, upsert_session_files
from code_recall.eval import generate_eval_cases, load_eval_cases, run_eval_cases, write_eval_cases
from code_recall.models import Session


def test_load_eval_cases_from_json_object(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({
        "cases": [
            {
                "query": "checkout webhook",
                "expected_project": "payments",
                "expected_terms": ["webhook"],
                "top_k": 5,
            }
        ]
    }))

    cases = load_eval_cases(path)

    assert len(cases) == 1
    assert cases[0].query == "checkout webhook"
    assert cases[0].top_k == 5


def test_run_eval_cases_checks_top_k(db_path):
    conn = get_connection(db_path)
    upsert_session(conn, Session(
        session_id="session-payments",
        project_path="/Users/test/payments",
        project_dir="-Users-test-payments",
        file_path="/tmp/session-payments.jsonl",
        summary="Checkout webhook validation",
        first_prompt="Debug checkout webhook signature handling",
        messages_text="validate_signature webhook handler Stripe checkout",
        message_count=4,
        file_size=1024,
        is_subagent=False,
    ))
    conn.commit()
    conn.close()

    report = run_eval_cases([
        load_eval_cases_from_dict({
            "query": "where did we fix webhook validation",
            "expected_session_id": "session-payments",
            "top_k": 3,
        })
    ], db_path=db_path, semantic=False)

    assert report["passed"] == 1
    assert report["total"] == 1


def test_run_eval_cases_requires_expected_session_not_just_terms(db_path):
    conn = get_connection(db_path)
    upsert_session(conn, Session(
        session_id="session-a",
        project_path="/Users/test/shop-a",
        project_dir="-Users-test-shop-a",
        file_path="/tmp/session-a.jsonl",
        summary="Checkout webhook validation",
        first_prompt="Debug checkout webhook validation",
        messages_text="checkout webhook validation",
        message_count=4,
        file_size=1024,
        is_subagent=False,
    ))
    upsert_session(conn, Session(
        session_id="session-b",
        project_path="/Users/test/shop-b",
        project_dir="-Users-test-shop-b",
        file_path="/tmp/session-b.jsonl",
        summary="Checkout webhook validation",
        first_prompt="Debug checkout webhook validation",
        messages_text="checkout webhook validation",
        message_count=4,
        file_size=1024,
        is_subagent=False,
    ))
    conn.commit()
    conn.close()

    case = load_eval_cases_from_dict({
        "query": "checkout webhook validation",
        "expected_session_id": "missing-session",
        "expected_terms": ["checkout", "webhook"],
        "top_k": 3,
    })
    report = run_eval_cases([case], db_path=db_path, semantic=False)

    assert report["passed"] == 0


def load_eval_cases_from_dict(data):
    path_data = {"cases": [data]}
    from code_recall.eval import EvalCase

    return EvalCase.from_dict(path_data["cases"][0])


def test_generate_eval_cases_uses_session_ids(db_path):
    conn = get_connection(db_path)
    upsert_session(conn, Session(
        session_id="session-checkout",
        project_path="/Users/test/shop",
        project_dir="-Users-test-shop",
        file_path="/tmp/session-checkout.jsonl",
        summary="Fix checkout webhook validation",
        first_prompt="Debug checkout webhook signature handling",
        messages_text="validate_signature webhook handler Stripe checkout",
        message_count=8,
        file_size=1024,
        is_subagent=False,
    ))
    upsert_session_files(conn, "session-checkout", [
        {"path": "/repo/src/webhook_handler.py", "name": "webhook_handler.py", "action": "read"},
    ])
    upsert_session_commands(conn, "session-checkout", [
        {"command": "pytest tests/test_webhook.py", "command_name": "pytest"},
    ])
    conn.commit()
    conn.close()

    cases = generate_eval_cases(db_path=db_path, limit=5)

    assert cases
    assert all(case["expected_session_id"] == "session-checkout" for case in cases)
    assert any("webhook_handler.py" in case["query"] for case in cases)
    assert any("pytest" in case["query"] for case in cases)


def test_write_eval_cases_round_trips(tmp_path):
    path = tmp_path / "evals" / "cases.json"
    write_eval_cases([
        {"query": "checkout", "expected_session_id": "s1", "top_k": 3}
    ], path)

    cases = load_eval_cases(path)

    assert cases[0].query == "checkout"
    assert cases[0].expected_session_id == "s1"
