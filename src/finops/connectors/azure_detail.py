"""
Azure resource-level cost connector via the Cost Management Query API.

Uses the Azure Cost Management Query API (POST .../query) to retrieve
resource-level and tag-level cost breakdowns, and the Capacity API for
reservation utilization summaries.

No storage account or export job required -- data is queried on demand.
This is a Pro plan feature.

Required env vars:
  AZURE_SUBSCRIPTION_ID  -- single sub ID or comma-separated list for multi-sub
  AZURE_CLIENT_ID        -- service principal / app registration client ID
  AZURE_CLIENT_SECRET    -- service principal secret
  AZURE_TENANT_ID        -- Azure Active Directory tenant ID
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..security.env import get_env

log = logging.getLogger(__name__)

_MGMT_BASE    = "https://management.azure.com"
_LOGIN_BASE   = "https://login.microsoftonline.com"
_COST_API_VER = "2023-11-01"
_CAP_API_VER  = "2023-05-01"


# ── configuration ─────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True when all required Azure auth env vars are set."""
    required = [
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
    ]
    return all(get_env(v) for v in required)


def _subscription_ids() -> list[str]:
    """Parse AZURE_SUBSCRIPTION_ID, supporting comma-separated multi-sub lists."""
    raw = get_env("AZURE_SUBSCRIPTION_ID", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


# ── OAuth2 token ──────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    """
    Obtain an OAuth2 access token via the client credentials flow.

    Posts to the Azure AD v2 token endpoint using the configured service
    principal credentials. The returned token is scoped to the Azure
    management API.

    Returns:
        A bearer token string suitable for Authorization headers.

    Raises:
        RuntimeError: if the token request fails or httpx is unavailable.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx is required. Run: pip install httpx")

    tenant  = get_env("AZURE_TENANT_ID")
    url     = f"{_LOGIN_BASE}/{tenant}/oauth2/v2.0/token"

    payload = {
        "grant_type":    "client_credentials",
        "client_id":     get_env("AZURE_CLIENT_ID"),
        "client_secret": get_env("AZURE_CLIENT_SECRET"),
        "scope":         "https://management.azure.com/.default",
    }

    try:
        resp = httpx.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Azure token request failed: {exc}") from exc

    token = data.get("access_token")
    if not token:
        raise RuntimeError(
            f"No access_token in Azure response. "
            f"Error: {data.get('error_description', data.get('error', 'unknown'))}"
        )
    return token


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


# ── internal query helpers ────────────────────────────────────────────────────

def _query_cost_management(
    token: str,
    subscription_id: str,
    body: dict,
) -> list[dict]:
    """
    POST to the Azure Cost Management Query API and handle pagination.

    Parses the columns-plus-rows response format into a list of row dicts.
    Follows nextLink until all pages are consumed.

    Args:
        token: Valid Azure management bearer token.
        subscription_id: Azure subscription ID.
        body: JSON body for the cost query.

    Returns:
        List of row dicts keyed by column name.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx is required. Run: pip install httpx")

    url = (
        f"{_MGMT_BASE}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query"
        f"?api-version={_COST_API_VER}"
    )
    headers = _auth_headers(token)
    all_rows: list[dict] = []
    columns: list[str] = []

    while url:
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Azure Cost Management query failed: %s", exc)
            break

        props = data.get("properties", data)

        # Parse columns on first page
        if not columns:
            columns = [
                col.get("name", f"col_{i}")
                for i, col in enumerate(props.get("columns", []))
            ]

        for row in props.get("rows", []):
            all_rows.append(dict(zip(columns, row)))

        # Pagination
        url = data.get("nextLink") or props.get("nextLink")
        # For paginated requests, method switches to GET
        if url:
            try:
                resp = httpx.get(url, headers=headers, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                props = data.get("properties", data)
                for row in props.get("rows", []):
                    all_rows.append(dict(zip(columns, row)))
                url = data.get("nextLink") or props.get("nextLink")
            except Exception as exc:
                log.warning("Azure pagination request failed: %s", exc)
                break

    return all_rows


# ── public API ────────────────────────────────────────────────────────────────

def get_resource_costs(
    start_date: date,
    end_date: date,
    subscription_id: str | None = None,
    resource_group: str | None = None,
    min_cost_usd: float = 1.0,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Return per-resource cost data from Azure Cost Management.

    Groups by ResourceId, ResourceType, ResourceGroupName, ServiceName,
    and MeterCategory. Filters to Usage charge types only. Applies a
    minimum cost threshold and result count limit.

    When subscription_id is None, queries all subscriptions found in
    AZURE_SUBSCRIPTION_ID (comma-separated) and merges results.

    Args:
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        subscription_id: Single subscription to query. None = all configured subs.
        resource_group: Optional resource group name filter.
        min_cost_usd: Exclude resources whose cost is below this threshold.
        limit: Maximum number of resources to return, ordered by cost desc.

    Returns:
        {
            "resources": [...],
            "total_cost": float,
            "total_resources": int,
            "subscription_id": str,
            "period": str,
            "source": "azure_cost_management",
        }
    """
    if not is_configured():
        return _error(
            "Azure not configured. Set AZURE_SUBSCRIPTION_ID, AZURE_CLIENT_ID, "
            "AZURE_CLIENT_SECRET, AZURE_TENANT_ID."
        )

    try:
        token = _get_access_token()
    except RuntimeError as exc:
        log.warning("Azure auth failed: %s", exc)
        return _error(str(exc))

    subs = [subscription_id] if subscription_id else _subscription_ids()

    grouping: list[dict] = [
        {"type": "Dimension", "name": "ResourceId"},
        {"type": "Dimension", "name": "ResourceType"},
        {"type": "Dimension", "name": "ResourceGroupName"},
        {"type": "Dimension", "name": "ServiceName"},
        {"type": "Dimension", "name": "MeterCategory"},
    ]

    query_filter: dict[str, Any] = {
        "dimensions": {
            "name":     "ChargeType",
            "operator": "In",
            "values":   ["Usage"],
        }
    }

    if resource_group:
        query_filter = {
            "and": [
                query_filter,
                {
                    "dimensions": {
                        "name":     "ResourceGroupName",
                        "operator": "In",
                        "values":   [resource_group],
                    }
                },
            ]
        }

    body: dict[str, Any] = {
        "type":       "ActualCost",
        "timeframe":  "Custom",
        "timePeriod": {
            "from": start_date.isoformat(),
            "to":   end_date.isoformat(),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"}
            },
            "grouping": grouping,
            "filter":   query_filter,
        },
    }

    all_rows: list[dict] = []
    for sub in subs:
        try:
            rows = _query_cost_management(token, sub, body)
            for row in rows:
                row["_subscription_id"] = sub
            all_rows.extend(rows)
        except Exception as exc:
            log.warning("Azure resource cost query failed for sub %s: %s", sub, exc)

    # Parse and filter
    resources: list[dict] = []
    total_cost = 0.0

    for row in all_rows:
        cost = _float(row.get("Cost", row.get("totalCost", 0)))
        if cost < min_cost_usd:
            continue
        total_cost += cost
        resources.append({
            "resource_id":     _str(row.get("ResourceId")),
            "resource_type":   _str(row.get("ResourceType")),
            "resource_group":  _str(row.get("ResourceGroupName")),
            "service":         _str(row.get("ServiceName") or row.get("MeterCategory")),
            "cost_usd":        round(cost, 4),
            "subscription_id": _str(row.get("_subscription_id")),
        })

    # Sort by cost desc and apply limit
    resources.sort(key=lambda r: r["cost_usd"], reverse=True)
    resources = resources[:limit]

    return {
        "resources":        resources,
        "total_cost":       round(total_cost, 4),
        "total_resources":  len(resources),
        "subscription_id":  subscription_id or ",".join(subs),
        "period":           f"{start_date} to {end_date}",
        "source":           "azure_cost_management",
    }


