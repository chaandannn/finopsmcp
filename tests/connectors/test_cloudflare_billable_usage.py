"""Cloudflare's Billable Usage API is FOCUS-shaped; the connector must use it as such.

The old connector read invoice-level billing-history and, failing that, priced
subscriptions at list. Both lose the two things that make a Cloudflare line
explainable: how much was consumed, and which product family consumed it. The
new endpoint returns both under FOCUS column names, so these guard that the
columns survive the trip instead of being flattened and re-guessed.

They also guard the fallback, because the endpoint is not on every account and a
regression there would silently zero out those bills.
"""
import asyncio
from datetime import date

import httpx
import pytest

from finops.connectors.saas.cloudflare import CloudflareConnector, _category
from finops.focus.schema import SERVICE_CATEGORIES

_START = date(2026, 7, 1)
_END = date(2026, 7, 31)

_USAGE_PATH = "/billable-usage"
_HISTORY_PATH = "/billing-history"
_SUBS_PATH = "/subscriptions"


class _Resp:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Fake httpx.AsyncClient routing by URL suffix.

    Records every URL so a test can assert what was NOT called: the point of
    several of these is that a working billable-usage response must stop the
    connector from reaching for the legacy endpoints at all.
    """

    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.calls.append(url)
        for suffix, resp in self.routes.items():
            if suffix in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(404)


def _wire(monkeypatch, routes):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _Client(routes, calls))
    conn = CloudflareConnector()
    conn._api_token = "tok"
    conn._account_id = "acct-1"
    return conn, calls


def _usage(rows):
    return _Resp(200, {"success": True, "errors": [], "messages": [], "result": rows})


# One row per product per charge period, shaped like the documented response.
_ROWS = [
    {
        "BillingCurrency": "USD",
        "BillingAccountId": "acct-1",
        "BillingAccountName": "StreamCo",
        "BillingPeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodEnd": "2026-07-31T23:59:59Z",
        "ChargeCategory": "Usage",
        "ChargeDescription": "Workers paid requests",
        "ServiceName": "Workers Standard",
        "ServiceFamilyName": "Workers",
        "ConsumedQuantity": 150000,
        "ConsumedUnit": "GB-months",
        "ContractedCost": 0.75,
        "BilledCost": 0.75,
        "ListCost": 1.00,
        "EffectiveCost": 0.75,
        "CumulatedContractedCost": 2.25,
    },
    {
        "BillingCurrency": "USD",
        "BillingAccountId": "acct-1",
        "BillingPeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodEnd": "2026-07-31T23:59:59Z",
        "ChargeCategory": "Usage",
        "ServiceName": "R2 Class A Operations",
        "ServiceFamilyName": "R2",
        "ConsumedQuantity": 4_000_000,
        "ConsumedUnit": "operations",
        "BilledCost": 18.00,
    },
    {
        "BillingCurrency": "USD",
        "BillingAccountId": "acct-1",
        "ChargeCategory": "Usage",
        "ServiceName": "Pro Plan",
        "ServiceFamilyName": "Zone Plans",
        "ZoneId": "zone-abc",
        "ZoneName": "streamco.tv",
        "BilledCost": 25.00,
    },
]


# ── The new endpoint is the source of truth ──────────────────────────────────

def test_billable_usage_is_used_and_legacy_endpoints_are_not_touched(monkeypatch):
    conn, calls = _wire(monkeypatch, {_USAGE_PATH: _usage(_ROWS)})

    summary = asyncio.run(conn.get_costs(_START, _END))

    assert round(summary.total_usd, 2) == 43.75
    assert summary.by_service["Workers Standard"] == 0.75
    assert summary.by_service["R2 Class A Operations"] == 18.00
    assert summary.by_region == {"streamco.tv": 25.00}
    assert summary.by_account == {"acct-1": 43.75}
    assert summary.currency == "USD"
    assert not any(_HISTORY_PATH in u or _SUBS_PATH in u for u in calls)


def test_window_is_passed_as_from_and_to(monkeypatch):
    """The endpoint defaults to the current billing period; a range must be sent
    or every historical question silently answers about this month instead."""
    captured = {}

    class _Capturing(_Client):
        async def get(self, url, **kw):
            captured.update(kw.get("params") or {})
            return await super().get(url, **kw)

    calls: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _Capturing({_USAGE_PATH: _usage([])}, calls))
    conn = CloudflareConnector()
    conn._api_token, conn._account_id = "tok", "acct-1"

    asyncio.run(conn.get_costs(_START, _END))
    assert captured == {"from": "2026-07-01", "to": "2026-07-31"}


def test_empty_result_means_no_spend_not_a_broken_endpoint(monkeypatch):
    """[] is an answer. Falling back here would price subscriptions at list and
    report spend the account did not incur."""
    conn, calls = _wire(monkeypatch, {_USAGE_PATH: _usage([])})

    summary = asyncio.run(conn.get_costs(_START, _END))

    assert summary.total_usd == 0.0
    assert summary.entries == []
    assert not any(_HISTORY_PATH in u or _SUBS_PATH in u for u in calls)


# ── FOCUS records built from the native columns ──────────────────────────────

def test_focus_records_keep_the_native_columns(monkeypatch):
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(_ROWS)})

    recs = asyncio.run(conn.get_costs_as_focus(_START, _END))
    workers, r2, zone = recs

    assert all(r.ProviderName == "Cloudflare" for r in recs)
    assert all(r.ServiceCategory in SERVICE_CATEGORIES for r in recs)

    # List vs billed is a real distinction here, not a copy of the same number.
    assert workers.BilledCost == 0.75
    assert workers.ListCost == 1.00
    assert workers.ChargeDescription == "Workers paid requests"
    assert workers.ResourceType == "Workers"
    assert workers.ChargePeriodEnd.day == 31

    # Quantity has no FocusRecord column, so it must survive as a tag.
    assert workers.Tags["ConsumedQuantity"] == "150000"
    assert workers.Tags["ConsumedUnit"] == "GB-months"
    assert r2.Tags["ConsumedQuantity"] == "4000000"

    # A zone is a resource, not a region.
    assert zone.ResourceId == "zone-abc"
    assert zone.ResourceName == "streamco.tv"
    assert zone.RegionId is None


def test_families_map_to_their_own_service_category(monkeypatch):
    """Workers is compute and R2 is storage. Filing them under Networking
    because Cloudflare started as a CDN misplaces the spend in every
    cross-provider category rollup."""
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(_ROWS)})

    workers, r2, zone = asyncio.run(conn.get_costs_as_focus(_START, _END))

    assert workers.ServiceCategory == "Compute"
    assert r2.ServiceCategory == "Storage"
    assert zone.ServiceCategory == "Networking"


@pytest.mark.parametrize("family,service,expected", [
    ("Workers AI", "Neurons", "AI and Machine Learning"),  # not Compute
    ("Workers", "Workers Standard", "Compute"),
    ("R2", "Class A Operations", "Storage"),
    ("D1", "Rows read", "Database"),
    ("Durable Objects", "Requests", "Database"),
    ("Logpush", "Logs delivered", "Observability"),
    ("Zone Plans", "Business Plan", "Networking"),
    ("", "Argo Smart Routing", "Networking"),
])
def test_category_mapping(family, service, expected):
    assert _category(family, service) == expected


def test_short_family_keys_need_word_boundaries():
    """"r2", "d1" and "kv" are two characters. Substring matching would file
    anything containing them in the wrong category, so the names below are
    deliberately adversarial rather than real products."""
    assert _category("", "R2D2 Widget") == "Networking"
    assert _category("", "KVStore Legacy") == "Networking"


def test_bogus_charge_category_is_clamped_to_usage(monkeypatch):
    rows = [{**_ROWS[0], "ChargeCategory": "Recurring"}]
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(rows)})

    rec = asyncio.run(conn.get_costs_as_focus(_START, _END))[0]
    assert rec.ChargeCategory == "Usage"


def test_credit_lines_keep_their_charge_category(monkeypatch):
    rows = [{**_ROWS[0], "ChargeCategory": "Credit", "BilledCost": -5.0}]
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(rows)})

    rec = asyncio.run(conn.get_costs_as_focus(_START, _END))[0]
    assert rec.ChargeCategory == "Credit"
    assert rec.BilledCost == -5.0


def test_non_finite_cost_does_not_poison_the_total(monkeypatch):
    rows = [{**_ROWS[0], "BilledCost": float("nan"), "ContractedCost": None,
             "EffectiveCost": None}]
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(rows)})

    summary = asyncio.run(conn.get_costs(_START, _END))
    assert summary.total_usd == 0.0


def test_mixed_currency_is_flagged_not_converted(monkeypatch):
    rows = [_ROWS[0], {**_ROWS[1], "BillingCurrency": "EUR"}]
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _usage(rows)})

    summary = asyncio.run(conn.get_costs(_START, _END))
    assert summary.currency == "MIXED"


# ── Fallback for accounts the endpoint does not cover ────────────────────────

@pytest.mark.parametrize("failure", [
    _Resp(404),
    _Resp(403),
    _Resp(200, {"success": False, "errors": [{"message": "nope"}], "result": None}),
    _Resp(200, bad_json=True),
    httpx.ConnectError("dns"),
])
def test_falls_back_when_the_endpoint_cannot_answer(monkeypatch, failure):
    history = _Resp(200, {"result": [
        {"amount": "12.50", "type": "Zone Subscription", "zone": {"name": "streamco.tv"}},
    ]})
    conn, calls = _wire(monkeypatch, {_USAGE_PATH: failure, _HISTORY_PATH: history})

    summary = asyncio.run(conn.get_costs(_START, _END))

    assert summary.total_usd == 12.50
    assert summary.by_service == {"Zone Subscription": 12.50}
    assert any(_HISTORY_PATH in u for u in calls)


def test_fallback_still_produces_focus_records(monkeypatch):
    """Coarser, but not missing: fallback lines still normalize, as one flat
    Networking record apiece."""
    history = _Resp(200, {"result": [
        {"amount": "12.50", "type": "Zone Subscription", "zone": {"name": "streamco.tv"}},
    ]})
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _Resp(404), _HISTORY_PATH: history})

    recs = asyncio.run(conn.get_costs_as_focus(_START, _END))

    assert len(recs) == 1
    assert recs[0].ProviderName == "Cloudflare"
    assert recs[0].ServiceCategory == "Networking"
    assert recs[0].BilledCost == 12.50


def test_subscriptions_fallback_when_history_is_unavailable(monkeypatch):
    subs = _Resp(200, {"result": [
        {"component_values": [{"name": "Workers Paid"}], "price": 5.0},
    ]})
    conn, _ = _wire(monkeypatch, {_USAGE_PATH: _Resp(404), _HISTORY_PATH: _Resp(403),
                                  _SUBS_PATH: subs})

    summary = asyncio.run(conn.get_costs(_START, _END))
    assert summary.total_usd == 5.0
    assert summary.by_service == {"Workers Paid": 5.0}
