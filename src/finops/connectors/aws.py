from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary

# Error markers that mean "the session expired, log back in", across SSO,
# MFA/session tokens, and an empty credential chain. Matched against both the
# botocore error code and the exception text/type name.
_EXPIRED_CREDENTIAL_MARKERS = (
    "ExpiredToken", "ExpiredTokenException",
    "SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError",
    "NoCredentialsError", "CredentialRetrievalError",
    "InvalidGrantException", "RefreshWithMFA",
)


def _reauth_hint(session) -> str:
    """A friendly re-login instruction for an expired SSO / MFA session, naming
    the profile so the user knows exactly what to run. The account is NOT lost:
    nable stored a profile reference, so once they log back in, boto3 reads the
    refreshed token from cache and the next query just works, no reconfiguration."""
    prof = ""
    try:
        prof = getattr(session, "profile_name", "") or ""
    except Exception:
        prof = ""
    if prof and prof != "default":
        return (
            f"Your AWS session for profile '{prof}' has expired. Nothing to "
            f"reconfigure: log back in and ask again. For SSO run "
            f"`aws sso login --profile {prof}`, or refresh that profile's MFA "
            f"session. nable picks up the new session automatically."
        )
    return (
        "Your AWS session has expired. Nothing to reconfigure: log back in and ask "
        "again. Run `aws sso login` (or refresh your MFA session) and nable picks "
        "up the new session automatically."
    )


class _DemoRefusingClient:
    """A Cost Explorer client that exists but will not call anything.

    Demo mode serves StreamCo data; every code path is supposed to intercept
    before an API call happens. Handing those paths a real client meant a missed
    interception spent real money on the presenter's account. This keeps
    construction working (paths that build-then-discard are unaffected) and turns
    a missed interception into an obvious error rather than a charge.
    """

    def __getattr__(self, name: str):
        def _refuse(*_a, **_k):
            raise RuntimeError(
                f"demo mode called the real Cost Explorer API ({name}). Demo data "
                f"should have been served before this point; this is a bug in the "
                f"demo interception, not a configuration problem."
            )
        return _refuse


