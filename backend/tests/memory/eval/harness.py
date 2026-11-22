# backend/tests/memory/eval/harness.py
"""Golden-set evaluation harness for the memory retrieval pipeline.

Phase 0 of the memory repair plan: measure BEFORE fixing. This module
runs golden cases through the REAL production retrieval path (the same
calls the chat dispatcher makes) and reports quality metrics so that
every later fix is proven by numbers, not by opinion.

What it exercises
-----------------
For each case a fresh SQLite database is created in a temporary
directory, the case memories are written through the real provider,
and retrieval runs through::

    SqliteFtsProvider -> build_memory_context -> ranker -> budget fit

with ``allow_recency_fallback=False`` and ``match_any=True``. That pair
is NOT an artificial test configuration: it is exactly what
``MemoryService.retrieve`` passes on the chat path when
``always_retrieve=True`` (the product default). See the
``[ALWAYS-RETRIEVE MODE]`` comment in
``backend/omweb/memory/manager/memory_service.py``.

Case schema (YAML)
------------------
    - id: en-pos-001            # unique, stable across runs
      lang: en                  # ar | en | ru
      kind: positive            # positive | no_intent | unrelated
      memories:                 # list of str, or {text, type} mappings
        - "We chose PostgreSQL for the project database"
        - text: "I prefer dark mode in all editors"
          type: preference      # semantic | episodic | procedural |
                                # preference | decision
      query: "which database did we pick"
      expect_idx: [0]           # indexes of memories that MUST be
                                # retrieved; [] for negative kinds

Kinds ``update``, ``duplicate`` and ``secret`` are accepted but marked
``deferred``: their runners arrive with the phases that fix them
(write policy in phase 3, data minimizer in phase 1). They never count
as passes or failures here.

Metrics
-------
    recall_at_5                 fraction of positive cases where at
                                least one expected memory was injected
    recall_at_5_by_lang         same, split by case language
    false_injection_no_intent   fraction of no_intent cases that
                                injected ANY memory (gate: must be 0)
    false_injection_unrelated   fraction of unrelated cases that
                                injected ANY memory (gate: <= 0.05)
    p95_latency_ms              95th percentile retrieval latency

CLI
---
Run from the ``backend`` directory::

    python -m tests.memory.eval.harness --cases tests/memory/eval/golden_cases.yaml --out baseline.json
    python -m tests.memory.eval.harness --baseline baseline.json          # regression gate
    python -m tests.memory.eval.harness --strict                          # absolute gates

Exit code is 0 when every active gate passes, 1 otherwise. ``--strict``
enforces the plan's absolute targets (they are EXPECTED to fail until
the corresponding defects are fixed); ``--baseline`` enforces only
"no regression worse than 2 percentage points" against a committed
baseline report, which is the phase-0 CI gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from omweb.memory.storage.normalizer import normalize_for_storage
from peldrun.memory.domain.entry import MemoryEntry, MemoryProvenance
from peldrun.memory.domain.enums import MemoryScope, MemorySourceType, MemoryType
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.domain.settings import MemorySettings
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider
from peldrun.memory.services.context_builder import build_memory_context
from peldrun.memory.services.router import Route, RouteDecision

#: Case kinds this runner evaluates today (the retrieval pipeline).
RETRIEVAL_KINDS = frozenset({"positive", "no_intent", "unrelated"})

#: Accepted kinds whose runners land with later phases. Recorded as
#: ``deferred`` so the golden file can grow ahead of the code.
DEFERRED_KINDS = {
    "update": "write-policy runner lands in phase 3",
    "duplicate": "write-policy runner lands in phase 3",
    "secret": "data-minimizer runner lands in phase 1",
}

#: Absolute gates from the repair plan (enabled by --strict).
STRICT_RECALL_BY_LANG = {"ar": 0.85, "en": 0.90}
STRICT_MAX_FALSE_UNRELATED = 0.05
STRICT_MAX_P95_MS = 150.0

#: Maximum recall drop (percentage points) tolerated vs a baseline.
BASELINE_MAX_RECALL_DROP = 0.02

DEFAULT_CASES_PATH = Path(__file__).with_name("golden_cases.yaml")


def _memory_spec(spec: Any) -> Tuple[str, MemoryType]:
    """Normalize one ``memories`` item to (text, memory_type)."""
    if isinstance(spec, str):
        return spec, MemoryType.SEMANTIC
    if isinstance(spec, dict) and isinstance(spec.get("text"), str):
        return spec["text"], MemoryType(spec.get("type", "semantic"))
    raise ValueError(f"invalid memory spec: {spec!r}")


async def run_case(
    case: Dict[str, Any],
    settings: Optional[MemorySettings] = None,
) -> Dict[str, Any]:
    """Run one golden case through the real retrieval pipeline.

    Never raises: a case-level failure is captured in the result so a
    single bad case cannot abort a full evaluation run.
    """
    kind = case.get("kind", "")
    result: Dict[str, Any] = {
        "id": case.get("id", "<missing-id>"),
        "lang": case.get("lang", "?"),
        "kind": kind,
        "expect": list(case.get("expect_idx", [])),
        "got": [],
        "latency_ms": 0.0,
        "status": "ok",
        "error": None,
    }
    if kind in DEFERRED_KINDS:
        result["status"] = "deferred"
        result["error"] = DEFERRED_KINDS[kind]
        return result
    if kind not in RETRIEVAL_KINDS:
        result["status"] = "error"
        result["error"] = f"unknown kind: {kind!r}"
        return result

    settings = settings or MemorySettings()
    try:
        specs = [_memory_spec(s) for s in case.get("memories", [])]
        with tempfile.TemporaryDirectory() as tmp:
            prov = SqliteFtsProvider(db_path=Path(tmp) / "m.db")
            await prov.open()
            try:
                for text, mtype in specs:
                    await prov.create(
                        MemoryEntry.create(
                            content=text,
                            memory_type=mtype,
                            scope_type=MemoryScope.USER,
                            scope_id="default",
                            provenance=MemoryProvenance(
                                source_type=MemorySourceType.EXPLICIT_USER
                            ),
                            normalized_content=normalize_for_storage(text),
                        )
                    )
                refs = resolve_scope_refs(
                    chat_id="c", project_id="p", user_id="default"
                )
                route = Route(decision=RouteDecision.DIRECTED, reason="golden")
                t0 = time.perf_counter()
                ctx = await build_memory_context(
                    query=case["query"],
                    scope_refs=refs,
                    route=route,
                    provider=prov,
                    settings=settings,
                    allow_recency_fallback=False,
                    match_any=True,
                )
                result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
                injected = {m.content for m in ctx.memories}
                result["got"] = [
                    i for i, (text, _) in enumerate(specs) if text in injected
                ]
            finally:
                await prov.close()
    except Exception as exc:  # noqa: BLE001 - report, do not abort the run
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def run_all(
    cases: List[Dict[str, Any]],
    settings: Optional[MemorySettings] = None,
) -> List[Dict[str, Any]]:
    """Run cases sequentially so latency measurements stay comparable."""
    return [await run_case(case, settings) for case in cases]


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-case results into the plan's quality metrics."""
    ok = [r for r in results if r["status"] == "ok"]
    pos = [r for r in ok if r["kind"] == "positive"]
    no_intent = [r for r in ok if r["kind"] == "no_intent"]
    unrelated = [r for r in ok if r["kind"] == "unrelated"]

    def recall(rs: List[Dict[str, Any]]) -> Optional[float]:
        return _rate(
            sum(bool(set(r["expect"]) & set(r["got"])) for r in rs), len(rs)
        )

    def false_rate(rs: List[Dict[str, Any]]) -> Optional[float]:
        return _rate(sum(bool(r["got"]) for r in rs), len(rs))

    latencies = sorted(r["latency_ms"] for r in ok)
    p95 = (
        latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)]
        if latencies
        else None
    )
    langs = sorted({r["lang"] for r in pos})
    return {
        "cases_total": len(results),
        "cases_ok": len(ok),
        "cases_deferred": sum(r["status"] == "deferred" for r in results),
        "cases_error": sum(r["status"] == "error" for r in results),
        "recall_at_5": recall(pos),
        "recall_at_5_by_lang": {
            lang: recall([r for r in pos if r["lang"] == lang]) for lang in langs
        },
        "false_injection_no_intent": false_rate(no_intent),
        "false_injection_unrelated": false_rate(unrelated),
        "p95_latency_ms": p95,
        "errors": [
            {"id": r["id"], "error": r["error"]}
            for r in results
            if r["status"] == "error"
        ],
    }