def get_tag_cost_breakdown(
    tag_key: str,
    start_date: date,
    end_date: date,
    subscription_id: str | None = None,
) -> dict[str, Any]:
    """
    Break Azure costs down by a resource tag using Cost Management.

    Queries the Cost Management API grouping by the specified tag key.
    Resources missing the tag are aggregated under "__untagged__".

    Args:
        tag_key: The tag key to group by (e.g. "team", "environment").
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        subscription_id: Single subscription to query. None = all configured subs.

    Returns:
        {
            "by_tag": {"payments": 1234.56, "__untagged__": 890.12, ...},
            "tag_key": str,
            "untagged_cost_usd": float,
            "source": "azure_cost_management",
        }
    """
    if not is_configured():
        return _error("Azure not configured.")

    try:
        token = _get_access_token()
    except RuntimeError as exc:
        log.warning("Azure auth failed: %s", exc)
        return _error(str(exc))

    subs = [subscription_id] if subscription_id else _subscription_ids()

    body: dict[str, Any] = {
        "type":       "ActualCost",
        "timeframe":  "Custom",
        "timePeriod": {
            "from": start_date.isoformat(),
            "to":   end_date.isoformat(),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"}
            },
            "grouping": [
                {"type": "TagKey", "name": tag_key},
            ],
        },
    }

    by_tag: dict[str, float] = {}

    for sub in subs:
        try:
            rows = _query_cost_management(token, sub, body)
            for row in rows:
                tag_val = _str(row.get(tag_key) or row.get("TagKey") or "")
                if not tag_val:
                    tag_val = "__untagged__"
                cost = _float(row.get("Cost", row.get("totalCost", 0)))
                by_tag[tag_val] = round(by_tag.get(tag_val, 0.0) + cost, 4)
        except Exception as exc:
            log.warning(
                "Azure tag breakdown failed for sub %s tag %s: %s",
                sub, tag_key, exc,
            )

    untagged_cost = by_tag.get("__untagged__", 0.0)

    return {
        "by_tag":           {k: v for k, v in sorted(by_tag.items(), key=lambda x: x[1], reverse=True)},
        "tag_key":          tag_key,
        "untagged_cost_usd": round(untagged_cost, 4),
        "period":           f"{start_date} to {end_date}",
        "source":           "azure_cost_management",
    }


