# SPDX-License-Identifier: Apache-2.0
"""aws MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def list_aws_accounts() -> dict:
    """
    List all AWS accounts configured in ~/.finops-mcp/accounts.yaml.

    Shows each account's name, account ID, region, and auth method.
    Use account names as the 'account' parameter in cost tools like get_cost_summary.

    Examples:
        - "What AWS accounts do I have configured?"
        - "List all my AWS accounts"
        - "Which account is the default?"
    """
    try:
        from ..accounts import list_accounts as _list, get_default_account
        accounts = _list()
        default = get_default_account()
        default_name = default.name if default else ""

        if not accounts:
            return {
                "accounts": [],
                "message": (
                    "No accounts configured. Run 'finops setup aws' to add one, "
                    "or 'finops setup aws --org' to auto-discover from AWS Organizations."
                ),
            }

        return {
            "default_account": default_name,
            "count": len(accounts),
            "accounts": [
                {
                    "name": a.name,
                    "account_id": a.account_id,
                    "region": a.region,
                    "auth": "role_arn" if a.role_arn else "profile" if a.profile else "default_credentials",
                    "is_default": a.name == default_name,
                    "tags": a.tags,
                }
                for a in accounts
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_traffic_cost_breakdown(
    days: int = 30,
) -> dict:
    """
    Break down AWS network/data-transfer spend: how much, and where it goes.

    Splits your traffic cost into INTERNAL (cross-AZ, cross-region, NAT, VPC
    peering, private endpoints) vs EXTERNAL (internet egress, CDN), then a
    per-scope breakdown and a ranked solve playbook (VPC endpoints,
    topology-aware routing, CDN, peering). Pulls Cost Explorer grouped by usage
    type; the classifier keeps only the network line items. AWS today; GCP and
    Azure decomposition are on the roadmap.

    Args:
        days: Look-back window in days (default 30).

    Examples:
        - "How much are we spending on network traffic and where is it going?"
        - "What's our internal vs external data transfer cost?"
        - "Break down our cross-AZ and egress spend"
    """
    from datetime import date as _date, timedelta
    from ..analyzers.traffic import build_traffic_breakdown

    aws = _srv.CLOUD_CONNECTORS.get("aws")
    if aws is None:
        return {"error": "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."}

    end = _date.today()
    start = end - timedelta(days=days)
    try:
        rows = await aws.get_network_breakdown(start, end)
    except Exception as e:
        return {"error": f"Could not pull cost data: {e}"}

    result = build_traffic_breakdown(rows, "aws")
    result["period"] = f"{start} to {end} ({days} days)"
    result["note"] = (
        "AWS only. Internal = stays in your cloud (cross-AZ, cross-region, NAT, "
        "peering); external = leaves it (internet egress, CDN). Ingress is free "
        "and excluded from the split."
    )
    return result


@_srv.mcp.tool()
async def get_data_transfer_costs(
    start_date: str | None = None,
    end_date: str | None = None,
    threshold_usd: float = 50.0,
) -> dict:
    """
    Identify significant data transfer cost line items from AWS Cost Explorer.

    Surfaces internet egress, cross-AZ transfer, inter-region transfer, and
    NAT Gateway data charges. Each finding includes a specific cost-reduction
    recommendation (VPC endpoints, CloudFront, regional consolidation).

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        threshold_usd: Only return usage types costing more than this (default $50).

    Examples:
        - "What are our data transfer costs?"
        - "How much are we paying for inter-region traffic?"
        - "Which data transfer charges are most expensive?"
        - "Are we overpaying for NAT Gateway data transfer?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_data_transfer_costs

        sd, ed = _srv._default_dates()
        if start_date:
            sd = _srv.date.fromisoformat(start_date)
        if end_date:
            ed = _srv.date.fromisoformat(end_date)

        ce = boto3.client("ce", region_name="us-east-1")
        findings = check_data_transfer_costs(
            ce,
            start=sd.isoformat(),
            end=ed.isoformat(),
            threshold_usd=threshold_usd,
        )
        findings.sort(key=lambda x: x.get("monthly_cost", 0), reverse=True)
        total_cost = sum(f.get("monthly_cost", 0) for f in findings)
        total_potential_savings = sum(f.get("estimated_monthly_savings", 0) for f in findings)

        return {
            "period": {"start": sd.isoformat(), "end": ed.isoformat()},
            "total_transfer_cost": round(total_cost, 2),
            "estimated_reducible_savings": round(total_potential_savings, 2),
            "count": len(findings),
            "findings": findings,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def connect_aws(account_id: str = "") -> dict:
    """
    Connect an AWS account from inside your MCP client, no terminal needed.

    Propose-then-confirm and local-only. It reads AWS credentials that already
    exist on this machine (named profiles, environment, the default chain),
    verifies each against STS, and connects the one you choose. It never creates,
    modifies, or deletes anything in your AWS account, and credentials stay on
    this machine.

    Call it with no arguments first to see which accounts are available (nothing
    is stored). Then call it again with account_id set to the one to connect.

    Examples:
        - "Connect my AWS account"
        - "Use the credentials on this machine to connect AWS"
        - "I want to see my real costs, not the sample data"

    Args:
        account_id: The 12-digit account to connect, from the candidate list a
            no-argument call returns. Omit to just list what's available.
    """
    from ..cloudshell import COMMAND as _CLOUDSHELL_CMD
    from ..setup_wizard import (
        _detect_aws_candidates, _emit_provider_connected, _auto_aws_name,
        _detect_sso_profiles_needing_login,
    )
    from ..accounts import AccountConfig, add_account, list_accounts

    candidates = await _srv.asyncio.to_thread(_detect_aws_candidates)
    connected_ids = {a.account_id for a in list_accounts() if a.account_id}

    # AWS Identity Center (SSO) profiles that are configured in ~/.aws/config but
    # not logged in. A plain STS probe drops these silently, so surface them with
    # the exact `aws sso login` command instead of pretending they do not exist.
    sso_pending = await _srv.asyncio.to_thread(_detect_sso_profiles_needing_login)
    connected_profiles = {a.profile for a in list_accounts() if getattr(a, "profile", None)}
    sso_pending = [s for s in sso_pending if s["profile"] not in connected_profiles]

    def _sso_hint(sso_list):
        """Shared block telling the user how to light up their SSO profiles."""
        return {
            "sso_profiles_needing_login": [
                {
                    "profile": s["profile"],
                    "account_id": s["account_id"] or "(unknown until login)",
                    "login_command": s["login_command"],
                }
                for s in sso_list
            ],
            "sso_note": (
                f"Found {len(sso_list)} AWS Identity Center (SSO) profile(s) in ~/.aws/config "
                "that are not logged in yet. Run the login command for the one you want, then "
                "call connect_aws again and it will appear as a connectable account. For example: "
                f"`{sso_list[0]['login_command']}`."
            ),
        }

    if not candidates:
        result = {
            "connected": False,
            "candidates": [],
            "message": (
                "No logged-in AWS credentials were found on this machine."
                + (" But nable did find SSO profiles that just need a login, see below."
                   if sso_pending else "")
            ),
            "how_to_connect": [
                # The command comes from cloudshell.py so the model never quotes a
                # stale one; the old literal here told users to pip install a
                # package their CloudShell's Python 3.9 cannot resolve.
                f"Fastest, in AWS CloudShell (already signed in): run '{_CLOUDSHELL_CMD}' "
                "and it uses CloudShell's own credentials.",
                "Or create a read-only access key: AWS console -> IAM -> your user -> "
                "Security credentials -> Create access key, then run 'finops setup aws'.",
            ],
            "note": ("connect_aws only reads credentials that already exist locally. It never "
                     "creates or changes anything in your AWS account."),
        }
        if sso_pending:
            result.update(_sso_hint(sso_pending))
        return result

    # No account chosen yet: propose what was found and store nothing.
    if not account_id:
        result = {
            "connected": False,
            "candidates": [
                {
                    "account_id": c["account_id"],
                    "label": c["label"],
                    "alias": c.get("alias") or "",
                    "source": "profile" if c.get("profile") else "default_chain",
                    "already_connected": c["account_id"] in connected_ids,
                }
                for c in candidates
            ],
            "message": (
                f"Found working AWS credentials for {len(candidates)} account(s). "
                "Call connect_aws again with account_id set to the one you want to connect."
                + (f" Also found {len(sso_pending)} SSO profile(s) that need `aws sso login` first, see below."
                   if sso_pending else "")
            ),
            "note": ("Nothing was stored. connect_aws only reads local credentials and never "
                     "changes your AWS account."),
        }
        if sso_pending:
            result.update(_sso_hint(sso_pending))
        return result

    match = next((c for c in candidates if c.get("account_id") == account_id), None)
    if match is None:
        return {
            "connected": False,
            "error": f"No detected credentials map to account {account_id}.",
            "available": [c["account_id"] for c in candidates],
        }

    if account_id in connected_ids:
        return {
            "connected": True,
            "account_id": account_id,
            "message": f"Account {account_id} is already connected. Ask for your cost summary.",
        }

    taken = {a.name for a in list_accounts()}
    name = _auto_aws_name(match, taken)
    cfg = AccountConfig(
        name=name,
        account_id=match["account_id"],
        region=match.get("region") or "us-east-1",
        profile=match.get("profile") or None,
    )
    await _srv.asyncio.to_thread(add_account, cfg)
    auth = "profile" if match.get("profile") else "default_chain"
    _emit_provider_connected(auth)
    # Drop the cached "no provider" answer so the next cost tool returns real data
    # this session, not the demo stub.
    from .. import demo_data as _dd
    _dd._real_provider_cache = None
    _srv._tool_surface_changed()
    return {
        "connected": True,
        "account_id": match["account_id"],
        "saved_as": name,
        "auth_method": auth,
        "next": ("Connected. Ask me for your cost summary or top cost drivers to see your "
                 "real numbers now."),
        "note": ("Credentials stay on this machine. nable reads billing data only; it never "
                 "changes your AWS account."),
    }


@_srv.mcp.tool()
async def get_resource_cost_breakdown_aws(
    start_date: str | None = None,
    end_date: str | None = None,
    service: str | None = None,
    account_id: str | None = None,
    min_cost_usd: float = 1.0,
    limit: int = 100,
) -> dict:
    """
    Return per-resource AWS cost detail from the Cost and Usage Report (CUR)
    via Athena. Includes unblended cost, on-demand equivalent, and effective
    savings from Savings Plans or Reserved Instances.

    Requires CUR delivery to S3 and an Athena database. Pro plan feature.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        service: AWS service code filter (e.g. "Amazon EC2"). None = all services.
        account_id: 12-digit AWS account ID filter. None = all accounts.
        min_cost_usd: Exclude resources below this cost threshold (default $1).
        limit: Maximum resources to return ordered by cost descending (default 100).

    Examples:
        - "Show me per-resource EC2 costs from CUR"
        - "Which S3 buckets are costing the most this month?"
        - "Break down costs by resource for account 123456789012"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.cur import get_resource_costs
        result = get_resource_costs(
            start_date=sd,
            end_date=ed,
            service=service,
            account_id=account_id,
            min_cost_usd=min_cost_usd,
            limit=limit,
        )

        resources = result.get("resources")
        if isinstance(resources, list) and resources:
            resources.sort(key=lambda r: r.get("unblended_cost", 0), reverse=True)
            kept, omitted = _srv.fit_to_budget(resources, max_tokens=6000)
            if omitted > 0:
                result["resources"] = kept
                result["resources_truncated"] = omitted
                result["hint"] = (
                    f"showing top {len(kept)} of {len(resources)} resources by cost; "
                    "narrow with service, account_id, or region, or raise min_cost_usd for detail. "
                    "total_cost and total_resources reflect the full result set."
                )
            total_savings = sum(r.get("effective_savings", 0) for r in resources)
            result["cost_note"] = _srv.cost_note(result, savings_found_usd=total_savings or None)

        return result
    except Exception as exc:
        _srv.log.error("get_resource_cost_breakdown_aws failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_bedrock_costs(days: int = 30, account: str = "") -> str:
    """
    Break down Amazon Bedrock costs by model and token type.

    Shows spend per model (Claude, Titan, Llama, etc.), input vs output token
    split, cost per 1k tokens, and trend vs the prior period.

    Args:
        days:    Number of days to analyze (default 30).
        account: Reserved for future multi-account support.
    Examples:
        - "What is Bedrock costing us?"
        - "Bedrock spend by model this month"

    """
    try:
        from ..connectors.aws_services.bedrock import BedrockAnalyzer
        analyzer = BedrockAnalyzer(region="us-east-1")
        return analyzer.get_costs(days=days)
    except Exception as e:
        return f"Bedrock cost analysis unavailable: {e}"


@_srv.mcp.tool()
async def get_marketplace_costs(days: int = 30, account: str = "") -> str:
    """
    Break down AWS Marketplace costs by product and vendor.

    Surfaces per-product spend, month-over-month trends, and flags
    products with more than $1,000 in spend for review.

    Args:
        days:    Number of days to analyze (default 30).
        account: Reserved for future multi-account support.
    Examples:
        - "What AWS Marketplace subscriptions are we paying for?"

    """
    try:
        from ..connectors.aws_services.marketplace import MarketplaceAnalyzer
        analyzer = MarketplaceAnalyzer()
        return analyzer.get_costs(days=days)
    except Exception as e:
        return f"Marketplace cost analysis unavailable: {e}"
