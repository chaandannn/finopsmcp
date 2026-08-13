# SPDX-License-Identifier: Apache-2.0
"""forecast MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def forecast_costs(
    account_id: str | None = None,
    service: str | None = None,
    horizon_days: int = 30,
    history_days: int = 90,
) -> dict:
    """
    Forecast future cloud spend using Holt-Winters time-series modelling.

    Automatically tunes forecast parameters (alpha/beta/gamma) to your account's
    historical spend patterns and returns a daily point forecast with 80%
    prediction intervals.

    Args:
        account_id:   AWS account ID (auto-discovered from STS if not provided)
        service:      specific service to forecast (e.g. "EC2", "RDS"), omit for total
        horizon_days: number of days to forecast (default 30)
        history_days: days of history to fit the model (default 90, need ≥14)

    Returns forecast including method used, MAPE accuracy %, monthly projection,
    and day-by-day point/lower/upper estimates.
    Examples:
        - "Forecast our AWS spend for next month"
        - "Where will EC2 costs be in 60 days?"

    """
    if (err := _srv.require_pro("forecasting")):
        return err
    try:
        from ..ml.forecasting import Forecaster
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        aws_configured = aws and await aws.is_configured()
        account_id = await _srv._resolve_account_id(account_id)
        if not account_id:
            return {
                "error": "No account_id provided and none could be auto-discovered.",
                "hint": "Connect AWS with `finops setup aws`, or pass account_id explicitly.",
            }
        f = Forecaster.for_account(
            account_id,
            service=service,
            days=history_days,
            aws_connector=aws if aws_configured else None,
        )
        if not f._series:
            return {
                "error": "No historical data found for this account/service.",
                "hint": "Connect your AWS account with `finops setup aws` to enable forecasting.",
            }
        return f.predict_dict(horizon_days)
    except Exception as e:
        return {"error": str(e)}
