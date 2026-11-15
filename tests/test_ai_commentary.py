"""AI commentary: fact sheet, guardrail, retry/fallback. No model needed:
the LLM is replaced by scripted responses."""

from datetime import date

import pytest
import requests
import responses

from opsreport.ai import commentary as ai
from opsreport.pipeline import run

AS_OF, PRIOR = date(2026, 11, 13), date(2026, 11, 6)


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    return run(AS_OF, PRIOR, out_dir=tmp_path_factory.mktemp("out")).data


@pytest.fixture(scope="module")
def facts(data):
    return ai.build_facts(data)


# Real drafts from llama3.2 plus hand-written cases. (text, should_pass)
CASES = [
    # --- must be rejected
    ("There are three high-materiality reconciliation breaks totaling $14.9M.", False),
    ("The team with the most overdue tickets is Reconciliation with 7 open tickets.", False),
    ("ISC AUM fell 1.3% week over week.", False),          # GEF's number on ISC
    ("Total AUM was $352.0M.", False),                     # invented
    ("There are 19 overdue tickets.", False),              # 19 is open, not overdue
    ("OPSREC-999 is the oldest ticket.", False),           # ticket doesn't exist
    # --- must be accepted
    ("Reconciliation breaks total $14.9M across all funds, with a high-materiality "
     "break count of 3 and missing trade breaks affecting GEF NVDA ($8.4M) and "
     "ISC 4385 ($3.4M).", True),
    ("There are 7 data-quality warnings; a review is necessary.", True),
    ("Reconciliation has 7 overdue tickets out of 12 open; 19 tickets are open and "
     "12 are past SLA overall.", True),
    ("Total AUM was $350.2M, down $1.5M (-0.4%) week over week.", True),
    ("Total AUM was about $350 million.", True),           # rounding is fine
    ("SLA met was 40% this week and 58% over the last four weeks.", True),
    ("The largest break is GEF NVDA, a Missing Trade with an absolute difference "
     "of $8.4M.", True),
    ("Data quality shows 0 errors and 7 warnings, including a stale BRL rate "
     "(-3.00%) on 2 positions.", True),
]


@pytest.mark.parametrize("text, should_pass", CASES)
def test_guardrail_cases(facts, text, should_pass):
    verdict = ai.verify([text], facts)
    assert verdict.ok is should_pass, verdict.problems


def test_facts_have_one_number_each(facts):
    for f in facts.facts:
        ai.parse_value(f.value)          # raises unless exactly one number


def test_tickers_with_digits_are_not_numbers(facts):
    nums = ai.extract_numbers("GEF NVDA and ISC 4385 and USCB FNMA 6.0 2054", facts.tags)
    assert nums == []


def test_bullet_count_limits(facts):
    assert not ai.verify([], facts).ok
    assert not ai.verify(["Total AUM was $350.2M."] * 7, facts).ok


def test_parse_bullets_handles_common_formats():
    raw = "Here you go:\n- one\n* two\n• three\n1. four\n2) five\n\nThanks!"
    assert ai.parse_bullets(raw) == ["one", "two", "three", "four", "five"]


# ------------------------------------------------------------ orchestration
GOOD = "- Total AUM was $350.2M, down $1.5M (-0.4%) week over week.\n" \
       "- Reconciliation has 7 overdue tickets out of 12 open."
BAD = "- Total AUM was $352.0M.\n- There are 19 overdue tickets."


def scripted(*replies):
    calls = []

    def chat(messages, model):
        calls.append(messages)
        return replies[len(calls) - 1]
    return chat, calls


def test_verified_first_attempt(data):
    chat, calls = scripted(GOOD)
    res = ai.generate(data, ["template"], chat=chat)
    assert res.source.startswith("llm:") and len(res.bullets) == 2 and len(calls) == 1


def test_retry_gets_feedback_then_passes(data):
    chat, calls = scripted(BAD, GOOD)
    res = ai.generate(data, ["template"], chat=chat)
    assert res.source.startswith("llm:")
    feedback = calls[1][-1]["content"]
    assert "failed fact-checking" in feedback and "$352.0M" in feedback


def test_falls_back_to_template_after_two_failures(data):
    chat, _ = scripted(BAD, BAD)
    res = ai.generate(data, ["template line"], chat=chat)
    assert res.bullets == ["template line"]
    assert "failed verification" in res.source and len(res.attempts) == 2


@responses.activate
def test_ollama_down_falls_back(data):
    responses.post(f"{ai.OLLAMA_URL}/api/chat",
                   body=requests.ConnectionError("connection refused"))
    res = ai.generate(data, ["template line"])
    assert res.bullets == ["template line"] and "unavailable" in res.source


@responses.activate
def test_pipeline_records_commentary_source_and_audit(tmp_path):
    responses.post(f"{ai.OLLAMA_URL}/api/chat", json={"message": {"content": GOOD}})
    res = run(AS_OF, PRIOR, out_dir=tmp_path, commentary="local-llm")
    assert res.data.commentary_source.startswith("llm:llama3.2")
    info = dict(zip(res.data.run_info["item"], res.data.run_info["value"], strict=True))
    assert info["commentary"].startswith("llm:")
    assert (tmp_path / "2026-11-13" / "commentary_audit_20261113.json").exists()
