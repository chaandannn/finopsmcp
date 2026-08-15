from __future__ import annotations

import asyncio

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class GCPConnector(BaseConnector):
    provider = "gcp"

    # Cached ADC default-project answer (class-level: several tools construct a
    # fresh connector per call). None = not probed yet.
    _adc_project_cache: list[str] | None = None

    def __init__(self) -> None:
        self._billing_account_ids: list[str] = [
            b.strip()
            for b in os.getenv("GCP_BILLING_ACCOUNT_IDS", "").split(",")
            if b.strip()
        ]
        key_path = os.getenv("GCP_SERVICE_ACCOUNT_KEY_PATH")
        if key_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", key_path)

    async def is_configured(self) -> bool:
        """Any usable GCP credential, including Application Default Credentials.

        This used to require GOOGLE_APPLICATION_CREDENTIALS (or the key-path
        variant) plus an explicit GCP_BILLING_ACCOUNT_IDS. `gcloud auth
        application-default login`, the most common developer setup, sets
        neither, so it read as unconfigured while AWS accepted its whole default
        chain. Same question, same answer, for every cloud now: see
        finops/ambient.py."""
        has_creds = bool(
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("GCP_SERVICE_ACCOUNT_KEY_PATH")
        )
        if has_creds and self._billing_account_ids:
            return True
        # to_thread: the GCP probe walks Application Default Credentials
        # synchronously. Same reasoning as azure.py's is_configured, and the two
        # compound because _active() gathers them.
        import asyncio

        from ..ambient import PROBES
        amb = await asyncio.to_thread(PROBES["gcp"])
        if amb.usable and not self._billing_account_ids:
            self._billing_account_ids = amb.scopes
        return amb.usable

    def project_ids(self) -> list[str]:
        """
        GCP resource scans (Compute, Monitoring) are per-project, not per-billing-
        account. Read GCP_PROJECT_IDS (comma-separated) first, then fall back to the
        default project on the Application Default Credentials.

        The ADC fallback is guarded: with no ADC file and no explicit project env,
        google.auth.default() probes the GCE metadata server with retries (~9s of
        hang on a laptop) before failing, and several tools call this per request.
        Only probe when a local credential source plausibly exists, cap the
        metadata timeout, and cache the answer for the process.
        """
        ids = [p.strip() for p in os.getenv("GCP_PROJECT_IDS", "").split(",") if p.strip()]
        if ids:
            return ids
        if GCPConnector._adc_project_cache is not None:
            return list(GCPConnector._adc_project_cache)

        adc_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        )
        explicit_project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        if not (os.path.exists(adc_file) or explicit_project):
            # No local credential source: skip the slow metadata probe entirely.
            # A GCE VM can opt back in with GOOGLE_CLOUD_PROJECT. Not cached, so
            # creating credentials mid-session is picked up on the next call.
            return []
        try:
            # Bound the metadata probe if one still happens (default 3s x retries).
            os.environ.setdefault("GCE_METADATA_TIMEOUT", "1")
            import google.auth

            _, project = google.auth.default()
            result = [project] if project else []
        except Exception:
            result = []
        GCPConnector._adc_project_cache = result
        return list(result)

    # ── internal helpers ────────────────────────────────────────────────────

    def _client(self):
        from google.cloud import billing_v1

        return billing_v1.CloudBillingClient()

    def _catalog_client(self):
        from google.cloud import billing_v1

        return billing_v1.CloudCatalogClient()

    def _query_bigquery(self, billing_account_id: str, start_date: date, end_date: date) -> list[dict]:
        """
        Query the BigQuery billing export table.
        Requires: GCP_BQ_BILLING_TABLE env var in the form `project.dataset.table`.
        Falls back to Cloud Billing API summary if not configured.
        """
        bq_table = os.getenv("GCP_BQ_BILLING_TABLE")
        if not bq_table:
            return self._query_billing_api(billing_account_id, start_date, end_date)

        from google.cloud import bigquery

        client = bigquery.Client()
        query = f"""
            SELECT
                service.description AS service,
                location.region AS region,
                -- Net cost = gross cost + credits (credits are stored negative:
                -- committed-use discounts, SUDs, promotions). Summing cost alone
                -- overstates spend by 10-30% and won't match the GCP console.
                SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS total_cost,
                currency
            FROM `{bq_table}`
            WHERE
                billing_account_id = @billing_account_id
                AND DATE(usage_start_time) >= @start_date
                AND DATE(usage_start_time) <= @end_date
            GROUP BY service, region, currency
            ORDER BY total_cost DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("billing_account_id", "STRING", billing_account_id),
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [dict(row) for row in rows]

    def _query_billing_api(self, billing_account_id: str, start_date: date, end_date: date) -> list[dict]:
        """
        Light fallback using the Cloud Billing SKU catalog.
        Note: The Billing API doesn't expose actual spend — for real spend data
        configure GCP_BQ_BILLING_TABLE (BigQuery billing export).
        """
        return []

    def _rows_to_summary(
        self,
        rows: list[dict],
        billing_account_id: str,
        start_date: date,
        end_date: date,
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0
        currencies: set[str] = set()

        for row in rows:
            service = row.get("service", "Unknown")
            region = row.get("region") or ""
            amount = float(row.get("total_cost", 0))
            cur = row.get("currency")
            if cur:
                currencies.add(cur)
            total += amount
            by_service[service] = by_service.get(service, 0.0) + amount
            if region:
                by_region[region] = by_region.get(region, 0.0) + amount
            entries.append(
                CostEntry(
                    provider="gcp",
                    account_id=billing_account_id,
                    account_name=billing_account_id,
                    service=service,
                    region=region,
                    amount=amount,
                )
            )

        return CostSummary(
            provider="gcp",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={billing_account_id: total},
            by_region=by_region,
            entries=entries,
            currency=(currencies.pop() if len(currencies) == 1 else ("MIXED" if currencies else "USD")),
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
        merged = CostSummary(
            provider="gcp",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        # Read-through cache + parallel billing accounts; BigQuery used to run
        # synchronously on the event loop and block every other connector.
        import copy as _copy
        from .. import cache as _cache
        _ck = _cache.make_key(
            "gcp.get_costs", ",".join(sorted(self._billing_account_ids)),
            start_date.isoformat(), end_date.isoformat(), granularity,
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            return _copy.deepcopy(_hit)

        async def _one(billing_account_id: str) -> CostSummary:
            try:
                rows = await asyncio.to_thread(self._query_bigquery, billing_account_id, start_date, end_date)
            except Exception as exc:
                from ._reauth import is_auth_expiry, reauth_message
                if is_auth_expiry(exc, "gcp"):
                    raise RuntimeError(reauth_message("gcp", f"billing account {billing_account_id}")) from exc
                raise
            return self._rows_to_summary(rows, billing_account_id, start_date, end_date)

        for summary in await asyncio.gather(*[_one(b) for b in self._billing_account_ids]):
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

        # Derive invoice_month from start_date for BillingPeriod derivation
        invoice_month = f"{start_date.year}{start_date.month:02d}"

        records = []
        for entry in summary.entries:
            raw: dict[str, Any] = {
                "cost": entry.amount,
                "service": {"description": entry.service},
                "location": {"region": entry.region},
                "project": {"id": entry.account_id, "name": entry.account_name},
                "invoice_month": invoice_month,
                "labels": entry.tags,
            }
            records.append(normalize("gcp", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": b, "name": b} for b in self._billing_account_ids]


def get_sku_costs_by_project(start_date: date, end_date: date) -> dict:
    """Per-project, per-SKU net cost from the BigQuery billing export.

    The shared input for the traffic-shaped waste detections (Cloud NAT data
    processing, GCS request overhead): both are per-operation charges that only
    the export itemises. The Billing API cannot answer this, so without
    GCP_BQ_BILLING_TABLE this returns an explicit error rather than an empty
    list an absent export would make indistinguishable from a clean estate.

    Net cost includes credits (stored negative), matching _query_bigquery: a
    CUD-covered SKU that summed gross would overstate by 10-30% and the
    detections would flag money that is not actually being paid.
    """
    bq_table = os.getenv("GCP_BQ_BILLING_TABLE")
    if not bq_table:
        return {"error": ("GCP_BQ_BILLING_TABLE is not set. SKU-level detection "
                          "needs the BigQuery billing export.")}
    try:
        from google.cloud import bigquery
    except ImportError:
        return {"error": "google-cloud-bigquery is not installed."}

    query = f"""
        SELECT
            project.id AS project_id,
            service.description AS service,
            sku.description AS sku,
            SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS cost_usd
        FROM `{bq_table}`
        WHERE
            DATE(usage_start_time) >= @start_date
            AND DATE(usage_start_time) <= @end_date
        GROUP BY project_id, service, sku
        HAVING cost_usd > 0
        ORDER BY cost_usd DESC
    """
    try:
        client = bigquery.Client()
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
        ])
        rows = [dict(r) for r in client.query(query, job_config=job_config).result()]
    except Exception as exc:
        return {"error": f"BigQuery billing export query failed: {type(exc).__name__}"}

    return {"rows": rows, "period": f"{start_date} to {end_date}",
            "source": "gcp_bigquery_billing_export"}
