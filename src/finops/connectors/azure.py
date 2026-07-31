from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class AzureConnector(BaseConnector):
    provider = "azure"

    def __init__(self) -> None:
        self._subscription_ids: list[str] = [
            s.strip()
            for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

    async def is_configured(self) -> bool:
        """Any usable Azure credential, not just a service principal.

        This used to require AZURE_CLIENT_ID/SECRET/TENANT_ID plus an explicit
        AZURE_SUBSCRIPTION_IDS. Someone who had run `az login`, which is the
        normal state for anyone working in Azure, read as unconfigured and was
        sent through a manual service principal wizard. AWS meanwhile accepted
        its whole default credential chain. Same question, same answer, for
        every cloud now: see finops/ambient.py."""
        required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
        if all(os.getenv(v) for v in required) and self._subscription_ids:
            return True
        from ..ambient import detect_azure
        amb = detect_azure()
        if amb.usable and not self._subscription_ids:
            # Adopt what the ambient credential can actually see, so the rest of
            # the connector has a scope to query without a second setup step.
            self._subscription_ids = amb.scopes
        return amb.usable

    # ── internal helpers ────────────────────────────────────────────────────

    def _credential(self):
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )

    def _query_costs(self, subscription_id: str, start_date: date, end_date: date, granularity: str) -> dict:
        from azure.mgmt.costmanagement import CostManagementClient
        from azure.mgmt.costmanagement.models import (
            QueryDataset,
            QueryDefinition,
            QueryGrouping,
            QueryTimePeriod,
        )

        client = CostManagementClient(self._credential())
        scope = f"/subscriptions/{subscription_id}"

        query = QueryDefinition(
            type="ActualCost",
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=f"{start_date.isoformat()}T00:00:00Z",
                to=f"{end_date.isoformat()}T00:00:00Z",
            ),
            dataset=QueryDataset(
                granularity=granularity.capitalize(),
                grouping=[
                    QueryGrouping(type="Dimension", name="ServiceName"),
                    QueryGrouping(type="Dimension", name="ResourceLocation"),
                ],
            ),
        )

        result = client.query.usage(scope=scope, parameters=query)
        return result

    def _parse_result(self, result, subscription_id: str, start_date: date, end_date: date) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0

        columns = {col.name: i for i, col in enumerate(result.columns)}
        cost_idx = columns.get("Cost", 0)
        service_idx = columns.get("ServiceName", 2)
        region_idx = columns.get("ResourceLocation", 3)

        for row in result.rows or []:
            amount = float(row[cost_idx])
            service = str(row[service_idx])
            region = str(row[region_idx])
            total += amount
            by_service[service] = by_service.get(service, 0.0) + amount
            by_region[region] = by_region.get(region, 0.0) + amount
            entries.append(
                CostEntry(
                    provider="azure",
                    account_id=subscription_id,
                    account_name=subscription_id,
                    service=service,
                    region=region,
                    amount=amount,
                )
            )

        return CostSummary(
            provider="azure",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={subscription_id: total},
            by_region=by_region,
            entries=entries,
        )

    # ── public API ──────────────────────────────────────────────────────────

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        # Read-through cache + parallel subscriptions. Azure Cost Management is
        # the slowest provider API in the stack, and the sync SDK call used to
        # run on the event loop, blocking every other connector while it waited.
        import copy as _copy
        from .. import cache as _cache
        _ck = _cache.make_key(
            "azure.get_costs", ",".join(sorted(self._subscription_ids)),
            start_date.isoformat(), end_date.isoformat(), granularity,
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            return _copy.deepcopy(_hit)

        merged = CostSummary(
            provider="azure",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        async def _one(sub_id: str) -> CostSummary:
            try:
                raw = await asyncio.to_thread(self._query_costs, sub_id, start_date, end_date, granularity)
            except Exception as exc:
                from ._reauth import is_auth_expiry, reauth_message
                if is_auth_expiry(exc, "azure"):
                    raise RuntimeError(reauth_message("azure", f"subscription {sub_id}")) from exc
                raise
            return self._parse_result(raw, sub_id, start_date, end_date)

        for summary in await asyncio.gather(*[_one(s) for s in self._subscription_ids]):
            merged.total_usd += summary.total_usd
            for k, v in summary.by_service.items():
                merged.by_service[k] = merged.by_service.get(k, 0.0) + v
            for k, v in summary.by_account.items():
                merged.by_account[k] = merged.by_account.get(k, 0.0) + v
            for k, v in summary.by_region.items():
                merged.by_region[k] = merged.by_region.get(k, 0.0) + v
            merged.entries.extend(summary.entries)

        _cache.set(_ck, _copy.deepcopy(merged), _cache.COST_TTL)
        return merged

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return cost data as a list of FocusRecord objects."""
        from ..focus import normalize

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        period_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
        period_end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc)

        records = []
        for entry in summary.entries:
            raw: dict[str, Any] = {
                "BilledCost": entry.amount,
                "EffectiveCost": entry.amount,
                "ServiceName": entry.service,
                "ResourceLocation": entry.region,
                "SubscriptionId": entry.account_id,
                "SubscriptionName": entry.account_name,
                "ChargeType": "Usage",
                "BillingPeriodStartDate": period_start.isoformat(),
                "BillingPeriodEndDate": period_end.isoformat(),
                "UsageDate": period_start.isoformat(),
                "Tags": entry.tags,
            }
            records.append(normalize("azure", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": s, "name": s} for s in self._subscription_ids]