class AWSConnector(BaseConnector):
    provider = "aws"

    def __init__(self, session=None, identity: str | None = None) -> None:
        """
        session: optional boto3.Session to use for all calls.
        If not provided, falls back to environment-based role ARNs or default credentials.
        """
        self._session = session  # set when created for a specific account
        # Which account this connector reads. Part of the cache key: a caller
        # building one connector per account MUST pass it, or entries collide.
        self._identity = identity
        self._role_arns: list[str] = [
            arn.strip()
            for arn in os.getenv("AWS_ROLE_ARNS", "").split(",")
            if arn.strip()
        ]
        _MAX_ROLES = int(os.getenv("FINOPS_MAX_ROLE_ARNS", "50"))
        if len(self._role_arns) > _MAX_ROLES:
            import logging as _log
            _log.getLogger("finops.aws").warning(
                "AWS_ROLE_ARNS has %d entries; capping at %d. Set FINOPS_MAX_ROLE_ARNS to override.",
                len(self._role_arns), _MAX_ROLES,
            )
            self._role_arns = self._role_arns[:_MAX_ROLES]

    def cache_identity(self) -> str:
        """A stable token for WHOSE numbers this connector returns.

        The cost cache used to key on AWS_ROLE_ARNS alone, which is one env var
        shared by every connector in the process. get_cost_summary_all_accounts
        builds one AWSConnector per account inside a single call, so account #1
        populated the entry and accounts #2..N read it back: every account in the
        org reported account #1's spend, and with a 12 hour TTL that also got
        pickled to disk and served after a restart.

        An explicit identity is the right answer. Where there is none but a
        session was injected, this returns a token unique to this instance, so
        the connector simply does not share cache entries with anyone. A miss
        costs one API call; a wrong hit costs a customer the wrong bill.
        """
        if self._identity:
            return f"id:{self._identity}"
        if self._session is not None:
            profile = getattr(self._session, "profile_name", None)
            if profile and profile != "default":
                return f"profile:{profile}"
            # Unidentified injected session: opt out of sharing entirely.
            return f"unshared:{id(self)}"
        return "env:" + (",".join(sorted(self._role_arns)) if self._role_arns else "default")

    async def is_configured(self) -> bool:
        try:
            import boto3  # noqa: F401

            if self._session:
                # Validate that the injected session has working credentials
                creds = self._session.get_credentials()
                return creds is not None

            # Either explicit creds or a role/instance profile must be resolvable
            import botocore.session

            s = botocore.session.get_session()
            creds = s.get_credentials()
            return creds is not None
        except Exception:
            return False

    # ── internal helpers ────────────────────────────────────────────────────

    def _make_client(self, role_arn: str | None = None):
        import boto3
        from botocore.config import Config as _BotoConfig

        # Demo mode must never hold a live Cost Explorer client. It used to get a
        # real one: every demo path is expected to intercept the RESULT and serve
        # StreamCo data instead, so the client was built and discarded. Any path
        # that forgot to intercept would have billed the presenter's own AWS
        # account mid-demo. This returns a client that refuses every call, so a
        # missed interception fails loudly on a laptop instead of silently
        # spending money in front of a customer.
        from ..demo_data import is_demo

        if is_demo():
            return _DemoRefusingClient()

        # Cost Explorer stays: it is what lets somebody point nable at
        # credentials they already have and get a number in a minute. The policy
        # only stops it being called when it is unnecessary: an operator banned
        # it, or this is unattended work where a per-request charge would repeat
        # on a timer with nobody watching.
        #
        # Every branch below returns ce_client(...) rather than boto3.client("ce").
        # It used to call ce_client ONLY to trip the kill switch and then build
        # its own client, which meant the unattended rule was never reached from
        # here and the nightly snapshot billed every customer $0.01 a request
        # while billing_access.py forbade exactly that in words. A guard you call
        # for its side effect and then walk past is not a guard.
        from ..billing_access import ce_client

        # Without explicit timeouts botocore waits 60s per attempt with ~4
        # retries; under CE throttling that leaks worker threads for minutes
        # (asyncio.wait_for cancels the awaiting coroutine, not the thread).
        _cfg = _BotoConfig(
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
        )

        # GovCloud note: AWS Cost Explorer is available in GovCloud regions.
        # Valid GovCloud CE endpoints: us-gov-west-1, us-gov-east-1.
        # Set AWS_DEFAULT_REGION or pass region_name to target GovCloud.
        # No code changes needed — boto3 respects the standard region resolution chain.
        _region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

        # If a session was injected (via account registry), use it directly
        if self._session and not role_arn:
            return ce_client(self._session, region=_region, config=_cfg,
                             reason="AWSConnector._make_client (session)")

        if role_arn:
            # The assume-role round trip happens first, but the client is still
            # built by the gate: an ungated construction here would be a second
            # ungated construction, and the multi-account path is precisely where
            # per-request charges multiply.
            sts = boto3.client("sts")
            assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-mcp")
            creds = assumed["Credentials"]
            return ce_client(
                region=_region, config=_cfg,
                reason="AWSConnector._make_client (assumed role)",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        return ce_client(region=_region, config=_cfg,
                         reason="AWSConnector._make_client")

    def _account_id(self, role_arn: str | None = None) -> str:
        import boto3

        if role_arn:
            return role_arn.split(":")[4]
        if self._session:
            sts = self._session.client("sts")
        else:
            sts = boto3.client("sts")
        return sts.get_caller_identity()["Account"]

    def _client_and_account(self, role_arn: str | None) -> tuple[Any, str]:
        """Both blocking STS round-trips in one call, so one thread hop covers them."""
        return self._make_client(role_arn), self._account_id(role_arn)

    def _build_summary(
        self,
        account_id: str,
        start_date: date,
        end_date: date,
        response: dict,
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0
        currencies: set[str] = set()

        for result in response.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                keys = group.get("Keys", [])
                service = keys[0] if keys else "Unknown"
                region = keys[1] if len(keys) > 1 else ""
                metric = group["Metrics"]["UnblendedCost"]
                amount = float(metric["Amount"])
                unit = metric.get("Unit")
                if unit:
                    currencies.add(unit)
                total += amount
                by_service[service] = by_service.get(service, 0.0) + amount
                if region:
                    by_region[region] = by_region.get(region, 0.0) + amount
                entries.append(
                    CostEntry(
                        provider="aws",
                        account_id=account_id,
                        account_name=account_id,
                        service=service,
                        region=region,
                        amount=amount,
                    )
                )

        return CostSummary(
            provider="aws",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={account_id: total},
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
        group_by = group_by or ["SERVICE"]
        ce_group_by = [{"Type": "DIMENSION", "Key": k} for k in group_by]

        from datetime import timedelta
        _earliest = end_date - timedelta(days=int(os.getenv("FINOPS_MAX_LOOKBACK_DAYS", "365")))
        if start_date < _earliest:
            start_date = _earliest

        # Read-through cache: Cost Explorer bills $0.01 per request, and an agentic
        # session re-asks the same cost question repeatedly. Serve a 12h-fresh copy
        # so repeat queries within a conversation cost nothing. CE data only
        # refreshes a few times a day, so this never serves stale numbers.
        import copy as _copy

        from .. import cache as _cache
        _ck = _cache.make_key(
            "aws.get_costs",
            self.cache_identity(),
            start_date.isoformat(), end_date.isoformat(),
            granularity, ",".join(group_by),
            repr(filters) if filters else "",
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            # Return an independent copy so a caller mutating the result cannot
            # poison the cached entry for the next call.
            return _copy.deepcopy(_hit)

        targets = self._role_arns if self._role_arns else [None]
        merged = CostSummary(
            provider="aws",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        _had_results = False
        for role_arn in targets:
            # to_thread. Both of these are synchronous botocore calls and both
            # reach the network: _make_client does sts:AssumeRole when a role ARN
            # is configured, _account_id does sts:GetCallerIdentity. The CE
            # pagination below was already threaded; these two were not, so they
            # ran on the event loop before it.
            #
            # That made _fetch_costs_cached's per-provider deadline decorative.
            # asyncio cannot cancel a thread it is running on, so the timeout
            # callback only fires once the blocking call has returned by itself,
            # and these run on plain botocore defaults (60s connect, 60s read,
            # plus retries) rather than the 5s/15s config the Cost Explorer
            # client gets. A user with three role ARNs and an STS that is not
            # answering waits minutes on a deadline advertised in seconds, with
            # every other provider in the same gather frozen behind them.
            ce, account_id = await asyncio.to_thread(
                self._client_and_account, role_arn)

            kwargs: dict[str, Any] = dict(
                TimePeriod={
                    "Start": start_date.isoformat(),
                    "End": end_date.isoformat(),
                },
                Granularity=granularity,
                Metrics=["UnblendedCost"],
                GroupBy=ce_group_by,
            )
            if filters:
                kwargs["Filter"] = filters

            # paginate
            results: list[dict] = []
            try:
                while True:
                    resp = await asyncio.to_thread(ce.get_cost_and_usage, **kwargs)
                    results.extend(resp.get("ResultsByTime", []))
                    token = resp.get("NextPageToken")
                    if not token:
                        break
                    kwargs["NextPageToken"] = token
            except Exception as exc:
                err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else type(exc).__name__
                err_str = str(exc)
                if "DataUnavailableException" in err_code or "DataUnavailableException" in err_str:
                    raise RuntimeError(
                        "AWS Cost Explorer data is not yet available for this account. "
                        "Cost Explorer needs up to 24 hours to backfill data after it is first enabled. "
                        "Try again tomorrow, or check the AWS Console > Billing > Cost Explorer."
                    ) from exc
                if err_code in ("InvalidClientTokenId", "AuthFailure") or "InvalidClientTokenId" in err_str:
                    raise RuntimeError(
                        "AWS credentials are invalid. Check your AWS_ACCESS_KEY_ID and "
                        "AWS_SECRET_ACCESS_KEY, then run: finops setup aws"
                    ) from exc
                if err_code == "AccessDenied" or "AccessDenied" in err_str:
                    raise RuntimeError(
                        "AWS credentials are valid but missing Cost Explorer permissions. "
                        "Add ce:GetCostAndUsage to your IAM policy, or run: finops setup aws --iam-template"
                    ) from exc
                if any(m in err_code or m in err_str for m in _EXPIRED_CREDENTIAL_MARKERS):
                    # Expired SSO / MFA / session token. The account config is NOT
                    # lost: nable stored a profile reference, so the user just logs
                    # back in and the next query works. Name the profile and give
                    # the exact command instead of telling them to re-setup.
                    raise RuntimeError(_reauth_hint(self._session)) from exc
                raise

            if results:
                _had_results = True

            summary = self._build_summary(
                account_id, start_date, end_date, {"ResultsByTime": results}
            )
            # merge into combined
            merged.total_usd += summary.total_usd
            for k, v in summary.by_service.items():
                merged.by_service[k] = merged.by_service.get(k, 0.0) + v
            for k, v in summary.by_account.items():
                merged.by_account[k] = merged.by_account.get(k, 0.0) + v
            for k, v in summary.by_region.items():
                merged.by_region[k] = merged.by_region.get(k, 0.0) + v
            merged.entries.extend(summary.entries)

        # Zero-spend only if CE returned rows across every account but the real
        # merged total is 0. Checked against the built total (which sums grouped
        # rows), not the raw ResultsByTime Total, which is empty under GroupBy
        # and used to false-flag active accounts as zero-spend.
        if _had_results and merged.total_usd == 0.0:
            merged._zero_spend_account = True

        # Store a copy so later mutation of the returned object cannot reach the cache.
        _cache.set(_ck, _copy.deepcopy(merged), _cache.COST_TTL)
        return merged

    async def get_daily_totals(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """Per-day unblended cost across all connected accounts, for the spend-over-time
        chart's custom date range. One DAILY Cost Explorer call per account (not one
        per day), summed across accounts. Returns [{"date": "YYYY-MM-DD", "total": float}]
        ordered oldest-first. CE's DAILY End is exclusive, so callers pass end+1 to
        include the final day."""
        from .. import cache as _cache
        _ck = _cache.make_key(
            "aws.get_daily_totals",
            ",".join(sorted(self._role_arns)) if self._role_arns else "default",
            start_date.isoformat(), end_date.isoformat(),
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            return [dict(r) for r in _hit]

        by_day: dict[str, float] = {}
        targets = self._role_arns if self._role_arns else [None]
        for role_arn in targets:
            ce = self._make_client(role_arn)
            kwargs: dict[str, Any] = dict(
                TimePeriod={"Start": start_date.isoformat(), "End": end_date.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
            while True:
                resp = await asyncio.to_thread(ce.get_cost_and_usage, **kwargs)
                for period in resp.get("ResultsByTime", []):
                    day = period.get("TimePeriod", {}).get("Start", "")
                    amt = float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0) or 0.0)
                    if day:
                        by_day[day] = by_day.get(day, 0.0) + amt
                token = resp.get("NextPageToken")
                if not token:
                    break
                kwargs["NextPageToken"] = token

        out = [{"date": d, "total": round(v, 2)} for d, v in sorted(by_day.items())]
        _cache.set(_ck, [dict(r) for r in out], _cache.COST_TTL)
        return out

    async def get_network_breakdown(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """
        Return per-usage-type cost rows for the period, grouped by USAGE_TYPE.

        Used by the traffic-cost tools: the classifier keeps only the network
        line items (DataTransfer, NatGateway, NetworkFirewall, etc.) so callers
        can pass the full grouped result. Returns [{usage_type, cost_usd, account_id}].
        """
        targets = self._role_arns if self._role_arns else [None]
        rows: list[dict[str, Any]] = []
        for role_arn in targets:
            ce = self._make_client(role_arn)
            account_id = self._account_id(role_arn)
            kwargs: dict[str, Any] = dict(
                TimePeriod={"Start": start_date.isoformat(), "End": end_date.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            try:
                while True:
                    resp = await asyncio.to_thread(ce.get_cost_and_usage, **kwargs)
                    for period in resp.get("ResultsByTime", []):
                        for grp in period.get("Groups", []):
                            usage_type = grp.get("Keys", [""])[0]
                            amount = float(grp.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0) or 0.0)
                            if amount <= 0:
                                continue
                            rows.append({
                                "usage_type": usage_type,
                                "cost_usd": amount,
                                "account_id": account_id,
                            })
                    token = resp.get("NextPageToken")
                    if not token:
                        break
                    kwargs["NextPageToken"] = token
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "get_network_breakdown failed (role=%s): %s", role_arn, exc)
        return rows

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
                "line_item_unblended_cost": entry.amount,
                "line_item_blended_cost": entry.amount,
                "pricing_public_on_demand_cost": entry.amount,
                "product_servicename": entry.service,
                "line_item_product_code": entry.service,
                "product_region": entry.region,
                "line_item_usage_account_id": entry.account_id,
                "line_item_line_item_type": "Usage",
                "bill_billing_period_start_date": period_start.isoformat(),
                "bill_billing_period_end_date": period_end.isoformat(),
                "line_item_usage_start_date": period_start.isoformat(),
                "line_item_usage_end_date": period_end.isoformat(),
                "resource_tags": entry.tags,
            }
            records.append(normalize("aws", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        if self._role_arns:
            return [
                {"id": arn.split(":")[4], "name": arn.split(":")[4]}
                for arn in self._role_arns
            ]
        # to_thread: _account_id calls sts:GetCallerIdentity synchronously, and
        # check_connector_health wraps THIS coroutine in
        # asyncio.wait_for(..., timeout=10.0) and reports "Timeout (>10s),
        # credentials may be valid but API is slow" when it trips. It could not
        # trip: with no await in the body the coroutine never suspends, so the
        # 10s callback never got the loop. Against an STS that is not answering,
        # the diagnostic tool a stuck user is told to run hung for minutes and
        # then reported healthy: True with a response time in the hundreds of
        # thousands of milliseconds.
        account_id = await asyncio.to_thread(self._account_id)
        return [{"id": account_id, "name": account_id}]
