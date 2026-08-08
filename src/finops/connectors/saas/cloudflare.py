"""Cloudflare cost, via the Billable Usage API.

Cloudflare now serves billing in FOCUS shape: GET /accounts/{id}/billable-usage
returns one row per product per charge period with FOCUS column names already on
it (BilledCost, EffectiveCost, ChargePeriodStart, ServiceName, ServiceFamilyName,
ConsumedQuantity). That makes Cloudflare the first SaaS connector here that does
not have to be reverse-engineered into FOCUS: the rows come out of the API
already normalized, so get_costs_as_focus reads them instead of flattening a
CostSummary and guessing the rest back.

The older path stays as a fallback. billing-history is invoice-level, arrives
after the fact, and is not available on every account; subscriptions below that
is list price, not usage. Accounts the new endpoint does not cover (it is
documented for self-serve billing) still get an answer, just a coarser one.

Token scope: Account > Billing > Read.
"""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary
from ...focus.schema import CHARGE_CATEGORIES, FocusRecord

# Where the untouched Billable Usage row rides on a CostEntry, so the FOCUS
# translation can use the real columns instead of re-deriving them from the
# flattened entry. Underscore-prefixed: it is wiring, not user-facing metadata.
_FOCUS_ROW = "_cf_focus"

# Cloudflare is not just a CDN on the bill any more. Workers is compute, R2 is
# storage, D1 is a database, Workers AI is inference. Filing all of it under
# Networking would misplace that spend in every cross-provider category rollup,
# and ServiceFamilyName is what finally makes the distinction available.
_FAMILY_CATEGORY: dict[str, str] = {
    "workers ai": "AI and Machine Learning",
    "ai gateway": "AI and Machine Learning",
    "vectorize": "AI and Machine Learning",
    "workers": "Compute",
    "pages": "Compute",
    "containers": "Compute",
    "r2": "Storage",
    "stream": "Storage",
    "images": "Storage",
    "d1": "Database",
    "kv": "Database",
    "durable objects": "Database",
    "hyperdrive": "Database",
    "logpush": "Observability",
    "logs": "Observability",
}
_DEFAULT_CATEGORY = "Networking"

# Longest key first so "Workers AI" lands in ML rather than matching "workers"
# and being called Compute. Word-bounded so the two-character keys ("r2", "d1",
# "kv") cannot match inside an unrelated product name.
_CATEGORY_PATTERNS = [
    (re.compile(rf"\b{re.escape(k)}\b"), v)
    for k, v in sorted(_FAMILY_CATEGORY.items(), key=lambda kv: -len(kv[0]))
]

# Usage columns Cloudflare returns that nable's FocusRecord has no field for.
# They ride as Tags rather than being dropped: quantity is what makes a cost
# line explainable ("$0.75 for 150000 GB-months"), not just reportable.
_TAG_FIELDS = (
    "ConsumedQuantity", "ConsumedUnit", "PricingQuantity", "PricingUnit",
    "CumulatedContractedCost", "CumulatedPricingQuantity",
    "SubscriptionId", "ZoneId", "ZoneName", "BillingCurrency", "ChargeClass",
)


def _money(*candidates: Any) -> float:
    """First finite number among the candidates, else 0.0.

    Cloudflare omits some cost columns on some line types, and a NaN or an inf
    would poison every total downstream and break strict JSON serialization, so
    non-finite values are treated as absent rather than passed through.
    """
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            f = float(c)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return 0.0


def _ts(value: Any, fallback: datetime) -> datetime:
    """Parse an ISO-8601 timestamp, falling back to the requested window.

    A row with a missing or malformed period should still be counted at the
    window bounds; dropping it would silently understate the bill.
    """
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _category(family: str, service: str) -> str:
    """FOCUS ServiceCategory for one bill line, from its family and name."""
    haystack = f"{family} {service}".lower()
    for pattern, category in _CATEGORY_PATTERNS:
        if pattern.search(haystack):
            return category
    return _DEFAULT_CATEGORY


def _row_tags(row: dict) -> dict[str, str]:
    return {
        key: str(row[key])
        for key in _TAG_FIELDS
        if row.get(key) is not None and row.get(key) != ""
    }


