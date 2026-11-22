# backend/tests/memory/test_retrieval_quality.py
"""Golden-set quality gates as pytest tests (phase 0 measurement layer).

Folds the harness metrics into the normal ``pytest tests/memory`` run so
a quality regression is caught by the same command as a unit regression,
and adds a PRECISION metric for the noise the baseline exposed (positive
cases that retrieve the right memory PLUS unrelated ones -- audit R2).

Gate philosophy (mirrors test_regression_known_defects.py):
  * Gates the system ALREADY meets are hard assertions (must stay green).
  * Targets not yet met (Arabic recall, precision) are xfail(strict=True):
    they fail today and force the marker off in the same commit that fixes
    them (phase 2). No target can be silently declared met.
  * A regression gate compares against the committed baseline report.

Run from ``backend``::

    python -m pytest tests/memory/test_retrieval_quality.py -q
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.memory.eval.harness import (
    DEFAULT_CASES_PATH,
    load_cases,
    run_all,
    summarise,
)

EVAL = Path(__file__).resolve().parent / "eval"
BASELINE_PATH = EVAL / "baseline_report.json"

# Absolute targets from the repair plan (calibrated on the golden set).
AR_RECALL_TARGET = 0.85
EN_RECALL_TARGET = 0.90
UNRELATED_MAX = 0.05
P95_MAX_MS = 150.0
PRECISION_TARGET = 0.90
BASELINE_MAX_RECALL_DROP = 0.02


@pytest.fixture(scope="module")
def golden():
    """Run the whole golden set once through the real pipeline."""
    cases = load_cases(DEFAULT_CASES_PATH)
    results = asyncio.run(run_all(cases))
    return summarise(results), results


def _precision(results) -> float:
    """Hits / total retrieved across positive cases (1.0 = no noise)."""
    pos = [r for r in results
           if r["kind"] == "positive" and r["status"] == "ok"]
    retrieved = sum(len(r["got"]) for r in pos)
    hits = sum(len(set(r["expect"]) & set(r["got"])) for r in pos)
    return (hits / retrieved) if retrieved else 1.0


# --- Gates the system already meets (must stay green) ---------------------

def test_no_intent_injects_nothing(golden):
    summary, _ = golden
    assert summary["cases_error"] == 0, summary["errors"]
    assert summary["false_injection_no_intent"] == 0.0


def test_unrelated_within_budget(golden):
    summary, _ = golden
    assert summary["false_injection_unrelated"] <= UNRELATED_MAX


def test_p95_latency_within_budget(golden):
    summary, _ = golden
    assert summary["p95_latency_ms"] <= P95_MAX_MS


def test_english_recall_target(golden):
    summary, _ = golden
    assert summary["recall_at_5_by_lang"]["en"] >= EN_RECALL_TARGET


def test_no_regression_vs_baseline(golden):
    summary, _ = golden
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["summary"]
    assert summary["recall_at_5"] >= base["recall_at_5"] - BASELINE_MAX_RECALL_DROP
    assert summary["false_injection_no_intent"] <= base["false_injection_no_intent"]
    assert summary["false_injection_unrelated"] <= base["false_injection_unrelated"]


# --- Targets not yet met (phase 2); strict xfail until fixed --------------

def test_arabic_recall_target(golden):
    summary, _ = golden
    assert summary["recall_at_5_by_lang"]["ar"] >= AR_RECALL_TARGET


def test_positive_precision_target(golden):
    _, results = golden
    assert _precision(results) >= PRECISION_TARGET
