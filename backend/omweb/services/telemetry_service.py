"""
backend/omweb/services/telemetry_service.py

PELDRUN Web Telemetry & Usage Analytics Domain Service.

Coordinates consumption analytics across UsageStore and PricingStore.
Serves dashboard KPIs, timeseries metrics, multi-dimensional breakdowns,
chat-specific reports, non-destructive repricing simulations,
and structured report exports (CSV / JSON).
Strictly respects Web-Core architectural boundaries (P1-02).
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from peldrun.runtime.pricing_store import PricingRule, get_pricing_store
from peldrun.runtime.usage_store import get_usage_store

logger = logging.getLogger("omweb.services.telemetry_service")


class TelemetryService:
    """Authoritative domain service providing usage telemetry queries for the Web layer."""

    def __init__(self) -> None:
        self.usage_store = get_usage_store()
        self.pricing_store = get_pricing_store()

    async def get_dashboard_summary(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile complete top-level KPI metrics for the analytics dashboard."""
        return await self.usage_store.query_summary(
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )

    async def get_timeseries_metrics(
        self,
        granularity: str = "day",
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve aggregated time-series buckets (day, week, month)."""
        safe_granularity = granularity.lower() if granularity.lower() in ("day", "week", "month") else "day"
        return await self.usage_store.query_timeseries(
            granularity=safe_granularity,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )

    async def get_dimension_breakdown(
        self,
        group_by: str = "model",
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve consumption breakdown grouped by model, provider, mode, or project."""
        safe_group = group_by.lower() if group_by.lower() in ("model", "provider", "mode", "project") else "model"
        return await self.usage_store.query_breakdown(
            group_by=safe_group,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            mode=mode,
        )

    async def get_chat_telemetry(self, chat_id: str) -> Dict[str, Any]:
        """Retrieve comprehensive historical token telemetry for a specific chat."""
        summary = await self.usage_store.get_chat_usage_summary(chat_id)
        invocations = await self.usage_store.list_chat_invocations(chat_id)
        return {
            "summary": summary,
            "invocations": invocations,
        }

    async def list_invocations_paginated(
        self,
        limit: int = 50,
        cursor: Optional[float] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch cursor-paginated list of individual LLM invocation ledger records."""
        items, next_cursor, has_more = await self.usage_store.query_invocations(
            limit=limit,
            cursor=cursor,
            from_time=from_time,
            to_time=to_time,
            chat_id=chat_id,
            project_id=project_id,
            provider=provider,
            model=model,
            mode=mode,
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
        }

    async def list_pricing_rules(self, active_only: bool = True) -> List[PricingRule]:
        """Retrieve configured pricing catalog."""
        return await self.pricing_store.list_rules(active_only=active_only)

    async def save_pricing_rule(self, rule: PricingRule) -> None:
        """Register or update a model pricing rule."""
        await self.pricing_store.save_rule(rule)

    async def retire_pricing_rule(self, pricing_id: str) -> bool:
        """Soft-retire a pricing rule without destructive deletion."""
        return await self.pricing_store.retire_rule(pricing_id)

    # ---------------------------------------------------------------- Destructive Reset
    async def reset_all_data(self) -> Dict[str, Any]:
        """DESTRUCTIVE: wipe all telemetry facts and reset pricing to defaults.

        Orchestrates a full environment reset for the operator-only
        ``POST /api/telemetry/reset`` endpoint (UsageTab "Reset Data"
        button). Existing tests and manual cleanup scripts are the
        only legitimate callers — production flows must never invoke
        this implicitly.

        Order of operations
        -------------------
        1. Pricing rules are wiped first (fast, few rows).
        2. Token ledger rows are wiped second (potentially large).
        3. Default pricing seeds are re-inserted LAST, so that even if
           step 2 fails partway through, the pricing catalog is
           restored to a consistent (if over-broad) state. A failure
           in step 3 would leave the pricing catalog empty — but the
           ledger is already wiped, so the system is in a known
           degraded state that the operator can recover by restarting
           the app (``_init_db`` re-seeds missing rows).

        Idempotency
        -----------
        Calling this twice in a row is safe: the second call deletes
        the freshly-seeded rules, deletes zero ledger rows, and
        re-seeds the catalog again. Net effect is identical.

        Returns
        -------
        dict
            Diagnostic counters for the UI toast:
              * ``ledger_rows_deleted``   — invocation facts removed
              * ``pricing_rules_deleted`` — pricing rules removed
              * ``pricing_seeds_restored``— default rules re-inserted
              * ``message``               — human-readable summary
        """
        # Snapshot before — the reset is destructive; the caller
        # may want to record what was wiped in an audit log.
        ledger_deleted = 0
        pricing_reset: Dict[str, int] = {"deleted": 0, "seeded": 0}

        try:
            pricing_reset = await self.pricing_store.reset_to_defaults()
            ledger_deleted = await self.usage_store.delete_all_invocations()
        except Exception as exc:
            logger.exception(
                "Telemetry reset failed midway: pricing_deleted=%d "
                "seeded=%d ledger_deleted=%d error=%s",
                pricing_reset.get("deleted", 0),
                pricing_reset.get("seeded", 0),
                ledger_deleted,
                exc,
            )
            raise

        logger.warning(
            "Telemetry full reset complete: %d ledger row(s) deleted, "
            "%d pricing rule(s) deleted, %d seed(s) restored.",
            ledger_deleted,
            pricing_reset.get("deleted", 0),
            pricing_reset.get("seeded", 0),
        )

        return {
            "ledger_rows_deleted": ledger_deleted,
            "pricing_rules_deleted": pricing_reset.get("deleted", 0),
            "pricing_seeds_restored": pricing_reset.get("seeded", 0),
            "message": (
                f"Wiped {ledger_deleted} invocation(s) and "
                f"{pricing_reset.get('deleted', 0)} pricing rule(s); "
                f"{pricing_reset.get('seeded', 0)} default pricing seed(s) "
                f"restored."
            ),
        }

    async def simulate_repricing(
        self,
        chat_id: Optional[str] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Simulate repricing without mutating immutable historical facts."""
        return await self.pricing_store.preview_repricing(
            chat_id=chat_id,
            from_time=from_time,
            to_time=to_time,
        )

    # ---------------------------------------------------------------- Report Exports (P1-03F)
    async def export_report_json(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile a canonical JSON export report combining summary, timeseries, and breakdowns."""
        summary = await self.get_dashboard_summary(from_time=from_time, to_time=to_time, chat_id=chat_id)
        timeseries = await self.get_timeseries_metrics(granularity="day", from_time=from_time, to_time=to_time, chat_id=chat_id)
        models_breakdown = await self.get_dimension_breakdown(group_by="model", from_time=from_time, to_time=to_time, chat_id=chat_id)
        providers_breakdown = await self.get_dimension_breakdown(group_by="provider", from_time=from_time, to_time=to_time, chat_id=chat_id)

        return {
            "report_title": "PELDRUN Usage & Cost Telemetry Report",
            "generated_at": time.time(),
            "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "filters": {
                "from_time": from_time,
                "to_time": to_time,
                "chat_id": chat_id,
            },
            "summary": summary,
            "timeseries_daily": timeseries,
            "breakdown_by_model": models_breakdown,
            "breakdown_by_provider": providers_breakdown,
        }

    async def export_report_csv(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
    ) -> str:
        """Export raw invocations matching filter into a standardized CSV string."""
        invocations, _, _ = await self.usage_store.query_invocations(
            limit=5000,
            from_time=from_time,
            to_time=to_time,
            chat_id=chat_id,
        )

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "invocation_id",
            "created_at",
            "chat_id",
            "mode",
            "provider",
            "model_requested",
            "model_returned",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "cost_usd",
            "latency_ms",
            "ttft_ms",
            "status",
            "estimated",
        ])

        for inv in invocations:
            writer.writerow([
                inv.get("invocation_id"),
                inv.get("created_at"),
                inv.get("chat_id"),
                inv.get("mode"),
                inv.get("provider"),
                inv.get("model_requested"),
                inv.get("model_returned"),
                inv.get("input_tokens"),
                inv.get("output_tokens"),
                inv.get("total_tokens"),
                inv.get("cached_input_tokens"),
                inv.get("reasoning_output_tokens"),
                inv.get("cost_usd"),
                inv.get("latency_ms"),
                inv.get("ttft_ms"),
                inv.get("status"),
                inv.get("estimated"),
            ])

        return output.getvalue()


telemetry_service = TelemetryService()

__all__ = ["TelemetryService", "telemetry_service"]