def _usage_note(row: dict) -> dict[str, str]:
    """Human-readable usage for the non-FOCUS surfaces (summaries, drill-downs)."""
    out: dict[str, str] = {}
    qty, unit = row.get("ConsumedQuantity"), row.get("ConsumedUnit")
    if qty not in (None, "") and unit:
        out["usage"] = f"{qty} {unit}"
    family = row.get("ServiceFamilyName")
    if family:
        out["family"] = str(family)
    return out


class CloudflareConnector(BaseConnector):
    provider = "cloudflare"
    _API = "https://api.cloudflare.com/client/v4"

    def __init__(self) -> None:
        self._api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")
        self._account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

    async def is_configured(self) -> bool:
        return bool(self._api_token and self._account_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_token}"}

    # ── Billable Usage (FOCUS-shaped, preferred) ─────────────────────────────

    async def _fetch_billable_usage(
        self, client: httpx.AsyncClient, start_date: date, end_date: date
    ) -> list[dict] | None:
        """Rows from the Billable Usage API, or None if this account cannot serve it.

        None and [] mean different things and the caller depends on the
        difference: [] is "the endpoint answered, there was no spend", None is
        "this account has no such endpoint" and is the only case that should
        fall back to the older, coarser billing-history path.
        """
        try:
            r = await client.get(
                f"{self._API}/accounts/{self._account_id}/billable-usage",
                headers=self._headers(),
                params={"from": start_date.isoformat(), "to": end_date.isoformat()},
            )
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or not body.get("success", True):
            return None
        result = body.get("result")
        if not isinstance(result, list):
            return None
        return [row for row in result if isinstance(row, dict)]

    def _summary_from_usage(
        self, rows: list[dict], start_date: date, end_date: date
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_account: dict[str, float] = {}
        by_region: dict[str, float] = {}
        currencies: set[str] = set()
        total = 0.0

        for row in rows:
            amount = _money(row.get("BilledCost"), row.get("ContractedCost"),
                            row.get("EffectiveCost"))
            service = str(row.get("ServiceName") or "").strip() or "Cloudflare"
            account = str(row.get("BillingAccountId") or "").strip() or self._account_id
            zone = str(row.get("ZoneName") or "").strip()
            currency = str(row.get("BillingCurrency") or "USD").strip().upper() or "USD"
            currencies.add(currency)

            total += amount
            by_service[service] = by_service.get(service, 0.0) + amount
            by_account[account] = by_account.get(account, 0.0) + amount
            if zone:
                by_region[zone] = by_region.get(zone, 0.0) + amount

            entries.append(CostEntry(
                provider="cloudflare",
                account_id=account,
                account_name=str(row.get("BillingAccountName") or account),
                # A zone is a resource, not a region, but `region` is the slot the
                # rest of nable reads for a per-domain breakdown. The FOCUS record
                # below keeps it honest and puts the zone in ResourceId instead.
                region=zone,
                service=service,
                amount=amount,
                currency=currency,
                metadata={_FOCUS_ROW: row, **_usage_note(row)},
            ))

        return CostSummary(
            provider="cloudflare",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account=by_account or {self._account_id: 0.0},
            by_region=by_region,
            entries=entries,
            currency=(currencies.pop() if len(currencies) == 1
                      else ("MIXED" if currencies else "USD")),
        )

    def _focus_record(
        self, row: dict, period_start: datetime, period_end: datetime
    ) -> FocusRecord:
        billed = _money(row.get("BilledCost"), row.get("ContractedCost"),
                        row.get("EffectiveCost"))
        service = str(row.get("ServiceName") or "").strip() or "Cloudflare"
        family = str(row.get("ServiceFamilyName") or "").strip()
        zone_id = str(row.get("ZoneId") or "").strip()
        zone_name = str(row.get("ZoneName") or "").strip()
        account = str(row.get("BillingAccountId") or "").strip() or self._account_id

        charge = str(row.get("ChargeCategory") or "").strip().title()
        if charge not in CHARGE_CATEGORIES:
            charge = "Usage"

        return FocusRecord(
            BilledCost=billed,
            EffectiveCost=_money(row.get("EffectiveCost"), row.get("ContractedCost"), billed),
            ListCost=_money(row.get("ListCost"), billed),
            ResourceId=zone_id or service,
            ResourceName=zone_name or service,
            ResourceType=family or "Service",
            ServiceName=service,
            ServiceCategory=_category(family, service),
            ProviderName="Cloudflare",
            PublisherName=str(row.get("InvoiceIssuerName") or "").strip() or "Cloudflare",
            # Cloudflare bills a global anycast network. There is no region to
            # report, and inventing one from the zone would be a false location.
            RegionId=None,
            RegionName=None,
            BillingPeriodStart=_ts(row.get("BillingPeriodStart"), period_start),
            BillingPeriodEnd=period_end,
            ChargePeriodStart=_ts(row.get("ChargePeriodStart"), period_start),
            ChargePeriodEnd=_ts(row.get("ChargePeriodEnd"), period_end),
            ChargeCategory=charge,
            ChargeDescription=str(row.get("ChargeDescription") or "").strip() or None,
            CommitmentDiscountId=None,
            CommitmentDiscountType=None,
            Tags=_row_tags(row),
            SubAccountId=account or None,
            SubAccountName=str(row.get("BillingAccountName") or account) or None,
        )

    # ── Legacy fallback ──────────────────────────────────────────────────────

    async def _legacy_costs(
        self, client: httpx.AsyncClient, start_date: date, end_date: date
    ) -> CostSummary:
        """Invoice-level billing history, then subscription list price.

        Neither carries usage quantities or a service family, so spend from here
        stays a flat Networking line. It is a floor, not a substitute.
        """
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        r = await client.get(
            f"{self._API}/accounts/{self._account_id}/billing-history",
            headers=self._headers(),
            params={"since": start_date.isoformat(), "before": end_date.isoformat()},
        )
        if r.status_code == 200:
            for item in r.json().get("result", []):
                amount = float(item.get("amount", 0))
                product = item.get("type", "Unknown")
                zone = item.get("zone", {}).get("name", "")
                total += amount
                by_service[product] = by_service.get(product, 0.0) + amount
                entries.append(CostEntry(
                    provider="cloudflare",
                    account_id=self._account_id,
                    account_name=self._account_id,
                    service=product,
                    region=zone,
                    amount=amount,
                ))
        else:
            r2 = await client.get(
                f"{self._API}/accounts/{self._account_id}/subscriptions",
                headers=self._headers(),
            )
            if r2.status_code == 200:
                for sub in r2.json().get("result", []):
                    name = sub.get("component_values", [{}])[0].get("name", "Subscription")
                    price = float(sub.get("price", 0))
                    if price:
                        by_service[name] = by_service.get(name, 0.0) + price
                        total += price
                        entries.append(CostEntry(
                            provider="cloudflare",
                            account_id=self._account_id,
                            account_name=self._account_id,
                            service=name,
                            region="",
                            amount=price,
                        ))

        return CostSummary(
            provider="cloudflare",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._account_id: total},
            by_region={},
            entries=entries,
        )

    # ── Connector interface ──────────────────────────────────────────────────

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        async with httpx.AsyncClient(timeout=30) as client:
            rows = await self._fetch_billable_usage(client, start_date, end_date)
            if rows is not None:
                return self._summary_from_usage(rows, start_date, end_date)
            return await self._legacy_costs(client, start_date, end_date)

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Cloudflare spend as FOCUS 1.2 records.

        Billable Usage rows become records directly, keeping the real charge
        periods, the list/effective/billed split, and a per-line service
        category. Only fallback lines go through the generic SaaS translator,
        which can do no better than one flat Networking record apiece.
        """
        from ...focus.translators.generic import entry_to_focus

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        period_start = datetime(start_date.year, start_date.month, start_date.day,
                                tzinfo=timezone.utc)
        period_end = datetime(end_date.year, end_date.month, end_date.day,
                              tzinfo=timezone.utc)

        records = []
        for entry in (getattr(summary, "entries", None) or []):
            meta = getattr(entry, "metadata", None)
            row = meta.get(_FOCUS_ROW) if isinstance(meta, dict) else None
            if isinstance(row, dict):
                records.append(self._focus_record(row, period_start, period_end))
            else:
                records.append(entry_to_focus(
                    entry,
                    provider="Cloudflare",
                    publisher="Cloudflare",
                    category="Networking",
                    period_start=period_start,
                    period_end=period_end,
                ))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self._API}/accounts/{self._account_id}",
                headers=self._headers(),
            )
            if r.status_code == 200:
                data = r.json().get("result", {})
                return [{"id": self._account_id, "name": data.get("name", self._account_id)}]
        return [{"id": self._account_id, "name": "Cloudflare"}]
