"""Regression test for a real bug found while testing the duplicate-capability
scanner: server._active(subset) used `subset or _ALL_CONNECTORS`, so an
explicitly-computed EMPTY dict (falsy in Python) was treated the same as "no
subset given" and silently fell back to every connector. That made
get_cost_summary(provider="typo'd-name") silently return everyone's spend
instead of an error, since {provider: ...} if provider in _ALL_CONNECTORS
else {} produces {} for an invalid name. Only a real None must fall back.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch


def test_active_with_none_falls_back_to_all_connectors():
    import finops.server as srv

    async def _configured():
        return True

    stub = SimpleNamespace(is_configured=_configured)
    # _active(None) reads the pre-materialized _ALL_CONNECTORS, NOT the two
    # source dicts, so patch that one. Also clear the _ACTIVE_CACHE: a real
    # connector configured on the dev box (or an earlier test) would otherwise
    # be served from cache and mask the fallback. (This is the exact bug that
    # let this test pass on a laptop with AWS configured but fail in CI.)
    srv._ACTIVE_CACHE.clear()
    with patch.dict(srv._ALL_CONNECTORS, {"aws": stub}, clear=True):
        result = asyncio.run(srv._active(None))
    srv._ACTIVE_CACHE.clear()
    assert set(result) == {"aws"}


def test_active_with_explicit_empty_dict_stays_empty():
    import finops.server as srv

    async def _configured():
        return True

    # Even with real connectors configured elsewhere, an explicitly empty
    # subset (e.g. an invalid provider name resolved to {}) must never fall
    # back to them.
    with patch.dict(srv.CLOUD_CONNECTORS, {"aws": SimpleNamespace(is_configured=_configured)}):
        result = asyncio.run(srv._active({}))
    assert result == {}


def test_get_cost_summary_rejects_unknown_provider_instead_of_returning_everything():
    import finops.server as srv
    from finops import cache as _cache
    _cache.clear()

    async def _configured():
        return True

    async def _get_costs(start, end, granularity="MONTHLY"):
        return SimpleNamespace(
            provider="aws", start_date=start, end_date=end,
            total_usd=999.0, by_service={"Amazon EC2": 999.0},
            by_account={}, by_region={}, currency="USD",
        )

    aws_stub = SimpleNamespace(is_configured=_configured, get_costs=_get_costs)
    with patch.dict(srv.CLOUD_CONNECTORS, {"aws": aws_stub}, clear=True), \
         patch.dict(srv.SAAS_CONNECTORS, {}, clear=True):
        result = asyncio.run(srv.get_cost_summary(provider="not-a-real-provider"))

    assert "error" in result, (
        "an unknown provider name must error, not silently return every "
        f"connected provider's spend; got {result!r}"
    )