def check_gates(
    summary: Dict[str, Any],
    baseline: Optional[Dict[str, Any]] = None,
    strict: bool = False,
) -> List[str]:
    """Return a list of gate violations (empty list = all gates pass)."""
    problems: List[str] = []
    if summary["cases_error"]:
        problems.append(f"{summary['cases_error']} case(s) raised errors")

    if strict:
        fi = summary["false_injection_no_intent"]
        if fi is not None and fi > 0.0:
            problems.append(f"no_intent false injection {fi:.2%} (must be 0%)")
        fu = summary["false_injection_unrelated"]
        if fu is not None and fu > STRICT_MAX_FALSE_UNRELATED:
            problems.append(
                f"unrelated false injection {fu:.2%} "
                f"(max {STRICT_MAX_FALSE_UNRELATED:.0%})"
            )
        for lang, target in STRICT_RECALL_BY_LANG.items():
            got = summary["recall_at_5_by_lang"].get(lang)
            if got is not None and got < target:
                problems.append(
                    f"recall@5[{lang}] {got:.2%} below target {target:.0%}"
                )
        p95 = summary["p95_latency_ms"]
        if p95 is not None and p95 > STRICT_MAX_P95_MS:
            problems.append(
                f"p95 latency {p95:.0f}ms above {STRICT_MAX_P95_MS:.0f}ms"
            )

    if baseline is not None:
        base = baseline.get("summary", baseline)
        old_recall = base.get("recall_at_5")
        new_recall = summary["recall_at_5"]
        if (
            old_recall is not None
            and new_recall is not None
            and new_recall < old_recall - BASELINE_MAX_RECALL_DROP
        ):
            problems.append(
                f"recall@5 regressed {old_recall:.2%} -> {new_recall:.2%}"
            )
        for key in ("false_injection_no_intent", "false_injection_unrelated"):
            old_v, new_v = base.get(key), summary.get(key)
            if old_v is not None and new_v is not None and new_v > old_v:
                problems.append(f"{key} regressed {old_v:.2%} -> {new_v:.2%}")
    return problems