def get_reservation_utilization(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Fetch Azure reservation utilization summaries from the Capacity API.

    Returns monthly utilization summaries for all reservations visible to
    the configured service principal. Wasted hours is reserved minus used.
    Average utilization is the mean across all returned reservation records.

    Args:
        start_date: Inclusive start of the summary period.
        end_date: Inclusive end of the summary period.

    Returns:
        {
            "reservations": [...],
            "avg_utilization_pct": float,
            "source": "azure_reservations_api",
        }
    """
    if not is_configured():
        return _error("Azure not configured.")

    try:
        import httpx
    except ImportError:
        return _error("httpx is required. Run: pip install httpx")

    try:
        token = _get_access_token()
    except RuntimeError as exc:
        log.warning("Azure auth failed: %s", exc)
        return _error(str(exc))

    headers = _auth_headers(token)
    url = (
        f"{_MGMT_BASE}/providers/Microsoft.Capacity/reservationSummaries"
        f"?api-version={_CAP_API_VER}"
        f"&grain=monthly"
        f"&$filter=properties/usageDate ge {start_date.isoformat()}"
        f" and properties/usageDate le {end_date.isoformat()}"
    )

    all_values: list[dict] = []

    while url:
        try:
            resp = httpx.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Azure reservation summary fetch failed: %s", exc)
            break

        all_values.extend(data.get("value", []))
        url = data.get("nextLink")

    reservations: list[dict] = []
    utilization_pcts: list[float] = []

    for item in all_values:
        props   = item.get("properties", {})
        res_id  = _str(item.get("id", ""))
        sku     = _str(props.get("skuName") or props.get("reservationOrderId", ""))

        avg_util    = _float(props.get("avgUtilizationPercentage", 0))
        used_hrs    = _float(props.get("usedHours", 0))
        reserved_hrs = _float(props.get("reservedHours", 0))
        wasted_hrs  = max(0.0, reserved_hrs - used_hrs)

        utilization_pcts.append(avg_util)
        reservations.append({
            "reservation_id":     res_id,
            "sku_name":           sku,
            "avg_utilization_pct": round(avg_util, 2),
            "used_hours":          round(used_hrs, 2),
            "reserved_hours":      round(reserved_hrs, 2),
            "wasted_hours":        round(wasted_hrs, 2),
            # The scope detector needs these. The summaries API carries them
            # inconsistently across offer types, so they are best-effort here and
            # back-filled from the Reservations list API below when absent.
            "meter_id":            _str(props.get("meterId") or ""),
            "region":              _str(props.get("location") or ""),
            "applied_scope_type":  _str(props.get("appliedScopeType") or ""),
            "applied_scopes":      props.get("appliedScopes") or [],
        })

    _backfill_reservation_scopes(reservations, headers)

    avg_util_overall = (
        round(sum(utilization_pcts) / len(utilization_pcts), 2)
        if utilization_pcts else 0.0
    )

    return {
        "reservations":         reservations,
        "avg_utilization_pct":  avg_util_overall,
        "period":               f"{start_date} to {end_date}",
        "source":               "azure_reservations_api",
    }


def _backfill_reservation_scopes(reservations: list[dict], headers: dict) -> None:
    """Fill scope/meter/region gaps from the Reservations list API.

    The summaries endpoint reports utilisation; the reservations endpoint is the
    source of truth for appliedScopeType and appliedScopes. Best-effort: a
    reservation we cannot enrich keeps blank scope fields, and the scope detector
    treats an unknown scope as not-narrow, so enrichment failure can only produce
    a missed finding, never a false one.
    """
    if not reservations or not any(
        not r.get("applied_scope_type") for r in reservations
    ):
        return
    try:
        import httpx
    except ImportError:
        return

    url = f"{_MGMT_BASE}/providers/Microsoft.Capacity/reservations?api-version={_CAP_API_VER}"
    by_id: dict[str, dict] = {}
    while url:
        try:
            resp = httpx.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Azure reservations list fetch failed: %s", exc)
            return
        for item in data.get("value", []):
            props = item.get("properties", {})
            by_id[_str(item.get("id", "")).lower()] = {
                "applied_scope_type": _str(props.get("appliedScopeType") or props.get("userFriendlyAppliedScopeType") or ""),
                "applied_scopes": props.get("appliedScopes") or [],
                "meter_id": _str((props.get("skuDescription") and props.get("meterId")) or props.get("meterId") or ""),
                "region": _str(item.get("location") or ""),
                "sku_name": _str((item.get("sku") or {}).get("name") or ""),
            }
        url = data.get("nextLink")

    for r in reservations:
        detail = by_id.get(_str(r.get("reservation_id")).lower())
        if not detail:
            continue
        for key in ("applied_scope_type", "applied_scopes", "meter_id", "region"):
            if not r.get(key):
                r[key] = detail[key]
        if not r.get("sku_name") and detail.get("sku_name"):
            r["sku_name"] = detail["sku_name"]


def get_on_demand_usage_by_subscription(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """On-demand (undiscounted) spend per subscription per meter over the window.

    The scope detector's third condition: money a narrowly-scoped reservation
    COULD have absorbed but did not, because it cannot reach the subscription
    that spent it. Grouped by MeterId so it joins against reservation meters,
    and filtered to OnDemand pricing so already-discounted usage never counts as
    recoverable a second time.

    Queries every subscription in AZURE_SUBSCRIPTION_ID. One subscription
    failing (no Cost Management access, moved tenant) skips it with a warning
    rather than losing the rest; the detector then simply cannot see that
    sibling, which can only under-report.
    """
    if not is_configured():
        return _error("Azure not configured.")
    try:
        import httpx  # noqa: F401
    except ImportError:
        return _error("httpx is required. Run: pip install httpx")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        log.warning("Azure auth failed: %s", exc)
        return _error(str(exc))

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start_date}T00:00:00Z", "to": f"{end_date}T23:59:59Z"},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "MeterId"},
                {"type": "Dimension", "name": "MeterSubCategory"},
                {"type": "Dimension", "name": "ResourceLocation"},
            ],
            "filter": {
                "dimensions": {"name": "PricingModel", "operator": "In", "values": ["OnDemand"]}
            },
        },
    }

    usage: list[dict] = []
    for sub_id in _subscription_ids():
        try:
            rows = _query_cost_management(token, sub_id, body)
        except Exception as exc:
            log.warning("Azure on-demand usage fetch failed for %s: %s", sub_id, exc)
            continue
        for row in rows:
            usage.append({
                "subscription_id": sub_id,
                "meter_id": _str(row.get("MeterId", "")),
                "sku_name": _str(row.get("MeterSubCategory", "")),
                "region": _str(row.get("ResourceLocation", "")),
                "on_demand_cost_usd": _float(row.get("Cost", row.get("totalCost", 0))),
            })

    return {"usage": usage, "period": f"{start_date} to {end_date}",
            "source": "azure_cost_management"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _float(value: Any) -> float:
    """Safely coerce a value to float."""
    try:
        return float(value) if value not in (None, "", "NULL") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _str(value: Any) -> str:
    """Safely coerce a value to str, stripping whitespace."""
    if value is None:
        return ""
    return str(value).strip()


def _error(message: str) -> dict[str, Any]:
    return {"error": message, "source": "azure_cost_management"}