def load_cases(path: Path) -> List[Dict[str, Any]]:
    """Load and minimally validate the golden YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list):
        raise ValueError(f"{path}: expected a list of cases")
    seen: set = set()
    for case in cases:
        if not isinstance(case, dict) or "id" not in case or "kind" not in case:
            raise ValueError(f"{path}: every case needs 'id' and 'kind'")
        if case["id"] in seen:
            raise ValueError(f"{path}: duplicate case id {case['id']!r}")
        seen.add(case["id"])
        if case["kind"] in RETRIEVAL_KINDS:
            if not isinstance(case.get("query"), str):
                raise ValueError(f"{path}: case {case['id']} needs a query")
            if case["kind"] == "positive" and not case.get("expect_idx"):
                raise ValueError(
                    f"{path}: positive case {case['id']} needs expect_idx"
                )
    return cases


def _print_summary(summary: Dict[str, Any]) -> None:
    def fmt(v: Optional[float]) -> str:
        return "n/a" if v is None else f"{v:.2%}"

    print(f"cases: {summary['cases_ok']} ok, "
          f"{summary['cases_deferred']} deferred, "
          f"{summary['cases_error']} error")
    print(f"recall@5: {fmt(summary['recall_at_5'])}  "
          f"by lang: { {k: fmt(v) for k, v in summary['recall_at_5_by_lang'].items()} }")
    print(f"false injection no_intent: {fmt(summary['false_injection_no_intent'])}  "
          f"unrelated: {fmt(summary['false_injection_unrelated'])}")
    p95 = summary["p95_latency_ms"]
    print(f"p95 latency: {'n/a' if p95 is None else f'{p95:.1f}ms'}")
    for err in summary["errors"]:
        print(f"  ERROR {err['id']}: {err['error']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--out", type=Path, default=None,
                        help="write the full JSON report here")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="fail on regression vs this JSON report")
    parser.add_argument("--strict", action="store_true",
                        help="enforce the plan's absolute quality gates")
    parser.add_argument("--limit", type=int, default=None,
                        help="run only the first N cases (debugging)")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.limit is not None:
        cases = cases[: args.limit]
    settings = MemorySettings()
    results = asyncio.run(run_all(cases, settings))
    summary = summarise(results)
    report = {
        "schema_version": 1,
        "settings": {
            "min_relevance_score": settings.min_relevance_score,
            "default_limit": settings.default_limit,
            "max_tokens": settings.token_budget.max_tokens,
        },
        "summary": summary,
        "results": results,
    }
    if args.out is not None:
        args.out.write_text(
            json.dumps(report, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    _print_summary(summary)

    baseline = None
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = check_gates(summary, baseline=baseline, strict=args.strict)
    for problem in problems:
        print(f"GATE FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
